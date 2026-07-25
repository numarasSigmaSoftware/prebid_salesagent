"""Universal read idempotency uses the same durable reservation flow."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.tools.tool import ToolResult

from src.core.exceptions import AdCPIdempotencyConflictError
from src.services.idempotency_replay import (
    ReservationResult,
    _decode_read_response,
    execute_idempotent_read,
    raise_on_tool_conflict,
)
from tests.factories import PrincipalFactory


@pytest.mark.asyncio
async def test_anonymous_read_reserves_in_read_class_and_completes() -> None:
    identity = PrincipalFactory.make_identity(
        tenant_id="tenant-1",
        tenant={"tenant_id": "tenant-1"},
        principal_id=None,
    )
    response = {"items": []}
    uow = MagicMock()
    uow.__enter__.return_value = uow
    work = AsyncMock(return_value=response)

    with (
        patch(
            "src.services.idempotency_replay.reserve_idempotent",
            return_value=ReservationResult(attempt_id="attempt-1"),
        ) as reserve,
        patch("src.core.database.repositories.uow.IdempotencyUoW", return_value=uow),
        patch("src.services.idempotency_replay.complete_idempotent") as complete,
    ):
        result = await execute_idempotent_read(
            tool_name="get_adcp_capabilities",
            idempotency_key="anonymous-read-key",
            identity=identity,
            raw_wire_payload={"idempotency_key": "anonymous-read-key"},
            response_type=None,
            work=work,
        )

    assert result == response
    assert reserve.call_args.kwargs["principal_id"] is None
    assert reserve.call_args.kwargs["operation_class"] == "read"
    complete.assert_called_once_with(
        uow,
        attempt_id="attempt-1",
        response_model=response,
        protocol_status="completed",
    )


@pytest.mark.asyncio
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
                principal_id=None,
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
