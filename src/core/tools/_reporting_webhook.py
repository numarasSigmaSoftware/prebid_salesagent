"""Shared validation for reporting-webhook capabilities."""

from typing import Any

from src.core.exceptions import AdCPCapabilityNotSupportedError

SUPPORTED_REPORTING_FREQUENCIES: frozenset[str] = frozenset({"daily"})


def validate_reporting_webhook_frequency(reporting_webhook: Any) -> None:
    """Reject reporting cadences the seller cannot fulfill.

    AdCP 3.1.1 requires one notification per configured frequency period.
    Persisting an unsupported cadence would acknowledge a webhook that never
    fires, so create and update share this validation before any write.
    """
    if reporting_webhook is None:
        return

    frequency = str(getattr(reporting_webhook, "reporting_frequency", "") or "").lower()
    if frequency not in SUPPORTED_REPORTING_FREQUENCIES:
        supported = ", ".join(sorted(SUPPORTED_REPORTING_FREQUENCIES))
        raise AdCPCapabilityNotSupportedError(
            f"Reporting frequency '{frequency}' is not supported by this seller.",
            suggestion=f"Use one of the supported reporting frequencies: {supported}.",
        )
