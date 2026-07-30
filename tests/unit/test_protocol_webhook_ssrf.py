"""SSRF gate for protocol push / reporting webhook URLs.

Pins that ProtocolWebhookService refuses unsafe URLs before any outbound POST,
mirrors application-level WebhookURLValidator usage in webhook_delivery, and
covers registration wiring: create_media_buy, update_media_buy, sync_creatives,
A2A message/send, and A2A set_push_notification_config handler.

Wire-level VALIDATION_ERROR / recovery=correctable + suggestion for
create_media_buy and sync_creatives is graded by transport-blind BDD scenarios
(BR-UC-002-ext-webhook-ssrf, BR-UC-006-ext-webhook-ssrf). A2A-native push-config
endpoints translate the same registration gate to InvalidParamsError with the
AdCP VALIDATION_ERROR envelope in ``data`` — pinned below.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests
from a2a.types import (
    InvalidParamsError,
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    TaskPushNotificationConfig,
)
from adcp.types import ReportingWebhook

from src.a2a_server.adcp_a2a_server import AdCPRequestHandler, _reject_unsafe_a2a_webhook_url
from src.core.database.models import DELIVERY_TASK_TYPE, PushNotificationConfig
from src.core.exceptions import AdCPValidationError
from src.core.resolved_identity import ResolvedIdentity
from src.core.schemas import CreateMediaBuyRequest
from src.core.testing_hooks import AdCPTestContext
from src.core.tools.creatives._sync import _sync_creatives_impl
from src.core.tools.media_buy_create import _create_media_buy_impl
from src.core.webhook_validator import WEBHOOK_SSRF_SUGGESTION_DEV, reject_unsafe_webhook_registration_url
from src.services.protocol_webhook_service import ProtocolWebhookService, _safe_delivery_error_message
from tests.factories.principal import PrincipalFactory
from tests.helpers import assert_envelope_shape
from tests.helpers.adcp_factories import create_test_media_buy_request_dict, valid_reporting_webhook

_METADATA_URL = "http://169.254.169.254/latest/meta-data/"


def _config(url: str) -> PushNotificationConfig:
    return PushNotificationConfig(
        id="pnc-ssrf-test",
        tenant_id="t1",
        principal_id="p1",
        url=url,
        authentication_type=None,
        authentication_token=None,
        is_active=True,
    )


def _reporting_webhook(url: str) -> ReportingWebhook:
    return ReportingWebhook.model_validate(valid_reporting_webhook(url))


def _identity() -> ResolvedIdentity:
    return PrincipalFactory.make_identity(
        principal_id="principal_1",
        tenant_id="test_tenant",
        auth_token="test-token",
        protocol="mcp",
        tenant={"tenant_id": "test_tenant", "human_review_required": False, "auto_create_media_buys": True},
        testing_context=AdCPTestContext(dry_run=False, test_session_id="test-session"),
    )


def _minimal_create_request(**overrides):
    data = create_test_media_buy_request_dict(
        product_ids=["prod_1"],
        total_budget=5000.0,
        pricing_option_id="cpm_usd_fixed",
        idempotency_key="unit-ssrf-create-key-0001",
        **overrides,
    )
    return CreateMediaBuyRequest(**data)


@pytest.mark.asyncio
async def test_send_notification_rejects_metadata_url_without_post() -> None:
    """Cloud metadata URL must fail closed before requests.Session.post."""
    service = ProtocolWebhookService()
    with patch.object(service._session, "post", autospec=True) as mock_post:
        sent = await service.send_notification(
            _config(_METADATA_URL),
            payload={"task_id": "t1", "status": "completed"},
            metadata={"task_type": "create_media_buy"},
        )
    assert sent is False
    mock_post.assert_not_called()


@pytest.mark.asyncio
async def test_send_notification_rejects_localhost_without_post(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production send path must reject localhost (ADCP_TESTING off)."""
    monkeypatch.delenv("ADCP_TESTING", raising=False)
    service = ProtocolWebhookService()
    with patch.object(service._session, "post", autospec=True) as mock_post:
        sent = await service.send_notification(
            _config("http://localhost:9999/webhook"),
            payload={"task_id": "t1", "status": "completed"},
            metadata={"task_type": "create_media_buy"},
        )
    assert sent is False
    mock_post.assert_not_called()


