"""Shared budget and currency policy helpers."""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def extract_budget_amount_and_currency(
    budget: Any,
    *,
    fallback_currency: str,
) -> tuple[float, str, bool]:
    """Return (amount, currency, explicit_currency) for float or Budget-like objects."""
    if isinstance(budget, int | float):
        return float(budget), fallback_currency, False

    amount = float(budget.total)
    currency = str(budget.currency) if budget.currency else fallback_currency
    explicit_currency = bool(getattr(budget, "currency", None))
    return amount, currency, explicit_currency


def get_minimum_package_budget(
    *,
    product: Any | None,
    currency_limit: Any,
    currency_code: str,
) -> Decimal | None:
    """Return the minimum spend policy for a package in a currency."""
    if product and getattr(product, "pricing_options", None):
        matching_option = next((po for po in product.pricing_options if po.currency == currency_code), None)
        if matching_option and matching_option.min_spend_per_package is not None:
            return Decimal(str(matching_option.min_spend_per_package))

    if currency_limit and getattr(currency_limit, "min_package_budget", None):
        return Decimal(str(currency_limit.min_package_budget))

    return None


def validate_package_minimum_budget(
    *,
    package_budget: Decimal,
    product: Any | None,
    currency_limit: Any,
    currency_code: str,
) -> str | None:
    """Return an error message if package budget violates minimum spend."""
    minimum = get_minimum_package_budget(product=product, currency_limit=currency_limit, currency_code=currency_code)
    if minimum is None or package_budget >= minimum:
        return None
    return (
        f"Package budget ({package_budget} {currency_code}) does not meet minimum spend requirement "
        f"({minimum} {currency_code}) for products in this package"
    )


def get_total_budget_cap(
    *,
    currency_limit: Any,
    flight_days: int,
    package_count: int,
) -> Decimal | None:
    """Derive a campaign-level ceiling from configured package daily limits."""
    max_daily_package_spend = getattr(currency_limit, "max_daily_package_spend", None)
    if max_daily_package_spend is None:
        return None

    safe_days = max(flight_days, 1)
    safe_package_count = max(package_count, 1)
    return Decimal(str(max_daily_package_spend)) * Decimal(safe_days) * Decimal(safe_package_count)


def validate_total_budget_cap(
    *,
    total_budget: Decimal,
    currency_limit: Any,
    currency_code: str,
    flight_days: int,
    package_count: int,
) -> str | None:
    """Return an error message if total budget exceeds the derived ceiling."""
    cap = get_total_budget_cap(currency_limit=currency_limit, flight_days=flight_days, package_count=package_count)
    if cap is None or total_budget <= cap:
        return None
    return (
        f"Updated campaign budget ({total_budget} {currency_code}) exceeds the allowed maximum "
        f"({cap} {currency_code}) for the current flight and package count."
    )
