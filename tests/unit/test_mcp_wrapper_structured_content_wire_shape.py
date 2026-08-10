"""Regression coverage: MCP wrappers must serialize responses via model_dump().

``fastmcp.tools.tool.ToolResult.__init__`` passes ``structured_content`` through
``pydantic_core.to_jsonable_python()``, which serializes the raw pydantic-core
schema directly and does NOT know about ``AdCPBaseModel``'s ``exclude_none=True``
default. A2A and REST call ``.model_dump()`` explicitly somewhere along their
dispatch path, so they correctly omit unset optional fields; a wrapper that
passes the response object straight through to ``ToolResult`` leaks every
unset optional field as an explicit ``null`` on MCP only.

Each test below mocks the wrapper's ``_impl`` to return a real, minimal
response instance, then asserts ``ToolResult.structured_content`` equals the
FULL expected dict literal -- not just "unwanted key absent". A bare
presence/absence check (``"key" not in structured_content``) passes trivially
against ``{}``, so it cannot tell "the fix omits nulls" apart from "the
wrapper stopped returning data"; it also can't tell apart
``.model_dump(mode="json")`` from a bare ``.model_dump()`` (mode governs
whether AnyUrl/datetime/enum values serialize to JSON-safe primitives, not
whether None fields are excluded, so a dropped ``mode="json"`` would pass a
presence/absence check but fail a full-literal one on any type with such a
field). The literals here were captured from the real ``.model_dump(mode="json")``
output at commit time; if AdCPBaseModel's serialization changes shape,
update the literal, not the assertion style.

Not covered here: ``create_media_buy``, and ``update_media_buy`` when it
returns ``UpdateMediaBuyResult``. Both wrap the domain response in a
``TaskResultEnvelope`` subclass (``CreateMediaBuyResult`` / ``UpdateMediaBuyResult``)
whose ``@model_serializer(mode="wrap")`` is baked into the model's core schema
and internally calls ``.model_dump()`` on the nested domain response either
way -- so passing the raw object to ``ToolResult`` already produces the same
output as calling ``.model_dump()`` explicitly (verified empirically,
including the ``replayed=True`` variant). ``update_media_buy`` returning a
BARE ``UpdateMediaBuySubmitted`` (the manual-approval path, no envelope
wrapper) is a genuine instance of the bug and IS covered below -- that type
does not share the envelope's ``@model_serializer``.
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

        assert result.structured_content == {
            "status": "completed",
            "replayed": False,
            "accounts": [
                {
                    "account_id": "acc_1",
                    "name": "Test",
                    "status": "active",
                    "advertiser": None,
                    "payment_terms": None,
                    "rate_card": None,
                }
            ],
        }

    @pytest.mark.asyncio
    async def test_sync_accounts_omits_unset_optionals(self):
        from src.core.schemas.account import SyncAccountsResponse
        from src.core.tools.accounts import sync_accounts

        response = SyncAccountsResponse(accounts=[])
        with patch("src.core.tools.accounts._sync_accounts_impl", new_callable=AsyncMock, return_value=response):
            result = await sync_accounts(ctx=None)

        assert result.structured_content == {"accounts": [], "status": "completed"}


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

        assert result.structured_content == {
            "status": "completed",
            "replayed": False,
            "adcp": {
                "major_versions": [1],
                "idempotency": {"supported": True, "replay_ttl_seconds": 86400, "account_id_is_opaque": False},
            },
            "supported_protocols": ["media_buy"],
        }


class TestCreativeFormatsWrapperOmitsUnsetOptionals:
    """list_creative_formats MCP wrapper (src/core/tools/creative_formats.py)."""

    @pytest.mark.asyncio
    async def test_list_creative_formats_omits_unset_optionals(self):
        from src.core.schemas import ListCreativeFormatsResponse
        from src.core.tools.creative_formats import list_creative_formats

        response = ListCreativeFormatsResponse(formats=[])
        with patch("src.core.tools.creative_formats._list_creative_formats_impl", return_value=response):
            result = await list_creative_formats(ctx=None)

        assert result.structured_content == {"status": "completed", "replayed": False, "formats": []}


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

        assert result.structured_content == {
            "status": "completed",
            "replayed": False,
            "query_summary": {"filters_applied": []},
            "pagination": {"has_more": False},
            "creatives": [],
        }


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

        assert result.structured_content == {"creatives": [], "status": "completed"}


class TestGetMediaBuysWrapperOmitsUnsetOptionals:
    """get_media_buys MCP wrapper (src/core/tools/media_buy_list.py)."""

    @pytest.mark.asyncio
    async def test_get_media_buys_omits_unset_optionals(self):
        from src.core.schemas._base import GetMediaBuysResponse
        from src.core.tools.media_buy_list import get_media_buys

        response = GetMediaBuysResponse(media_buys=[])
        with patch("src.core.tools.media_buy_list._get_media_buys_impl", return_value=response):
            result = await get_media_buys(ctx=None)

        assert result.structured_content == {"media_buys": []}


class TestPerformanceWrapperOmitsUnsetOptionals:
    """update_performance_index MCP wrapper (src/core/tools/performance.py)."""

    @pytest.mark.asyncio
    async def test_update_performance_index_omits_unset_optionals(self):
        from src.core.schemas import UpdatePerformanceIndexResponse
        from src.core.tools.performance import update_performance_index

        response = UpdatePerformanceIndexResponse(status="success", detail="ok")
        with patch("src.core.tools.performance._update_performance_index_impl", return_value=response):
            result = await update_performance_index(media_buy_id="mb_1", performance_data=[], ctx=None)

        assert result.structured_content == {"status": "success", "detail": "ok"}


class TestPropertiesWrapperOmitsUnsetOptionals:
    """list_authorized_properties MCP wrapper (src/core/tools/properties.py)."""

    @pytest.mark.asyncio
    async def test_list_authorized_properties_omits_unset_optionals(self):
        from src.core.schemas import ListAuthorizedPropertiesResponse
        from src.core.tools.properties import list_authorized_properties

        response = ListAuthorizedPropertiesResponse(publisher_domains=[])
        with patch("src.core.tools.properties._list_authorized_properties_impl", return_value=response):
            result = await list_authorized_properties(ctx=None)

        assert result.structured_content == {"publisher_domains": []}


class TestSignalsWrappersOmitUnsetOptionals:
    """get_signals / activate_signal MCP wrappers (src/core/tools/signals.py).

    Neither tool is currently registered as a live MCP tool in
    src/core/main.py's _register_tool(...) calls -- this pins correct
    behavior for when it is (or for any other caller of these wrapper
    functions), not a claim of current MCP reachability.
    """

    @pytest.mark.asyncio
    async def test_get_signals_omits_unset_optionals(self):
        from src.core.schemas import GetSignalsRequest, GetSignalsResponse
        from src.core.tools.signals import get_signals

        response = GetSignalsResponse(signals=[])
        with patch("src.core.tools.signals._get_signals_impl", new_callable=AsyncMock, return_value=response):
            result = await get_signals(GetSignalsRequest(), context=None)

        assert result.structured_content == {
            "status": "completed",
            "replayed": False,
            "signals": [],
            "cache_scope": "public",
        }

    @pytest.mark.asyncio
    async def test_activate_signal_omits_unset_optionals(self):
        from src.core.schemas import ActivateSignalResponse
        from src.core.tools.signals import activate_signal

        response = ActivateSignalResponse(signal_id="sig_1")
        with patch("src.core.tools.signals._activate_signal_impl", new_callable=AsyncMock, return_value=response):
            result = await activate_signal(signal_agent_segment_id="sig_1", ctx=None)

        assert result.structured_content == {"signal_id": "sig_1"}


class TestGetMediaBuyDeliveryWrapperOmitsUnsetOptionals:
    """get_media_buy_delivery MCP wrapper (src/core/tools/media_buy_delivery.py).

    Registered as a live MCP tool (src/core/main.py _register_tool(get_media_buy_delivery)).
    Was still raw-passing (leaking 20 explicit-null fields) when this test
    was added -- flagged in PR review as the second-largest leak after
    get_adcp_capabilities.
    """

    @pytest.mark.asyncio
    async def test_get_media_buy_delivery_omits_unset_optionals(self):
        from datetime import UTC, datetime

        from adcp.types.generated_poc.media_buy.get_media_buy_delivery_response import (
            ReportingPeriod as DeliveryReportingPeriod,
        )

        from src.core.schemas import AggregatedTotals, GetMediaBuyDeliveryResponse
        from src.core.tools.media_buy_delivery import get_media_buy_delivery

        response = GetMediaBuyDeliveryResponse(
            reporting_period=DeliveryReportingPeriod(
                start=datetime(2026, 1, 1, tzinfo=UTC), end=datetime(2026, 1, 31, tzinfo=UTC)
            ),
            currency="USD",
            aggregated_totals=AggregatedTotals(impressions=0, spend=0, media_buy_count=0),
            media_buy_deliveries=[],
        )
        with patch("src.core.tools.media_buy_delivery._get_media_buy_delivery_impl", return_value=response):
            result = await get_media_buy_delivery(ctx=None)

        assert result.structured_content == {
            "status": "completed",
            "replayed": False,
            "reporting_period": {"start": "2026-01-01T00:00:00Z", "end": "2026-01-31T00:00:00Z"},
            "currency": "USD",
            "aggregated_totals": {"impressions": 0.0, "spend": 0.0, "media_buy_count": 0},
            "media_buy_deliveries": [],
        }


class TestUpdateMediaBuyBareSubmittedOmitsUnsetOptionals:
    """update_media_buy MCP wrapper, manual-approval (bare UpdateMediaBuySubmitted) path.

    _update_media_buy_impl's declared return type is
    ``UpdateMediaBuyResult | UpdateMediaBuySubmitted`` -- the pending-approval
    branch returns UpdateMediaBuySubmitted BARE (no TaskResultEnvelope
    wrapper). That type has no @model_serializer of its own, so it does NOT
    share the envelope's raw-pass safety (verified empirically: 12
    explicit-null fields leak under raw pydantic_core serialization). Its own
    docstring claimed raw-pass was safe here ("yields the spec-correct
    submitted envelope on every transport") -- that claim was false; this
    test is the regression guard for the corrected code and docstring.
    """

    @pytest.mark.asyncio
    async def test_update_media_buy_submitted_omits_unset_optionals(self):
        from src.core.schemas._base import UpdateMediaBuySubmitted
        from src.core.tools.media_buy_update import update_media_buy

        response = UpdateMediaBuySubmitted(task_id="ws_1")
        with patch("src.core.tools.media_buy_update._update_media_buy_impl", return_value=response):
            result = await update_media_buy(media_buy_id="mb_1", paused=True, ctx=None)

        assert result.structured_content == {"task_id": "ws_1", "status": "submitted", "replayed": False}