@pytest.mark.asyncio
async def test_send_notification_posts_when_url_is_public() -> None:
    """Safe public URL proceeds to POST (validator + session both exercised)."""
    service = ProtocolWebhookService()
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status = MagicMock()

    with (
        patch(
            "src.core.webhook_validator.WebhookURLValidator.validate_outbound_webhook_url",
            return_value=(True, ""),
        ),
        patch.object(service._session, "post", return_value=response) as mock_post,
        patch(
            "src.services.protocol_webhook_service.extract_webhook_result_data",
            return_value=None,
        ),
        patch(
            "src.services.protocol_webhook_service.get_audit_logger",
            return_value=MagicMock(),
        ),
    ):
        sent = await service.send_notification(
            _config("https://buyer.example.com/hooks/adcp"),
            payload={"task_id": "t1", "status": "completed"},
            metadata={"task_type": "create_media_buy", "tenant_id": "t1"},
        )

    assert sent is True
    mock_post.assert_called_once_with(
        "https://buyer.example.com/hooks/adcp",
        json={"task_id": "t1", "status": "completed"},
        headers={"Content-Type": "application/json", "User-Agent": "AdCP-Sales-Agent/1.0"},
        timeout=10.0,
        allow_redirects=False,
    )


@pytest.mark.asyncio
def _loggable_metadata() -> dict[str, str]:
    """Metadata that makes _delivery_log_context() return a real context, not
    None: task_type must be in _LOGGABLE_DELIVERY_TASK_TYPES, and tenant_id/
    principal_id/media_buy_id must ALL be set. Without this,
    _write_delivery_log() no-ops unconditionally on its `if context is None:
    return` (protocol_webhook_service.py) -- a redirect test seeded with
    non-loggable metadata (e.g. bare {"task_type": "create_media_buy",
    "tenant_id": "t1"}) would only ever prove `sent is False` and that the
    POST wasn't retried; the failed-state persistence and audit warning
    would go completely unverified and could be deleted with the test
    staying green."""
    return {
        "task_type": DELIVERY_TASK_TYPE,
        "tenant_id": "tenant-1",
        "principal_id": "principal-1",
        "media_buy_id": "media-buy-1",
    }


def _assert_pending_then_failed_on_non_2xx(
    mock_write_log: MagicMock,
    audit_logger_mock: MagicMock,
    *,
    status_code: int,
    url: str,
) -> None:
    """Assert _write_delivery_log was called exactly twice against the SAME
    delivery-log row -- once to reserve the "pending" event identity before
    the POST, once to record the actual non-2xx outcome as "failed" -- plus
    a matching audit warning. Shared by every non-2xx-response test in this
    module: the field-by-field verification is identical regardless of
    which status code or URL triggered the failure.
    """
    assert mock_write_log.call_count == 2
    pending_call, failed_call = mock_write_log.call_args_list

    assert pending_call.kwargs["status"] == "pending"
    assert pending_call.kwargs["attempt_count"] == 0

    assert failed_call.kwargs["status"] == "failed"
    assert failed_call.kwargs["attempt_count"] == 1
    assert failed_call.kwargs["http_status_code"] == status_code
    assert failed_call.kwargs["error_message"] == _safe_delivery_error_message(
        url, reason=f"HTTP {status_code} response"
    )
    assert failed_call.kwargs["completed_at"] is not None
    assert failed_call.kwargs["response_time_ms"] is not None

    # Both writes persist the SAME delivery-log row (identity, not just
    # equality -- _DeliveryLogContext carries a randomly-generated log_id) --
    # a pending -> failed transition, not two unrelated rows.
    assert pending_call.kwargs["context"] is failed_call.kwargs["context"]

    audit_logger_mock.log_warning.assert_called_once_with(
        f"{DELIVERY_TASK_TYPE} webhook failed with non-2xx response {status_code}"
    )


