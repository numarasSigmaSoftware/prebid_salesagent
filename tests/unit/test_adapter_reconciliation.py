"""Provider-state inspection is never treated as operation identity."""

from unittest.mock import MagicMock, call

from src.adapters.base import AdServerAdapter, DownstreamMutation, ReconciliationOutcome
from src.adapters.broadstreet.adapter import BroadstreetAdapter
from src.adapters.google_ad_manager import GoogleAdManager
from src.adapters.kevel import Kevel
from src.adapters.mock_ad_server import MockAdServer
from src.adapters.triton_digital import TritonDigital
from src.adapters.xandr import XandrAdapter


def test_adapter_default_fails_closed_without_operation_lookup() -> None:
    adapter = MagicMock()
    mutation = DownstreamMutation(
        downstream_request_id="request-1",
        media_buy_id="campaign-1",
        action="pause_media_buy",
        package_id=None,
        budget=None,
    )

    result = AdServerAdapter.reconcile_media_buy_update(adapter, mutation)

    assert result.outcome is ReconciliationOutcome.UNKNOWN
    assert result.response is None


def test_external_adapters_without_exact_markers_reject_before_invocation() -> None:
    for adapter_type in (BroadstreetAdapter, Kevel, TritonDigital):
        assert adapter_type.requires_downstream_reconciliation is True
        assert adapter_type.supports_media_buy_create_reconciliation is False
        assert adapter_type.supports_media_buy_update_reconciliation is False


def test_gam_enables_only_its_exact_marked_create_contract() -> None:
    assert GoogleAdManager.supports_media_buy_create_reconciliation is True
    assert GoogleAdManager.supports_media_buy_update_reconciliation is False


def test_gam_reconciliation_uses_full_marker_when_31_bit_prefix_collides() -> None:
    adapter = object.__new__(GoogleAdManager)
    adapter.orders_manager = MagicMock()
    adapter.orders_manager.find_order_by_operation_marker.return_value = None
    common = {
        "media_buy_id": "mb-1",
        "action": "create_media_buy",
        "package_id": None,
        "budget": None,
    }
    first = DownstreamMutation(downstream_request_id="deadbeef" + "1" * 56, **common)
    second = DownstreamMutation(downstream_request_id="deadbeef" + "2" * 56, **common)

    assert adapter.reconcile_media_buy_create(first).outcome is ReconciliationOutcome.NOT_APPLIED
    assert adapter.reconcile_media_buy_create(second).outcome is ReconciliationOutcome.NOT_APPLIED
    assert adapter.orders_manager.find_order_by_operation_marker.call_args_list == [
        call(f"op_{first.downstream_request_id}"),
        call(f"op_{second.downstream_request_id}"),
    ]


def test_xandr_remains_explicitly_unsupported_before_invocation() -> None:
    assert XandrAdapter.requires_downstream_reconciliation is True
    assert XandrAdapter.supports_media_buy_create_reconciliation is False
    assert XandrAdapter.supports_media_buy_update_reconciliation is False


def test_mock_uses_the_same_deterministic_reconciliation_contract() -> None:
    assert MockAdServer.requires_downstream_reconciliation is True
    assert MockAdServer.supports_media_buy_create_reconciliation is True
    assert MockAdServer.supports_media_buy_update_reconciliation is True
