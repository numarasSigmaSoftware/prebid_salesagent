#!/usr/bin/env python3
"""
E2E tests for A2A webhook payload type compliance.

Per AdCP A2A spec (https://docs.adcontextprotocol.org/docs/protocols/a2a-guide#push-notifications-a2a-specific):
- Final states (completed, failed, canceled): Send full Task object with artifacts
- Intermediate states (working, input-required, submitted): Send TaskStatusUpdateEvent

This test validates that our A2A server sends the correct payload type based on status.
"""

import os
import uuid
from time import sleep
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from tests.e2e._webhook_capture import WebhookCaptureHandler, run_webhook_capture_server
from tests.e2e.adcp_request_builder import (
    build_a2a_message_send,
    build_adcp_media_buy_request,
    get_test_date_range,
    parse_tool_result,
)
from tests.e2e.utils import live_db_env, make_mcp_client, set_live_adapter_behavior


async def _discover_product_and_pricing(live_server: dict, test_auth_token: str) -> tuple[str, str]:
    """Discover a real product_id + pricing_option_id via get_products.

    The A2A create_media_buy skill handler only accepts the AdCP-spec
    ``packages[]`` format — legacy ``product_ids``/``total_budget`` is rejected
    with VALIDATION_ERROR before the manual-approval path runs, so a legacy
    request can never yield a ``submitted`` TaskStatusUpdateEvent webhook
    . Building a valid packages request needs a real
    pricing_option_id; discover it like test_adcp_full_lifecycle does.
    """
    async with make_mcp_client(live_server, token=test_auth_token) as client:
        products_result = await client.call_tool(
            "get_products",
            {"brand": {"domain": "testbrand.com"}, "brief": "video advertising"},
        )
        products_data = parse_tool_result(products_result)
    products = products_data["products"]
    assert products, "ci-test tenant must expose at least one product"
    product = products[0]
    pricing_options = product.get("pricing_options", [])
    assert pricing_options, f"Product {product['product_id']} must expose pricing_options"
    return product["product_id"], pricing_options[0]["pricing_option_id"]


class SnakeCaseWireViolation(AssertionError):
    """Raised when an A2A webhook payload uses snake_case keys instead of camelCase.

    The A2A v0.3 protobuf descriptor declares explicit JSON names (json_name) for
    every field: task_id -> "taskId", context_id -> "contextId", message_id ->
    "messageId". google.protobuf.json_format.MessageToDict() emits these camelCase
    names by default. Passing preserving_proto_field_name=True overrides them with
    snake_case, which silently breaks every spec-compliant A2A consumer. This
    classifier fails loudly so that regression cannot pass as an "unknown" payload.
    """


# Proto fields whose snake_case form on the wire is a spec violation. The value is
# the spec-compliant camelCase wire name (proto json_name).
_SNAKE_CASE_WIRE_VIOLATIONS = {
    "task_id": "taskId",
    "context_id": "contextId",
    "message_id": "messageId",
}


def classify_a2a_payload(payload: dict[str, Any]) -> str:
    """Classify an A2A webhook payload as 'Task' or 'TaskStatusUpdateEvent'.

    Per A2A spec:
    - Task has an 'id' field (final states: completed, failed, canceled)
    - TaskStatusUpdateEvent has a 'taskId' field (intermediate states)

    Raises:
        SnakeCaseWireViolation: if the payload carries snake_case proto field names
            (task_id/context_id/message_id) — a wire contract violation that must
            never be silently classified as 'unknown'.
        AssertionError: if the payload matches neither Task nor TaskStatusUpdateEvent.
    """
    snake_keys_present = sorted(k for k in _SNAKE_CASE_WIRE_VIOLATIONS if k in payload)
    if snake_keys_present:
        expected = {k: _SNAKE_CASE_WIRE_VIOLATIONS[k] for k in snake_keys_present}
        raise SnakeCaseWireViolation(
            f"A2A webhook payload uses snake_case wire keys {snake_keys_present}; "
            f"the A2A spec requires camelCase {expected}. Payload keys: {sorted(payload)}"
        )

    if "taskId" in payload:
        return "TaskStatusUpdateEvent"
    if "id" in payload:
        return "Task"
    raise AssertionError(
        f"A2A webhook payload is neither Task (has 'id') nor "
        f"TaskStatusUpdateEvent (has 'taskId'). Payload keys: {sorted(payload)}"
    )