@pytest.mark.asyncio
async def test_send_notification_does_not_follow_redirect_to_metadata() -> None:
    """302 to link-local metadata must not be followed (open-redirect SSRF) AND
    must be recorded as a failed delivery -- not silently dropped, not
    recorded as success.

    Uses a REAL requests.Response, not a MagicMock with a fabricated
    raise_for_status side_effect: requests.Response.raise_for_status() does
    NOT raise for 3xx (verified against the real class, not assumed) -- a
    mock that forces it to raise here masks that this exact response used to
    fall through to the success branch below, since allow_redirects=False
    means we only ever see the 302 itself, never a response from wherever it
    points.
    """
    service = ProtocolWebhookService()
    redirect = requests.Response()
    redirect.status_code = 302
    redirect.headers["Location"] = _METADATA_URL
    audit_logger_mock = MagicMock()
    url = "https://buyer.example.com/hooks/adcp"

    with (
        patch(
            "src.core.webhook_validator.WebhookURLValidator.validate_outbound_webhook_url",
            return_value=(True, ""),
        ),
        patch.object(service._session, "post", return_value=redirect) as mock_post,
        patch(
            "src.services.protocol_webhook_service.extract_webhook_result_data",
            return_value=None,
        ),
        patch(
            "src.services.protocol_webhook_service.get_audit_logger",
            return_value=audit_logger_mock,
        ),
        patch.object(service, "_write_delivery_log") as mock_write_log,
    ):
        sent = await service.send_notification(
            _config(url),
            payload={"task_id": "t1", "status": "completed"},
            metadata=_loggable_metadata(),
        )

    assert sent is False
    # Exactly one attempt: an un-followed redirect is a permanent failure
    # (like 4xx), not something worth retrying against the same URL -- it
    # would just get the same redirect again.
    mock_post.assert_called_once_with(
        url,
        json={"task_id": "t1", "status": "completed"},
        headers={"Content-Type": "application/json", "User-Agent": "AdCP-Sales-Agent/1.0"},
        timeout=10.0,
        allow_redirects=False,
    )
    _assert_pending_then_failed_on_non_2xx(mock_write_log, audit_logger_mock, status_code=302, url=url)


@pytest.mark.asyncio
async def test_send_notification_records_failure_on_ordinary_redirect() -> None:
    """A redirect from an ordinary, non-SSRF-flagged target must ALSO be
    recorded as a failed delivery, never success -- this is a general
    correctness property of _send_with_retry_and_logging (raise_for_status()
    never raises for any 3xx), not something that only matters for
    metadata/private-IP targets specifically."""
    service = ProtocolWebhookService()
    redirect = requests.Response()
    redirect.status_code = 307
    redirect.headers["Location"] = "https://buyer.example.com/hooks/adcp-new"
    audit_logger_mock = MagicMock()
    url = "https://buyer.example.com/hooks/adcp"

    with (
        patch(
            "src.core.webhook_validator.WebhookURLValidator.validate_outbound_webhook_url",
            return_value=(True, ""),
        ),
        patch.object(service._session, "post", return_value=redirect) as mock_post,
        patch(
            "src.services.protocol_webhook_service.extract_webhook_result_data",
            return_value=None,
        ),
        patch(
            "src.services.protocol_webhook_service.get_audit_logger",
            return_value=audit_logger_mock,
        ),
        patch.object(service, "_write_delivery_log") as mock_write_log,
    ):
        sent = await service.send_notification(
            _config(url),
            payload={"task_id": "t1", "status": "completed"},
            metadata=_loggable_metadata(),
        )

    assert sent is False
    mock_post.assert_called_once_with(
        url,
        json={"task_id": "t1", "status": "completed"},
        headers={"Content-Type": "application/json", "User-Agent": "AdCP-Sales-Agent/1.0"},
        timeout=10.0,
        allow_redirects=False,
    )
    _assert_pending_then_failed_on_non_2xx(mock_write_log, audit_logger_mock, status_code=307, url=url)


def test_reject_unsafe_webhook_registration_url_raises_validation_error() -> None:
    with pytest.raises(AdCPValidationError) as exc_info:
        reject_unsafe_webhook_registration_url(
            "http://metadata.google.internal/computeMetadata/v1/",
            field="reporting_webhook.url",
        )
    assert exc_info.value.field == "reporting_webhook.url"
    assert "Invalid reporting_webhook.url" in exc_info.value.message
    assert exc_info.value.suggestion == WEBHOOK_SSRF_SUGGESTION_DEV
    assert exc_info.value.recovery == "correctable"


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_reject_unsafe_webhook_registration_url_noop_on_blank(blank: str | None) -> None:
    """Blank / missing URL is not a rejection — callers extract-then-call unconditionally."""
    reject_unsafe_webhook_registration_url(blank, field="push_notification_config.url")


