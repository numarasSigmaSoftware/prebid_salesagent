"""Tests for typed access to MCP middleware wire-payload state."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from fastmcp.server.context import Context

from src.core.transport_helpers import get_mcp_raw_wire_payload
from tests.factories.principal import PrincipalFactory


def test_returns_stashed_wire_dict():
    ctx = MagicMock(spec=Context)
    ctx.get_state = AsyncMock(return_value={"idempotency_key": "key-1"})

    payload = asyncio.run(get_mcp_raw_wire_payload(ctx))

    assert payload == {"idempotency_key": "key-1"}
    ctx.get_state.assert_awaited_once_with("raw_wire_payload")


def test_rejects_untyped_mock_state_value():
    ctx = MagicMock(spec=Context)
    ctx.get_state = AsyncMock(return_value=PrincipalFactory.make_identity(protocol="mcp"))

    assert asyncio.run(get_mcp_raw_wire_payload(ctx)) is None
    ctx.get_state.assert_awaited_once_with("raw_wire_payload")


def test_non_mcp_context_has_no_wire_payload():
    assert asyncio.run(get_mcp_raw_wire_payload(None)) is None
