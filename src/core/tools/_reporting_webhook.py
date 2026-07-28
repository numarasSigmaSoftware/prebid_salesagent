"""Shared validation for reporting-webhook capabilities."""

from collections.abc import Iterable
from typing import Any

from adcp.types import ReportingWebhook

from src.core.exceptions import AdCPCapabilityNotSupportedError
from src.core.reporting_capabilities import IMPLICIT_REPORTING_METRICS, SUPPORTED_REPORTING_FREQUENCIES


def _reporting_frequency(reporting_webhook: ReportingWebhook) -> str:
    return str(reporting_webhook.reporting_frequency or "").lower()


def _normalized_values(values: Iterable[Any]) -> set[str]:
    """Normalize enum or string capability values for set comparisons."""
    return {str(getattr(value, "value", value)).lower() for value in values}


def _effective_available_metrics(values: Iterable[Any]) -> set[str]:
    """Return declared metrics plus the base metrics AdCP makes implicit."""
    return _normalized_values(values) | IMPLICIT_REPORTING_METRICS


def validate_reporting_webhook_frequency(reporting_webhook: ReportingWebhook | None) -> None:
    """Reject reporting cadences the seller cannot fulfill.

    AdCP 3.1.1 requires one notification per configured frequency period.
    Persisting an unsupported cadence would acknowledge a webhook that never
    fires, so create and update share this validation before any write.
    """
    if reporting_webhook is None:
        return

    frequency = _reporting_frequency(reporting_webhook)
    if frequency not in SUPPORTED_REPORTING_FREQUENCIES:
        supported = ", ".join(sorted(SUPPORTED_REPORTING_FREQUENCIES))
        raise AdCPCapabilityNotSupportedError(
            f"Reporting frequency '{frequency}' is not supported by this seller.",
            suggestion=f"Use one of the supported reporting frequencies: {supported}.",
            field="reporting_webhook.reporting_frequency",
        )


def validate_reporting_webhook_product_support(
    reporting_webhook: ReportingWebhook | None,
    products: Iterable[Any],
    *,
    required_product_ids: Iterable[str] = (),
) -> None:
    """Require every selected product to advertise the requested webhook contract.

    AdCP 3.1.1 ``core/reporting-webhook.json`` requires the selected cadence to
    be supported by every product and ``requested_metrics`` to be a subset of
    each product's ``available_metrics``. Product capability absence is
    fail-closed because it does not advertise webhook support.
    """
    if reporting_webhook is None:
        return

    frequency = _reporting_frequency(reporting_webhook)
    required_ids = {str(product_id) for product_id in required_product_ids}
    requested_metrics = _normalized_values(reporting_webhook.requested_metrics or ())
    unsupported = set(required_ids)
    unavailable_metrics_by_product: dict[str, set[str]] = {}
    for product in products:
        product_id = str(getattr(product, "product_id", "<unknown>"))
        unsupported.discard(product_id)
        capabilities = getattr(product, "reporting_capabilities", None) or {}
        supports_webhooks = bool(capabilities.get("supports_webhooks"))
        frequencies = _normalized_values(capabilities.get("available_reporting_frequencies", ()))
        if not supports_webhooks or frequency not in frequencies:
            unsupported.add(product_id)
            continue

        unavailable_metrics = requested_metrics - _effective_available_metrics(
            capabilities.get("available_metrics", ())
        )
        if unavailable_metrics:
            unavailable_metrics_by_product[product_id] = unavailable_metrics

    if unsupported:
        product_ids = ", ".join(sorted(unsupported))
        raise AdCPCapabilityNotSupportedError(
            f"Reporting frequency '{frequency}' is not supported by every selected product: {product_ids}.",
            suggestion=(
                "Choose products whose reporting_capabilities enable webhooks "
                f"and include the '{frequency}' reporting frequency."
            ),
            field="reporting_webhook.reporting_frequency",
        )

    if unavailable_metrics_by_product:
        details = "; ".join(
            f"{product_id}: {', '.join(sorted(metrics))}"
            for product_id, metrics in sorted(unavailable_metrics_by_product.items())
        )
        raise AdCPCapabilityNotSupportedError(
            f"Requested reporting metrics are not available on every selected product: {details}.",
            suggestion=(
                "Remove unsupported requested_metrics or choose products whose "
                "reporting_capabilities.available_metrics include every requested metric."
            ),
            field="reporting_webhook.requested_metrics",
        )
