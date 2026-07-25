"""Reporting webhook terms must be supportable by every selected product."""

from types import SimpleNamespace

import pytest

from src.core.exceptions import AdCPCapabilityNotSupportedError
from src.core.tools.media_buy_create import _validate_reporting_webhook_products


def _product(product_id: str, *, frequencies: list[str], metrics: list[str], webhooks: bool = True):
    return SimpleNamespace(
        product_id=product_id,
        reporting_capabilities={
            "available_reporting_frequencies": frequencies,
            "available_metrics": metrics,
            "supports_webhooks": webhooks,
        },
    )


def test_reporting_contract_accepts_intersection_across_products() -> None:
    webhook = SimpleNamespace(reporting_frequency="daily", requested_metrics=["impressions"])
    products = [
        _product("one", frequencies=["daily"], metrics=["impressions", "clicks"]),
        _product("two", frequencies=["daily", "monthly"], metrics=["impressions"]),
    ]

    _validate_reporting_webhook_products(webhook, products, context=None)


@pytest.mark.parametrize(
    ("webhook", "field"),
    [
        (
            SimpleNamespace(reporting_frequency="monthly", requested_metrics=["impressions"]),
            "reporting_webhook.reporting_frequency",
        ),
        (
            SimpleNamespace(reporting_frequency="daily", requested_metrics=["clicks"]),
            "reporting_webhook.requested_metrics",
        ),
    ],
)
def test_reporting_contract_rejects_any_unsupported_product(webhook, field: str) -> None:
    products = [
        _product("one", frequencies=["daily", "monthly"], metrics=["impressions", "clicks"]),
        _product("two", frequencies=["daily"], metrics=["impressions"]),
    ]

    with pytest.raises(AdCPCapabilityNotSupportedError) as exc_info:
        _validate_reporting_webhook_products(webhook, products, context=None)

    assert exc_info.value.error_code == "UNSUPPORTED_FEATURE"
    assert exc_info.value.field == field
    assert "two" in str(exc_info.value)
