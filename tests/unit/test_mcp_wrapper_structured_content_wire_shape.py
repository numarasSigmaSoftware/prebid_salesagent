"""Regression coverage: MCP wrappers must serialize responses via model_dump().

``fastmcp.tools.tool.ToolResult.__init__`` passes ``structured_content`` through
``pydantic_core.to_jsonable_python()``, which serializes the raw pydantic-core
schema directly and does NOT know about ``AdCPBaseModel``'s ``exclude_none=True``
default. A2A and REST call ``.model_dump()`` explicitly somewhere along their
dispatch path, so they correctly omit unset optional fields; a wrapper that
passes the response object straight through to ``ToolResult`` leaks every
unset optional field as an explicit ``null`` on MCP only.

Each test below mocks the wrapper's ``_impl`` to return a real, minimal
response instance with optional fields deliberately left unset, then asserts
the MCP ``ToolResult.structured_content`` omits them -- matching what
A2A/REST already produce via ``response.model_dump(mode="json")``.

Not covered here: ``create_media_buy`` / ``update_media_buy``. Both return a
``TaskResultEnvelope`` subclass (``CreateMediaBuyResult`` / ``UpdateMediaBuyResult``)
whose ``@model_serializer(mode="wrap")`` is baked into the model's core schema
and internally calls ``.model_dump()`` on the nested domain response either
way -- so passing the raw object to ``ToolResult`` already produces the same
output as calling ``.model_dump()`` explicitly. See their docstrings in
``src/core/schemas/_base.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


class TestAccountsWrappersOmitUnsetOptionals:
    """list_accounts / sync_accounts MCP wrappers (src/core/tools/accounts.py)."""

    @pytest.mark.asyncio
    async def test_list_accounts_omits_unset_optionals(self):
        from src.core.schemas.account import Account, ListAccountsResponse
        from src.core.tools.accounts import list_accounts

        response = ListAccountsResponse(accounts=[Account(account_id="acc_1", name="Test", status="active")])
        with patch("src.core.tools.accounts._list_accounts_impl", return_value=response):
            result = await list_accounts(ctx=None)

        assert "errors" not in result.structured_content
        assert "pagination" not in result.structured_content
        assert "context" not in result.structured_content

    @pytest.mark.asyncio
    async def test_sync_accounts_omits_unset_optionals(self):
        from src.core.schemas.account import SyncAccountsResponse
        from src.core.tools.accounts import sync_accounts

        response = SyncAccountsResponse(accounts=[])
        with patch("src.core.tools.accounts._sync_accounts_impl", new_callable=AsyncMock, return_value=response):
            result = await sync_accounts(ctx=None)

        assert "dry_run" not in result.structured_content
        assert "context" not in result.structured_content


class TestCapabilitiesWrapperOmitsUnsetOptionals:
    """get_adcp_capabilities MCP wrapper (src/core/tools/capabilities.py)."""

    @pytest.mark.asyncio
    async def test_get_adcp_capabilities_omits_unset_optionals(self):
        from adcp.types import GetAdcpCapabilitiesResponse
        from adcp.types.generated_poc.protocol.get_adcp_capabilities_response import (
            Adcp,
            Idempotency,
            MajorVersion,
            SupportedProtocol,
        )

        from src.core.tools.capabilities import get_adcp_capabilities

        response = GetAdcpCapabilitiesResponse(
            adcp=Adcp(
                major_versions=[MajorVersion(1)],
                idempotency=Idempotency(supported=True, replay_ttl_seconds=86400),
            ),
            supported_protocols=[SupportedProtocol.media_buy],
        )
        with patch("src.core.tools.capabilities._get_adcp_capabilities_impl", return_value=response):
            result = await get_adcp_capabilities(ctx=None)

        # media_buy, signals, governance etc. are all unset on this minimal response.
        assert "media_buy" not in result.structured_content
        assert "signals" not in result.structured_content
        assert "errors" not in result.structured_content


class TestCreativeFormatsWrapperOmitsUnsetOptionals:
    """list_creative_formats MCP wrapper (src/core/tools/creative_formats.py)."""

    @pytest.mark.asyncio
    async def test_list_creative_formats_omits_unset_optionals(self):
        from src.core.schemas import ListCreativeFormatsResponse
        from src.core.tools.creative_formats import list_creative_formats

        response = ListCreativeFormatsResponse(formats=[])
        with patch("src.core.tools.creative_formats._list_creative_formats_impl", return_value=response):
            result = await list_creative_formats(ctx=None)

        assert "pagination" not in result.structured_content
        assert "errors" not in result.structured_content
        assert "source" not in result.structured_content


class TestCreativesListingWrapperOmitsUnsetOptionals:
    """list_creatives MCP wrapper (src/core/tools/creatives/listing.py)."""

    @pytest.mark.asyncio
    async def test_list_creatives_omits_unset_optionals(self):
        from src.core.schemas import ListCreativesResponse
        from src.core.schemas.creative import Pagination, QuerySummary
        from src.core.tools.creatives.listing import list_creatives

        response = ListCreativesResponse(
            query_summary=QuerySummary(), pagination=Pagination(has_more=False), creatives=[]
        )
        with patch("src.core.tools.creatives.listing._list_creatives_impl", return_value=response):
            result = await list_creatives(ctx=None)

        assert "format_summary" not in result.structured_content
        assert "status_summary" not in result.structured_content
        assert "errors" not in result.structured_content


class TestSyncCreativesWrapperOmitsUnsetOptionals:
    """sync_creatives MCP wrapper (src/core/tools/creatives/sync_wrappers.py)."""

    @pytest.mark.asyncio
    async def test_sync_creatives_omits_unset_optionals(self):
        from src.core.schemas import SyncCreativesResponse
        from src.core.tools.creatives.sync_wrappers import sync_creatives

        response = SyncCreativesResponse(creatives=[])
        # Patched where sync_wrappers.py looks it up (imported by name from ._sync),
        # not at its definition site in _sync.py.
        with patch("src.core.tools.creatives.sync_wrappers._sync_creatives_impl", return_value=response):
            result = await sync_creatives(creatives=[], ctx=None)

        assert "dry_run" not in result.structured_content
        assert "context" not in result.structured_content


class TestGetMediaBuysWrapperOmitsUnsetOptionals:
    """get_media_buys MCP wrapper (src/core/tools/media_buy_list.py)."""

    @pytest.mark.asyncio
    async def test_get_media_buys_omits_unset_optionals(self):
        from src.core.schemas._base import GetMediaBuysResponse
        from src.core.tools.media_buy_list import get_media_buys

        response = GetMediaBuysResponse(media_buys=[])
        with patch("src.core.tools.media_buy_list._get_media_buys_impl", return_value=response):
            result = await get_media_buys(ctx=None)

        assert "errors" not in result.structured_content
        assert "context" not in result.structured_content


class TestPerformanceWrapperOmitsUnsetOptionals:
    """update_performance_index MCP wrapper (src/core/tools/performance.py)."""

    @pytest.mark.asyncio
    async def test_update_performance_index_omits_unset_optionals(self):
        from src.core.schemas import UpdatePerformanceIndexResponse
        from src.core.tools.performance import update_performance_index

        response = UpdatePerformanceIndexResponse(status="success", detail="ok")
        with patch("src.core.tools.performance._update_performance_index_impl", return_value=response):
            result = await update_performance_index(media_buy_id="mb_1", performance_data=[], ctx=None)

        assert "context" not in result.structured_content


class TestPropertiesWrapperOmitsUnsetOptionals:
    """list_authorized_properties MCP wrapper (src/core/tools/properties.py)."""

    @pytest.mark.asyncio
    async def test_list_authorized_properties_omits_unset_optionals(self):
        from src.core.schemas import ListAuthorizedPropertiesResponse
        from src.core.tools.properties import list_authorized_properties

        response = ListAuthorizedPropertiesResponse(publisher_domains=[])
        with patch("src.core.tools.properties._list_authorized_properties_impl", return_value=response):
            result = await list_authorized_properties(ctx=None)

        assert "errors" not in result.structured_content
        assert "context" not in result.structured_content
        assert "last_updated" not in result.structured_content


class TestSignalsWrappersOmitUnsetOptionals:
    """get_signals / activate_signal MCP wrappers (src/core/tools/signals.py)."""

    @pytest.mark.asyncio
    async def test_get_signals_omits_unset_optionals(self):
        from src.core.schemas import GetSignalsRequest, GetSignalsResponse
        from src.core.tools.signals import get_signals

        response = GetSignalsResponse(signals=[])
        with patch("src.core.tools.signals._get_signals_impl", new_callable=AsyncMock, return_value=response):
            result = await get_signals(GetSignalsRequest(), context=None)

        assert "errors" not in result.structured_content
        assert "pagination" not in result.structured_content

    @pytest.mark.asyncio
    async def test_activate_signal_omits_unset_optionals(self):
        from src.core.schemas import ActivateSignalResponse
        from src.core.tools.signals import activate_signal

        response = ActivateSignalResponse(signal_id="sig_1")
        with patch("src.core.tools.signals._activate_signal_impl", new_callable=AsyncMock, return_value=response):
            result = await activate_signal(signal_agent_segment_id="sig_1", ctx=None)

        assert "errors" not in result.structured_content
        assert "activation_details" not in result.structured_content
        assert "context" not in result.structured_content