def assert_no_classification_errors(received: list[dict[str, Any]]) -> None:
    """Fail if any captured webhook could not be classified as a valid A2A payload.

    A non-None ``classification_error`` means the payload used snake_case wire keys
    (the gh-#1299 bug) or matched neither Task nor TaskStatusUpdateEvent. Either way
    it is a spec violation that must fail the test loudly — never pass as 'unknown'.
    """
    errors = [(w["status"], w["classification_error"]) for w in received if w["classification_error"] is not None]
    assert not errors, (
        f"{len(errors)} webhook payload(s) failed A2A wire classification (snake_case or unrecognised shape): {errors}"
    )


class WebhookPayloadCapture(WebhookCaptureHandler):
    """Webhook receiver that captures each payload with its A2A classification.

    Extends the shared capture handler via the ``record`` hook — only the
    classification logic lives here, never a copied ``do_POST``.
    """

    received_webhooks: list[dict[str, Any]] = []

    def record(self, payload):
        # Extract status
        status = None
        if "status" in payload:
            status_obj = payload["status"]
            if isinstance(status_obj, dict):
                status = status_obj.get("state")
            else:
                status = str(status_obj)

        # A2A wire contract is camelCase (proto json_name): taskId, contextId,
        # messageId. snake_case (task_id, context_id) is a spec violation — the
        # a2a-sdk protobuf descriptor declares the JSON names explicitly. Record
        # the classification (or its failure) BEFORE responding so a regression
        # in protocol_webhook_service is observable to the test instead of being
        # swallowed by an "unknown" classification (gh-#1299 follow-up).
        classification_error = None
        payload_type = None
        try:
            payload_type = classify_a2a_payload(payload)
        except AssertionError as classify_exc:
            classification_error = str(classify_exc)

        return {
            "payload": payload,
            "payload_type": payload_type,
            "classification_error": classification_error,
            "status": status,
            "path": self.path,
        }


@pytest.fixture
def webhook_capture_server():
    """Start a local HTTP server to capture webhook payloads."""
    with run_webhook_capture_server(WebhookPayloadCapture, WebhookPayloadCapture.received_webhooks) as info:
        yield info


