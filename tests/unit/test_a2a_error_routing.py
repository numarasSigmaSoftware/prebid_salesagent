"""A2A error routing: typed failures use Tasks; internal crashes use JSON-RPC.

Pinned AdCP 3.1.1, ``docs/building/operating/transport-errors.mdx`` "Layer
Separation": typed application failures belong in a failed Task carrying the
two-layer AdCP envelope, while an untyped internal crash belongs in the
transport channel as a sanitized JSON-RPC error.

Grading: ungraded — A2A transport mechanic, unit-graded here. No conformance
storyboard under ``dist/compliance/3.1.1/`` exercises A2A failed-Task routing.

These tests pin both sides: typed ``AdCPError`` retains its Task envelope;
unexpected ``Exception`` cannot become retryable ``SERVICE_UNAVAILABLE`` or
expose its message on the transport error.
"""

from unittest.mock import MagicMock, patch

import pytest
from a2a.types import InternalError, InvalidRequestError, SendMessageRequest, Task, TaskState

from src.a2a_server.adcp_a2a_server import AdCPRequestHandler
from src.core.exceptions import AdCPValidationError
from tests.a2a_helpers import (
    extract_processing_error_envelope,
    make_a2a_context,
    make_mock_a2a_identity,
    make_nl_send_message_request,
)
from tests.helpers import assert_envelope_shape
from tests.utils.a2a_helpers import create_a2a_message_with_skill

_MOCK_IDENTITY = make_mock_a2a_identity()
_make_nl_request = make_nl_send_message_request


def _make_handler() -> tuple[AdCPRequestHandler, object]:
    """Handler + authenticated call context for driving on_message_send."""
    handler = AdCPRequestHandler()
    handler._get_auth_token = MagicMock(return_value="test-token")
    ctx = make_a2a_context(auth_token="test-token", headers={"host": "test.example.com"})
    return handler, ctx


@pytest.mark.asyncio
async def test_untyped_processing_failure_raises_sanitized_internal_error():
    """An unexpected processing crash is sanitized onto the JSON-RPC path."""
    handler, ctx = _make_handler()
    params = _make_nl_request("Show me available products in the catalog")

    with patch("src.core.resolved_identity.resolve_identity", return_value=_MOCK_IDENTITY):
        with patch(
            "src.a2a_server.adcp_a2a_server.core_get_products_tool",
            side_effect=RuntimeError("adapter exploded: secret-canary"),
        ):
            with pytest.raises(InternalError) as exc_info:
                await handler.on_message_send(params, context=ctx)

    assert exc_info.value.message == "Internal server error"
    assert exc_info.value.data is None
    assert "adapter exploded" not in str(exc_info.value)
    assert not handler.tasks, "a transport-rejected request must not leave a lifecycle Task"


@pytest.mark.asyncio
async def test_typed_adcp_error_keeps_its_own_wire_code_on_failed_task():
    """A typed AdCPError escaping to the outer handler keeps its own wire code.

    The envelope must carry the AdCPError's code (here ``VALIDATION_ERROR``),
    not a blanket ``INTERNAL_ERROR`` — ``_build_error_envelope`` passes typed
    errors through ``normalize_to_adcp_error`` unchanged.
    """
    handler, ctx = _make_handler()
    params = _make_nl_request("Show me available products in the catalog")

    with patch("src.core.resolved_identity.resolve_identity", return_value=_MOCK_IDENTITY):
        with patch(
            "src.a2a_server.adcp_a2a_server.core_get_products_tool",
            side_effect=AdCPValidationError("brief must not be empty"),
        ):
            result = await handler.on_message_send(params, context=ctx)

    assert isinstance(result, Task), f"expected a returned Task, got {type(result).__name__}"
    assert result.status.state == TaskState.TASK_STATE_FAILED, (
        f"expected TASK_STATE_FAILED, got {result.status.state!r}"
    )
    assert_envelope_shape(
        extract_processing_error_envelope(result),
        "VALIDATION_ERROR",
        recovery="correctable",
        message_substr="brief must not be empty",
    )


@pytest.mark.asyncio
async def test_genuine_transport_fault_still_raises_json_rpc_error():
    """A transport-protocol fault must still surface as a JSON-RPC error.

    Missing authentication for a non-discovery skill is a transport-layer
    fault (the request cannot be routed at all), so ``on_message_send``
    re-raises the ``A2AError`` (here ``InvalidRequestError``) onto the
    JSON-RPC layer instead of returning a failed Task.
    """
    handler = AdCPRequestHandler()
    # No auth token at all — create_media_buy is a non-discovery skill.
    ctx = make_a2a_context(auth_token=None, headers={"host": "test.example.com"})
    message = create_a2a_message_with_skill("create_media_buy", {"product_ids": ["prod_1"]})
    params = SendMessageRequest(message=message)

    with pytest.raises(InvalidRequestError) as exc_info:
        await handler.on_message_send(params, context=ctx)

    assert "authentication" in str(exc_info.value).lower(), (
        f"transport fault should name the missing authentication; got: {exc_info.value}"
    )
