"""Pause-on-create is rejected consistently before any reservation or mutation."""

from unittest.mock import patch

import pytest

from tests.helpers.envelope_assertions import assert_envelope_shape


@pytest.mark.asyncio
async def test_mcp_pause_on_create_rejects_before_reservation() -> None:
    """MCP emits the exact unsupported-feature envelope without touching persistence."""
    from src.core.schemas import ContextObject
    from src.core.tool_error_logging import AdCPToolError, with_error_logging
    from src.core.tools.media_buy_create import create_media_buy
    from tests.factories.principal import PrincipalFactory

    context = ContextObject(test_case="pause-on-create")
    identity = PrincipalFactory.make_identity(
        tenant_id="tenant-1",
        principal_id="principal-1",
        tenant={"tenant_id": "tenant-1"},
        protocol="mcp",
    )
    with (
        patch("src.core.tools.media_buy_create.require_identity", return_value=identity),
        patch("src.core.database.repositories.MediaBuyUoW") as mock_uow,
        pytest.raises(AdCPToolError) as raised,
    ):
        await with_error_logging(create_media_buy)(
            brand={"domain": "testbrand.com"},
            packages=[],
            start_time="2026-01-01T00:00:00Z",
            end_time="2026-02-01T00:00:00Z",
            idempotency_key="pause-create-mcp-0001",
            paused=True,
            context=context,
            ctx=None,
        )

    assert_envelope_shape(
        raised.value,
        "UNSUPPORTED_FEATURE",
        recovery="correctable",
        check_mcp_tool_error=True,
    )
    expected = "Create the media buy, then call update_media_buy with paused=true."
    assert raised.value.envelope["adcp_error"]["suggestion"] == expected
    assert raised.value.envelope["errors"][0]["suggestion"] == expected
    assert raised.value.envelope["context"] == {"test_case": "pause-on-create"}
    mock_uow.assert_not_called()