class TestA2AWebhookPayloadTypes:
    """Test A2A webhook payload type compliance with AdCP spec."""

    @pytest.mark.asyncio
    async def test_synchronous_completed_response_sends_no_webhook(
        self,
        docker_services_e2e,
        live_server,
        test_auth_token,
        webhook_capture_server,
    ):
        """
        Test that completed status sends a Task payload (not TaskStatusUpdateEvent).

        Per AdCP spec:
        - Completed is a final state
        - Final states should send Task object with artifacts
        """
        # Enable auto-approval so create_media_buy completes immediately
        set_live_adapter_behavior(live_server, manual_approval_required=False)

        a2a_url = f"{live_server['a2a']}/a2a"
        context_id = str(uuid.uuid4())

        product_id, pricing_option_id = await _discover_product_and_pricing(live_server, test_auth_token)
        start_time, end_time = get_test_date_range(days_from_now=1, duration_days=30)
        media_buy_params = build_adcp_media_buy_request(
            product_ids=[product_id],
            total_budget=5000.0,
            start_time=start_time,
            end_time=end_time,
            brand={"domain": "testbrand.com"},
            pricing_option_id=pricing_option_id,
            context={"e2e": "webhook_completed_test"},
        )

        message = build_a2a_message_send(
            skill="create_media_buy",
            parameters=media_buy_params,
            context_id=context_id,
            push_notification_config={
                "url": webhook_capture_server["url"],
                "authentication": {
                    "schemes": ["Bearer"],
                    "credentials": "test-webhook-token-that-is-at-least-32-chars",
                },
            },
        )

        headers = {
            "Authorization": f"Bearer {test_auth_token}",
            "Content-Type": "application/json",
            "x-adcp-tenant": "ci-test",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(a2a_url, json=message, headers=headers)

            # Request should succeed
            assert response.status_code == 200, f"A2A request failed: {response.text}"
            result = response.json()
            assert "error" not in result, f"A2A error: {result.get('error')}"

        sleep(2)
        assert webhook_capture_server["received"] == [], (
            "A synchronous terminal response must not be duplicated as an initial webhook"
        )

    @pytest.mark.asyncio
    async def test_initial_submitted_is_not_webhooked_but_later_transition_is(
        self,
        docker_services_e2e,
        live_server,
        test_auth_token,
        webhook_capture_server,
    ):
        """
        Test that submitted status sends a TaskStatusUpdateEvent payload.

        Per AdCP spec:
        - Submitted is an intermediate state
        - Intermediate states should send TaskStatusUpdateEvent
        """
        # Enable manual approval so create_media_buy returns submitted state
        set_live_adapter_behavior(live_server, manual_approval_required=True)

        a2a_url = f"{live_server['a2a']}/a2a"
        context_id = str(uuid.uuid4())

        # AdCP-spec packages[] format (the A2A skill rejects legacy
        # product_ids/total_budget before the manual-approval path).
        product_id, pricing_option_id = await _discover_product_and_pricing(live_server, test_auth_token)
        start_time, end_time = get_test_date_range(days_from_now=1, duration_days=30)
        media_buy_params = build_adcp_media_buy_request(
            product_ids=[product_id],
            total_budget=50000.0,
            start_time=start_time,
            end_time=end_time,
            brand={"domain": "testbrand.com"},
            pricing_option_id=pricing_option_id,
            context={"e2e": "webhook_submitted_test"},
        )
        # Send A2A create_media_buy message that triggers approval workflow
        message = build_a2a_message_send(
            skill="create_media_buy",
            parameters=media_buy_params,
            context_id=context_id,
            push_notification_config={
                "url": webhook_capture_server["url"],
                "authentication": {
                    "schemes": ["Bearer"],
                    "credentials": "test-webhook-token-that-is-at-least-32-chars",
                },
            },
        )

        headers = {
            "Authorization": f"Bearer {test_auth_token}",
            "Content-Type": "application/json",
            "x-adcp-tenant": "ci-test",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(a2a_url, json=message, headers=headers)

            # Request should succeed (returns submitted status for async operations)
            assert response.status_code == 200, f"A2A request failed: {response.text}"
            response_body = response.json()
            task_id = response_body["result"]["id"]

        # A submitted task is only an initial response; no publisher action means
        # there is no later event to deliver. Drive an explicit live-server
        # approval transition through its committed workflow/task outboxes.
        from src.core.database.models import Tenant

        with live_db_env(live_server) as env:
            tenant_id = env.get_session().scalars(select(Tenant.tenant_id).filter_by(subdomain="ci-test")).one()
        test_control_token = os.environ["ADCP_TEST_CONTROL_TOKEN"]
        async with httpx.AsyncClient(timeout=30.0) as client:
            transition_response = await client.post(
                f"{live_server['mcp']}/_internal/testing/a2a-tasks/{task_id}/complete",
                headers={"x-adcp-test-control-token": test_control_token},
                json={
                    "tenant_id": tenant_id,
                    "principal_id": "ci-test-principal",
                    "response_data": {"status": "approved"},
                },
            )
        assert transition_response.status_code == 200, transition_response.text
        assert transition_response.json()["published"] is True

        # Wait for webhook to be delivered
        timeout_seconds = 15
        poll_interval = 0.5
        elapsed = 0

        # A manual-approval media buy emits the intermediate `submitted`
        # TaskStatusUpdateEvent first, then (mock auto-approval simulation) a
        # terminal `completed` Task. Breaking on merely the first delivery
        # races against that ordering — poll until the submitted webhook is
        # actually captured (or timeout).
        while elapsed < timeout_seconds and not any(
            w["status"] in {"completed", "failed", "rejected"} for w in webhook_capture_server["received"]
        ):
            sleep(poll_interval)
            elapsed += poll_interval

        received = webhook_capture_server["received"]
        assert received, "Expected at least one webhook delivery"

        # No received webhook may carry a snake_case wire violation (gh-#1299).
        assert_no_classification_errors(received)

        assert not [w for w in received if w["status"] == "submitted"]
        terminal = [w for w in received if w["status"] in {"completed", "failed", "rejected"}]
        assert terminal, f"Expected a later terminal transition; got {[w['status'] for w in received]}"
        assert terminal[0]["payload_type"] == "Task"
        assert terminal[0]["payload"].get("artifacts")

    @pytest.mark.asyncio
    async def test_synchronous_success_does_not_emit_initial_webhook(
        self,
        docker_services_e2e,
        live_server,
        test_auth_token,
        webhook_capture_server,
    ):
        """A terminal state returned inline is not repeated as a webhook."""
        # Enable auto-approval
        set_live_adapter_behavior(live_server, manual_approval_required=False)

        a2a_url = f"{live_server['a2a']}/a2a"
        context_id = str(uuid.uuid4())

        product_id, pricing_option_id = await _discover_product_and_pricing(live_server, test_auth_token)
        start_time, end_time = get_test_date_range(days_from_now=1, duration_days=30)
        media_buy_params = build_adcp_media_buy_request(
            product_ids=[product_id],
            total_budget=8000.0,
            start_time=start_time,
            end_time=end_time,
            brand={"domain": "testbrand.com"},
            pricing_option_id=pricing_option_id,
            context={"e2e": "webhook_payload_type_match_test"},
        )

        message = build_a2a_message_send(
            skill="create_media_buy",
            parameters=media_buy_params,
            context_id=context_id,
            push_notification_config={"url": webhook_capture_server["url"]},
        )

        headers = {
            "Authorization": f"Bearer {test_auth_token}",
            "Content-Type": "application/json",
            "x-adcp-tenant": "ci-test",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            await client.post(a2a_url, json=message, headers=headers)

        sleep(2)
        assert webhook_capture_server["received"] == []


class TestWebhookPayloadStructure:
    """Test webhook payload structure compliance."""

    @pytest.mark.asyncio
    async def test_synchronous_task_is_not_repeated_as_webhook(
        self,
        docker_services_e2e,
        live_server,
        test_auth_token,
        webhook_capture_server,
    ):
        """The complete Task returned inline is not pushed a second time."""
        set_live_adapter_behavior(live_server, manual_approval_required=False)

        a2a_url = f"{live_server['a2a']}/a2a"

        product_id, pricing_option_id = await _discover_product_and_pricing(live_server, test_auth_token)
        start_time, end_time = get_test_date_range(days_from_now=1, duration_days=30)
        media_buy_params = build_adcp_media_buy_request(
            product_ids=[product_id],
            total_budget=3000.0,
            start_time=start_time,
            end_time=end_time,
            brand={"domain": "testbrand.com"},
            pricing_option_id=pricing_option_id,
            context={"e2e": "webhook_task_required_fields_test"},
        )

        message = build_a2a_message_send(
            skill="create_media_buy",
            parameters=media_buy_params,
            push_notification_config={"url": webhook_capture_server["url"]},
        )

        headers = {
            "Authorization": f"Bearer {test_auth_token}",
            "Content-Type": "application/json",
            "x-adcp-tenant": "ci-test",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            await client.post(a2a_url, json=message, headers=headers)

        sleep(2)
        assert webhook_capture_server["received"] == []

    @pytest.mark.asyncio
    async def test_initial_submitted_state_is_not_pushed(
        self,
        docker_services_e2e,
        live_server,
        test_auth_token,
        webhook_capture_server,
    ):
        """The initial submitted response is not emitted as a push event."""
        # Enable manual approval to get submitted status
        set_live_adapter_behavior(live_server, manual_approval_required=True)

        a2a_url = f"{live_server['a2a']}/a2a"

        # AdCP-spec packages[] format (legacy product_ids/total_budget is
        # rejected before the manual-approval path → no submitted webhook).
        product_id, pricing_option_id = await _discover_product_and_pricing(live_server, test_auth_token)
        start_time, end_time = get_test_date_range(days_from_now=1, duration_days=30)
        media_buy_params = build_adcp_media_buy_request(
            product_ids=[product_id],
            total_budget=10000.0,
            start_time=start_time,
            end_time=end_time,
            brand={"domain": "testbrand.com"},
            pricing_option_id=pricing_option_id,
            context={"e2e": "webhook_tsue_required_fields"},
        )

        # Trigger an async operation that sends intermediate status
        message = build_a2a_message_send(
            skill="create_media_buy",
            parameters=media_buy_params,
            push_notification_config={"url": webhook_capture_server["url"]},
        )

        headers = {
            "Authorization": f"Bearer {test_auth_token}",
            "Content-Type": "application/json",
            "x-adcp-tenant": "ci-test",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            await client.post(a2a_url, json=message, headers=headers)

        sleep(2)
        assert webhook_capture_server["received"] == []


class TestProtocolWebhookWireFormat:
    """Hermetic wire-format contract tests for ProtocolWebhookService.

    These exercise the real ``ProtocolWebhookService.send_notification`` code path
    against a local HTTP capture server — no Docker stack, no database. They are
    the regression guard for gh-#1299: dropping ``preserving_proto_field_name=True``
    so A2A protobuf payloads serialize with the spec-mandated camelCase wire names.

    Mutation contract: re-adding ``preserving_proto_field_name=True`` to
    ``protocol_webhook_service.py`` makes
    ``test_task_status_update_event_serializes_camelcase`` FAIL (snake_case keys
    raise SnakeCaseWireViolation in the capture classifier).
    """

    @pytest.fixture(autouse=True)
    def _allow_loopback_receiver(self, monkeypatch):
        """Permit delivery to the loopback HTTP capture server (#1512 SSRF).

        These hermetic tests are a trusted local harness whose receiver lives on
        127.0.0.1 over plain HTTP — the exact case the dedicated
        ``ADCP_ALLOW_PRIVATE_WEBHOOKS`` opt-in covers (the E2E compose stack sets the
        same flag). Without it the SSRF validator correctly rejects the private/HTTP
        target and no webhook is delivered.
        """
        monkeypatch.setenv("ADCP_ALLOW_PRIVATE_WEBHOOKS", "true")
        from tests.helpers.webhook_signing import webhook_signing_jwk_json

        monkeypatch.setenv("ADCP_WEBHOOK_SIGNING_JWK", webhook_signing_jwk_json())
        monkeypatch.setenv("ADCP_WEBHOOK_SIGNING_JWKS_URI", "https://seller.example/.well-known/jwks.json")

    def _send_and_capture(self, payload) -> dict[str, Any]:
        """Send `payload` via the real service and return the classified capture."""
        import asyncio
        from unittest.mock import patch

        from src.core.database.models import PushNotificationConfig
        from src.services.protocol_webhook_service import ProtocolWebhookService

        # host='127.0.0.1': this class is unit-style (no Docker) — the service
        # runs in-process, so loopback is always the right callback host.
        # Real outbound validator allows localhost when ADCP_TESTING=true
        # (do not patch the SSRF gate — that would hide regressions).
        with (
            run_webhook_capture_server(
                WebhookPayloadCapture, WebhookPayloadCapture.received_webhooks, host="127.0.0.1"
            ) as info,
            patch.dict("os.environ", {"ADCP_TESTING": "true"}),
        ):
            config = PushNotificationConfig(
                id="pnc-test",
                tenant_id="t-test",
                principal_id="p-test",
                url=info["url"],
                authentication_type=None,
                authentication_token=None,
            )
            service = ProtocolWebhookService()
            sent = asyncio.run(service.send_notification(config, payload, metadata={"task_type": "create_media_buy"}))
            assert sent is True, "ProtocolWebhookService.send_notification should report success"

            received = list(info["received"])

        assert len(received) == 1, f"Expected exactly one captured webhook, got {len(received)}"
        return received[0]

    def test_task_status_update_event_serializes_camelcase(self):
        """TaskStatusUpdateEvent must hit the wire as camelCase (taskId, not task_id).

        This is the gh-#1299 regression guard and the mutation-test target.
        """
        from a2a.types import TaskState, TaskStatus, TaskStatusUpdateEvent

        event = TaskStatusUpdateEvent(
            task_id="t-123",
            context_id="c-456",
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
        )

        capture = self._send_and_capture(event)
        payload = capture["payload"]

        assert capture["classification_error"] is None, (
            f"Wire payload failed A2A classification (snake_case regression?): {capture['classification_error']}"
        )
        assert capture["payload_type"] == "TaskStatusUpdateEvent"
        assert payload["taskId"] == "t-123", f"Expected camelCase 'taskId', got payload keys {sorted(payload)}"
        assert payload["contextId"] == "c-456", f"Expected camelCase 'contextId', got payload keys {sorted(payload)}"
        assert "task_id" not in payload, "snake_case 'task_id' must not appear on the A2A wire"
        assert "context_id" not in payload, "snake_case 'context_id' must not appear on the A2A wire"

    def test_task_serializes_camelcase(self):
        """Final-state Task must serialize with camelCase contextId and classify as Task."""
        from a2a.types import Task, TaskState, TaskStatus

        task = Task(
            id="t-789",
            context_id="c-789",
            status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
        )

        capture = self._send_and_capture(task)
        payload = capture["payload"]

        assert capture["classification_error"] is None, (
            f"Wire payload failed A2A classification: {capture['classification_error']}"
        )
        assert capture["payload_type"] == "Task"
        assert payload["id"] == "t-789"
        assert payload["contextId"] == "c-789", f"Expected camelCase 'contextId', got payload keys {sorted(payload)}"
        assert "context_id" not in payload, "snake_case 'context_id' must not appear on the A2A wire"

    def test_classifier_rejects_snake_case_wire_payload(self):
        """The capture classifier must fail loudly on a snake_case payload.

        Guards the test infrastructure itself: a future snake_case regression can
        never be silently absorbed as an 'unknown' payload type.
        """
        with pytest.raises(SnakeCaseWireViolation):
            classify_a2a_payload({"task_id": "t-1", "context_id": "c-1", "status": {"state": "submitted"}})

    def test_classifier_accepts_camelcase_task_status_update_event(self):
        """The camelCase TaskStatusUpdateEvent shape classifies without error."""
        result = classify_a2a_payload({"taskId": "t-1", "contextId": "c-1", "status": {"state": "submitted"}})
        assert result == "TaskStatusUpdateEvent"

    def test_classifier_accepts_camelcase_task(self):
        """The camelCase Task shape classifies as Task."""
        result = classify_a2a_payload({"id": "t-1", "contextId": "c-1", "status": {"state": "completed"}})
        assert result == "Task"
