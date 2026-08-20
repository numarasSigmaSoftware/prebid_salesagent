"""Reporting webhook frequencies are rejected before create/update persistence."""

from types import SimpleNamespace

import pytest

from src.core.exceptions import AdCPCapabilityNotSupportedError
from src.core.tools._reporting_webhook import (
    validate_reporting_webhook_frequency,
    validate_reporting_webhook_product_support,
)


@pytest.mark.parametrize("frequency", ["hourly", "monthly"])
def test_shared_validator_rejects_unsupported_frequency(frequency):
    webhook = SimpleNamespace(reporting_frequency=frequency)

    with pytest.raises(AdCPCapabilityNotSupportedError, match=frequency):
        validate_reporting_webhook_frequency(webhook)


def test_shared_validator_accepts_daily() -> None:
    validate_reporting_webhook_frequency(SimpleNamespace(reporting_frequency="daily"))


def _product(
    product_id: str,
    *,
    supports_webhooks: bool = True,
    frequencies: tuple[str, ...] = ("daily",),
    metrics: tuple[str, ...] = ("impressions", "spend"),
) -> SimpleNamespace:
    return SimpleNamespace(
        product_id=product_id,
        reporting_capabilities={
            "supports_webhooks": supports_webhooks,
            "available_reporting_frequencies": list(frequencies),
            "available_metrics": list(metrics),
        },
    )


def test_product_validator_accepts_intersection() -> None:
    webhook = SimpleNamespace(reporting_frequency="daily", requested_metrics=["impressions"])
    validate_reporting_webhook_product_support(webhook, [_product("p1"), _product("p2")])


def test_product_validator_accepts_implicit_base_metrics() -> None:
    webhook = SimpleNamespace(reporting_frequency="daily", requested_metrics=["impressions", "spend"])
    validate_reporting_webhook_product_support(
        webhook,
        [_product("p1", metrics=("clicks",)), _product("p2", metrics=())],
    )


def test_product_validator_rejects_when_only_one_selected_product_supports_frequency() -> None:
    webhook = SimpleNamespace(reporting_frequency="daily", requested_metrics=None)

    with pytest.raises(AdCPCapabilityNotSupportedError, match="p-monthly"):
        validate_reporting_webhook_product_support(
            webhook,
            [_product("p-daily"), _product("p-monthly", frequencies=("monthly",))],
        )


@pytest.mark.parametrize(
    ("product", "message"),
    [
        (_product("p-no-webhook", supports_webhooks=False), "p-no-webhook"),
        (_product("p-monthly", frequencies=("monthly",)), "p-monthly"),
        (SimpleNamespace(product_id="p-missing", reporting_capabilities=None), "p-missing"),
    ],
)
def test_product_validator_rejects_unsupported_product(product: SimpleNamespace, message: str) -> None:
    webhook = SimpleNamespace(reporting_frequency="daily", requested_metrics=None)

    with pytest.raises(AdCPCapabilityNotSupportedError, match=message):
        validate_reporting_webhook_product_support(webhook, [product])


def test_product_validator_rejects_metric_missing_from_one_selected_product() -> None:
    webhook = SimpleNamespace(reporting_frequency="daily", requested_metrics=["impressions", "clicks"])

    with pytest.raises(AdCPCapabilityNotSupportedError, match=r"p-no-clicks: clicks") as exc_info:
        validate_reporting_webhook_product_support(
            webhook,
            [
                _product("p-all", metrics=("impressions", "clicks")),
                _product("p-no-clicks", metrics=("impressions",)),
            ],
        )

    assert exc_info.value.field == "reporting_webhook.requested_metrics"