def test_sanitize_webhook_url_for_log_strips_credentials_query_and_fragment() -> None:
    from src.core.webhook_validator import (
        UNPARSEABLE_WEBHOOK_URL_FOR_LOG,
        sanitize_webhook_url_for_log,
        webhook_url_for_log,
    )

    dirty = "https://user:pass@buyer.example.com:8443/hook?token=abc#frag"
    assert sanitize_webhook_url_for_log(dirty) == "https://buyer.example.com/hook"
    assert webhook_url_for_log(dirty) == "https://buyer.example.com/hook"
    assert sanitize_webhook_url_for_log(None) is None
    assert sanitize_webhook_url_for_log("not-a-url") is None
    assert webhook_url_for_log(None) == UNPARSEABLE_WEBHOOK_URL_FOR_LOG
    assert webhook_url_for_log("not-a-url") == UNPARSEABLE_WEBHOOK_URL_FOR_LOG


def test_reject_unsafe_webhook_registration_url_allows_public() -> None:
    # Registration skips DNS — fixture hostnames must not NXDOMAIN-fail.
    reject_unsafe_webhook_registration_url("https://buyer.example.com/hook", field="push_notification_config.url")


def test_reject_unsafe_webhook_registration_url_allows_unresolvable_public_hostname() -> None:
    """Registration gate must not require DNS (BDD fixture hosts)."""
    reject_unsafe_webhook_registration_url(
        "https://nonexistent-buyer-ssrf-fixture.invalid/hook",
        field="reporting_webhook.url",
    )


def test_push_notification_config_repo_upsert_rejects_ssrf_url() -> None:
    """Repository upsert is a second registration gate (A2A set_push_notification_config)."""
    from src.core.database.repositories.push_notification_config import PushNotificationConfigRepository

    repo = PushNotificationConfigRepository(MagicMock(), "t1")
    with pytest.raises(ValueError, match="Invalid webhook URL"):
        repo.upsert(
            config_id="pnc_bad",
            principal_id="p1",
            url=_METADATA_URL,
            authentication_type=None,
            authentication_token=None,
            validation_token=None,
        )


@pytest.mark.asyncio
async def test_create_media_buy_rejects_reporting_webhook_anyurl() -> None:
    """Registration gate must run for real ReportingWebhook.url (AnyUrl, not str)."""
    req = _minimal_create_request(reporting_webhook=_reporting_webhook(_METADATA_URL))
    with pytest.raises(AdCPValidationError) as exc_info:
        await _create_media_buy_impl(req, identity=_identity())
    assert exc_info.value.field == "reporting_webhook.url"
    assert "Invalid reporting_webhook.url" in exc_info.value.message


@pytest.mark.asyncio
async def test_create_media_buy_rejects_push_config_before_workflow() -> None:
    """PNC SSRF must run before workflow metadata write (wiring + ordering)."""
    req = _minimal_create_request()
    mock_ctx = MagicMock()
    with (
        patch("src.core.tools.media_buy_create.get_context_manager", return_value=mock_ctx),
        pytest.raises(AdCPValidationError) as exc_info,
    ):
        await _create_media_buy_impl(
            req,
            push_notification_config={"url": _METADATA_URL},
            identity=_identity(),
        )
    assert exc_info.value.field == "push_notification_config.url"
    mock_ctx.create_workflow_step.assert_not_called()
    mock_ctx.create_context.assert_not_called()


def test_sync_creatives_rejects_unsafe_push_config_url() -> None:
    """sync_creatives must reject metadata URL at registration before DB work."""
    with pytest.raises(AdCPValidationError) as exc_info:
        _sync_creatives_impl(
            creatives=[],
            push_notification_config={"url": _METADATA_URL},
            identity=_identity(),
        )
    assert exc_info.value.field == "push_notification_config.url"


