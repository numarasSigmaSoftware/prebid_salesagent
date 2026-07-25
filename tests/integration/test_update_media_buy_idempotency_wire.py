"""Durable update-media-buy idempotency through every exposed wire boundary."""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from src.core.schemas import UpdateMediaBuyRequest
from src.core.utils import utc_flight_start
from tests.harness.media_buy_dual import MediaBuyDualEnv
from tests.harness.transport import Transport
from tests.helpers import assert_envelope_shape

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

_WIRE_TRANSPORTS = [Transport.MCP, Transport.A2A, Transport.REST]


def _seed_update_env(env: MediaBuyDualEnv) -> str:
    from tests.bdd.conftest import _setup_existing_media_buy

    tenant, principal, product, _ = env.setup_media_buy_data()
    context: dict = {}
    _setup_existing_media_buy(context, env, tenant, principal, product)
    media_buy_id = context["existing_media_buy"].media_buy_id
    env._seeded_media_buy_id = media_buy_id
    return media_buy_id


@pytest.mark.parametrize("transport", _WIRE_TRANSPORTS)
def test_update_wire_retry_replays_without_second_adapter_mutation(integration_db, transport: Transport) -> None:
    key = f"update-wire-{transport.value}-{uuid.uuid4().hex}"

    with MediaBuyDualEnv(
        tenant_id=f"update_wire_{transport.value}",
        principal_id=f"agent_update_wire_{transport.value}",
    ) as env:
        media_buy_id = _seed_update_env(env)
        first_request = UpdateMediaBuyRequest(
            media_buy_id=media_buy_id,
            paused=True,
            idempotency_key=key,
            context={"correlation_id": "original"},
        )
        retry_request = first_request.model_copy(update={"context": {"correlation_id": "retry"}})
        adapter_update = env.mock["update_adapter"].return_value.update_media_buy

        first = env.call_via(transport, req=first_request)
        adapter_update.assert_called_once_with(
            media_buy_id=media_buy_id,
            action="pause_media_buy",
            package_id=None,
            budget=None,
            today=utc_flight_start(date.today()),
        )
        second = env.call_via(transport, req=retry_request)

    assert first.is_success, first.error
    assert second.is_success, second.error
    assert first.wire_response is not None
    assert second.wire_response is not None
    assert first.wire_response.get("replayed", False) is False
    assert first.wire_response["context"] == {"correlation_id": "original"}
    assert second.wire_response["replayed"] is True
    assert second.wire_response["context"] == {"correlation_id": "retry"}
    assert adapter_update.call_count == 1


@pytest.mark.parametrize("transport", _WIRE_TRANSPORTS)
def test_update_wire_conflict_has_exact_envelope(integration_db, transport: Transport) -> None:
    key = f"update-conflict-wire-{transport.value}-{uuid.uuid4().hex}"

    with MediaBuyDualEnv(
        tenant_id=f"update_conflict_wire_{transport.value}",
        principal_id=f"agent_update_conflict_wire_{transport.value}",
    ) as env:
        media_buy_id = _seed_update_env(env)
        first = env.call_via(
            transport,
            req=UpdateMediaBuyRequest(media_buy_id=media_buy_id, paused=True, idempotency_key=key),
        )
        second = env.call_via(
            transport,
            req=UpdateMediaBuyRequest(media_buy_id=media_buy_id, paused=False, idempotency_key=key),
        )

    assert first.is_success, first.error
    assert second.is_error
    assert_envelope_shape(second.wire_error_envelope, "IDEMPOTENCY_CONFLICT", recovery="correctable")
