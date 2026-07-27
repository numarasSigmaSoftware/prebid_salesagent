"""Shared validation for reporting-webhook capabilities."""

from collections.abc import Iterable
from typing import Any

from src.core.exceptions import AdCPCapabilityNotSupportedError

SUPPORTED_REPORTING_FREQUENCIES: frozenset[str] = frozenset({"daily"})


def _reporting_frequency(reporting_webhook: Any) -> str:
    return str(getattr(reporting_webhook, "reporting_frequency", "") or "").lower()


def validate_reporting_webhook_frequency(reporting_webhook: Any) -> None:
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
    reporting_webhook: Any,
    products: Iterable[Any],
    *,
    required_product_ids: Iterable[str] = (),
) -> None:
    """Require every selected product to advertise the requested webhook cadence.

    AdCP 3.1.1 ``core/reporting-webhook.json`` requires the selected cadence to
    be supported by every product in the media buy. Product capability absence
    is fail-closed because it does not advertise webhook support.
    """
    if reporting_webhook is None:
        return

    frequency = _reporting_frequency(reporting_webhook)
    required_ids = {str(product_id) for product_id in required_product_ids}
    unsupported = set(required_ids)
    for product in products:
        product_id = str(getattr(product, "product_id", "<unknown>"))
        unsupported.discard(product_id)
        capabilities = getattr(product, "reporting_capabilities", None) or {}
        supports_webhooks = bool(capabilities.get("supports_webhooks"))
        frequencies = {str(value).lower() for value in capabilities.get("available_reporting_frequencies", [])}
        if not supports_webhooks or frequency not in frequencies:
            unsupported.add(product_id)

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
