"""Canonical reporting-capability builders for seller-owned product data."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from adcp.types import ReportingCapabilities

SUPPORTED_REPORTING_FREQUENCIES: frozenset[str] = frozenset({"daily"})


def build_daily_reporting_capabilities(
    *,
    supports_webhooks: bool,
    available_metrics: Iterable[str] = ("impressions",),
) -> dict[str, Any]:
    """Return a fresh, schema-validated capability document for daily reporting."""
    capabilities = ReportingCapabilities.model_validate(
        {
            "available_reporting_frequencies": sorted(SUPPORTED_REPORTING_FREQUENCIES),
            "expected_delay_minutes": 1440,
            "timezone": "UTC",
            "supports_webhooks": supports_webhooks,
            "available_metrics": list(available_metrics),
            "date_range_support": "date_range",
        }
    )
    return capabilities.model_dump(mode="json", exclude_none=True)
