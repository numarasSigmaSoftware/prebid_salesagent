"""Canonical reporting-capability builders for seller-owned product data."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from adcp.types import AvailableMetric, ReportingCapabilities

SUPPORTED_REPORTING_FREQUENCIES: frozenset[str] = frozenset({"daily"})

# AdCP 3.1.1 core/reporting-capabilities.json, available_metrics.description:
# "Impressions and spend are always implicitly included." Both the capability
# validator (which accepts a requested_metrics set on the premise these ride
# along) and the webhook serializer (which must not project them out of the
# body) read this one set, so it lives here rather than beside either consumer.
# Members are spelled through AvailableMetric so a vocabulary change fails at
# import rather than silently degrading to an unsatisfiable literal.
IMPLICIT_REPORTING_METRICS: frozenset[str] = frozenset({AvailableMetric.impressions.value, AvailableMetric.spend.value})


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
