"""Wire coverage for create_media_buy callback URL validation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from src.core.schemas import CreateMediaBuyRequest
from tests.harness.media_buy_create import MediaBuyCreateEnv
from tests.harness.transport import Transport
from tests.helpers import assert_envelope_shape
from tests.helpers.secret_scrub import serialize_wire_error

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


@pytest.mark.parametrize("transport", [Transport.MCP, Transport.REST])
def test_private_callback_is_rejected_on_wire_before_workflow_writes(integration_db, transport):
    """MCP and REST reject a private callback before creating durable workflow state.

    The A2A protocol-level configuration has its own earlier boundary and is
    covered by ``test_message_send_rejects_private_push_notification_url_before_storage``.
    """
    now = datetime.now(UTC)
    request = CreateMediaBuyRequest(
        brand={"domain": "callback-validation.example"},
        packages=[
            {
                "product_id": "prod_1",
                "budget": 5000.0,
                "pricing_option_id": "cpm_usd_fixed",
            }
        ],
        start_time=(now + timedelta(days=1)).isoformat(),
        end_time=(now + timedelta(days=8)).isoformat(),
        idempotency_key=f"callback-test-{uuid.uuid4().hex}",
    )
    rejected_url = "https://127.0.0.1/internal-callback"

    with MediaBuyCreateEnv() as env:
        env.setup_media_buy_data()
        arguments = request.model_dump(mode="json", exclude_none=True)
        arguments["push_notification_config"] = {"url": rejected_url}

        result = env.call_via(transport, **arguments)

        assert result.is_error
        assert_envelope_shape(
            result.wire_error_envelope,
            "VALIDATION_ERROR",
            recovery="correctable",
        )
        for error in (
            result.wire_error_envelope["adcp_error"],
            result.wire_error_envelope["errors"][0],
        ):
            assert error["message"] == "Push notification URL must resolve to a public HTTPS endpoint"
            assert error["field"] == "push_notification_config.url"
        assert rejected_url not in serialize_wire_error(result.wire_error_envelope)
        env.mock["context_mgr"].assert_not_called()
