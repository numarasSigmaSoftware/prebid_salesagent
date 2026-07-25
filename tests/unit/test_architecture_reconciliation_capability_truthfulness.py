"""Enabled reconciliation capability flags must have real adapter contracts."""

from src.adapters import ADAPTER_REGISTRY
from src.adapters.base import AdServerAdapter
from src.adapters.google_ad_manager import GoogleAdManager
from src.adapters.mock_ad_server import MockAdServer

_EXACT_RECONCILIATION_CONTRACTS = {
    (GoogleAdManager, "supports_media_buy_create_reconciliation"),
    (MockAdServer, "supports_media_buy_create_reconciliation"),
    (MockAdServer, "supports_media_buy_update_reconciliation"),
}


def test_enabled_reconciliation_flags_override_the_fail_closed_base_methods() -> None:
    for adapter_class in set(ADAPTER_REGISTRY.values()):
        for capability, method_name in (
            ("supports_media_buy_create_reconciliation", "reconcile_media_buy_create"),
            ("supports_media_buy_update_reconciliation", "reconcile_media_buy_update"),
        ):
            if getattr(adapter_class, capability, False):
                assert (adapter_class, capability) in _EXACT_RECONCILIATION_CONTRACTS, (
                    f"{adapter_class.__name__}.{capability}=True has no reviewed exact provider-operation contract"
                )
                assert getattr(adapter_class, method_name) is not getattr(AdServerAdapter, method_name), (
                    f"{adapter_class.__name__}.{capability}=True requires an exact {method_name} override"
                )