def test_reject_unsafe_a2a_webhook_url_rejects_metadata() -> None:
    """A2A registration helper maps SSRF to InvalidParamsError + AdCP envelope in data."""
    with pytest.raises(InvalidParamsError, match="Invalid push_notification_config.url") as exc_info:
        _reject_unsafe_a2a_webhook_url(_METADATA_URL)
    assert_envelope_shape(exc_info.value.data, "VALIDATION_ERROR", recovery="correctable")
    assert exc_info.value.data["errors"][0].get("suggestion")


@pytest.mark.asyncio
async def test_a2a_message_send_rejects_unsafe_push_config_url() -> None:
    """message/send must reject metadata URL before stash."""
    handler = AdCPRequestHandler()
    text_part = Part()
    text_part.text = "list products"
    message = Message(message_id="m-ssrf", role=Role.ROLE_USER, parts=[text_part])
    push = TaskPushNotificationConfig(url=_METADATA_URL)
    params = SendMessageRequest(
        message=message,
        configuration=SendMessageConfiguration(task_push_notification_config=push),
    )

    with pytest.raises(InvalidParamsError, match="Invalid push_notification_config.url") as exc_info:
        await handler.on_message_send(params, context=MagicMock())

    assert_envelope_shape(exc_info.value.data, "VALIDATION_ERROR", recovery="correctable")
    assert handler._task_push_configs == {}


@pytest.mark.asyncio
async def test_a2a_set_push_handler_rejects_metadata_url() -> None:
    """Handler on_create_task_push_notification_config must reject before upsert."""
    handler = AdCPRequestHandler()
    identity = _identity()
    tool_context = MagicMock()
    tool_context.tenant_id = identity.tenant_id
    tool_context.principal_id = identity.principal_id
    params = TaskPushNotificationConfig(url=_METADATA_URL, task_id="task-1", id="pnc-1")

    with (
        patch.object(handler, "_get_auth_token", return_value="tok"),
        patch.object(handler, "_resolve_a2a_identity", return_value=identity),
        patch.object(handler, "_make_tool_context", return_value=tool_context),
        patch("src.a2a_server.adcp_a2a_server.PushNotificationConfigUoW") as mock_uow,
        pytest.raises(InvalidParamsError, match="Invalid push_notification_config.url") as exc_info,
    ):
        await handler.on_create_task_push_notification_config(params, context=MagicMock())

    assert_envelope_shape(exc_info.value.data, "VALIDATION_ERROR", recovery="correctable")
    mock_uow.assert_not_called()


def test_update_media_buy_rejects_reporting_webhook_url() -> None:
    """update_media_buy must reject an unsafe reporting_webhook.url at registration,
    mirroring create_media_buy -- this is the same capability on the update path.
    """
    from tests.harness.media_buy_update import MediaBuyUpdateEnv

    with MediaBuyUpdateEnv() as env:
        env.set_media_buy(media_buy_id="mb-001", status="active")
        with pytest.raises(AdCPValidationError) as exc_info:
            env.call_impl(
                media_buy_id="mb-001",
                reporting_webhook={
                    "url": _METADATA_URL,
                    "authentication": {"schemes": ["Bearer"], "credentials": "x" * 40},
                    "reporting_frequency": "daily",
                },
            )
    assert exc_info.value.field == "reporting_webhook.url"
    assert "Invalid reporting_webhook.url" in exc_info.value.message


def test_update_media_buy_rejects_push_notification_config_url() -> None:
    """update_media_buy must reject an unsafe push_notification_config.url at
    registration -- the sibling field to reporting_webhook, on the same request,
    validated the same way. This is the fix for the gap where an unsafe
    push_notification_config.url was silently accepted and persisted on update
    while the same URL on reporting_webhook was correctly rejected.
    """
    from tests.harness.media_buy_update import MediaBuyUpdateEnv

    with MediaBuyUpdateEnv() as env:
        env.set_media_buy(media_buy_id="mb-001", status="active")
        with pytest.raises(AdCPValidationError) as exc_info:
            env.call_impl(
                media_buy_id="mb-001",
                push_notification_config={
                    "url": _METADATA_URL,
                    "authentication_type": "bearer",
                    "authentication_token": "x" * 40,
                },
            )
    assert exc_info.value.field == "push_notification_config.url"
    assert "Invalid push_notification_config.url" in exc_info.value.message
