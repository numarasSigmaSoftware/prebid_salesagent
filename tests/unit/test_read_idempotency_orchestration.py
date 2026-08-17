"""Universal read idempotency uses the same durable reservation flow."""

from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.tools.tool import ToolResult

from src.core.exceptions import AdCPAuthRequiredError, AdCPIdempotencyConflictError
from src.services.idempotency_replay import (
    ReservationResult,
    _decode_read_response,
    execute_idempotent_read,
    raise_on_tool_conflict,
)
from tests.factories import PrincipalFactory


@pytest.mark.asyncio
async def test_anonymous_read_with_a_key_is_refused_not_scoped_to_null() -> None:
    """An unauthenticated caller has no scope to replay within, so the key is refused.

    AdCP 3.1.1 security.mdx: keys "are scoped per (authenticated agent,
    account)" and "have no meaning across agents on the same seller". Accepting
    one anonymously used to persist (tenant, NULL, NULL); because the lookup
    index is NULLS NOT DISTINCT that is ONE row-space shared by every anonymous
    caller of the tenant, so a client-chosen key could collide across callers
    and replay one caller's envelope to another.

    Refusing is the only spec-consistent option: a seller that accepts a key
    MUST apply the replay contract, and it cannot be applied without an agent.
    """
    identity = PrincipalFactory.make_identity(
        tenant_id="tenant-1",
        tenant={"tenant_id": "tenant-1"},
        principal_id=None,
    )
    work = AsyncMock()

    with (
        patch("src.services.idempotency_replay.reserve_idempotent") as reserve,
        pytest.raises(AdCPAuthRequiredError, match="scoped to the authenticated agent"),
    ):
        await execute_idempotent_read(
            tool_name="get_adcp_capabilities",
            idempotency_key="anonymous-read-key",
            identity=identity,
            raw_wire_payload={"idempotency_key": "anonymous-read-key"},
            response_type=None,
            work=work,
        )

    # Refused BEFORE any durable row is reserved and before the read runs.
    reserve.assert_not_called()
    work.assert_not_awaited()


async def test_read_replay_never_executes_work() -> None:
    work = AsyncMock()
    with patch(
        "src.services.idempotency_replay.reserve_idempotent",
        return_value=ReservationResult(replay={"items": [], "replayed": True}),
    ):
        result = await execute_idempotent_read(
            tool_name="list_creative_formats",
            idempotency_key="read-replay-key",
            identity=PrincipalFactory.make_identity(
                tenant_id="tenant-1",
                tenant={"tenant_id": "tenant-1"},
                principal_id="principal-1",
            ),
            raw_wire_payload={"idempotency_key": "read-replay-key"},
            response_type=None,
            work=work,
        )

    assert result["replayed"] is True
    work.assert_not_awaited()


def test_typed_read_replay_keeps_text_and_structured_content_identical() -> None:
    original = ToolResult(
        structured_content={
            "products": [{"product_id": "p-1"}],
            "context": {"correlation_id": "original"},
        }
    )
    replay = _decode_read_response(
        {"response": original.model_dump(mode="json")},
        ToolResult,
        {"correlation_id": "retry"},
    )

    assert replay.structured_content == {
        "products": [{"product_id": "p-1"}],
        "context": {"correlation_id": "retry"},
        "replayed": True,
    }
    assert replay.content[0].text == (
        '{"products":[{"product_id":"p-1"}],"context":{"correlation_id":"retry"},"replayed":true}'
    )


def test_same_key_and_hash_cannot_be_reused_across_tools() -> None:
    with pytest.raises(AdCPIdempotencyConflictError, match="different tool"):
        raise_on_tool_conflict("get_products", "list_creatives")


def test_same_tool_replay_is_not_a_tool_conflict() -> None:
    raise_on_tool_conflict("get_products", "get_products")
