"""Shared helpers for reconstructing media-buy reconciliation results."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from src.adapters.base import DownstreamMutation, ReconciliationOutcome, ReconciliationResult
from src.core.database.repositories.uow import MediaBuyUoW
from src.core.schemas import AffectedPackage, UpdateMediaBuySuccess


@dataclass(frozen=True)
class MediaPackageSnapshot:
    """Detached scalar state needed by provider reconciliation."""

    package_id: str
    package_config: dict[str, Any]


def load_media_package_snapshots(
    tenant_id: str,
    mutation: DownstreamMutation,
) -> list[MediaPackageSnapshot]:
    """Read scoped packages and detach only scalar reconciliation state."""
    with MediaBuyUoW(tenant_id) as uow:
        assert uow.media_buys is not None
        packages = (
            [uow.media_buys.get_package(mutation.media_buy_id, mutation.package_id)]
            if mutation.package_id
            else uow.media_buys.get_packages(mutation.media_buy_id)
        )
        return [
            MediaPackageSnapshot(
                package_id=package.package_id,
                package_config=dict(package.package_config or {}),
            )
            for package in packages
            if package is not None
        ]


def applied_update_result(
    mutation: DownstreamMutation,
    *,
    paused: bool | None = None,
    changes_applied: dict[str, Any] | None = None,
) -> ReconciliationResult:
    """Build the canonical response for a provider update proven applied."""
    affected_packages = []
    if mutation.package_id is not None and (paused is not None or changes_applied is not None):
        affected_packages.append(
            AffectedPackage(
                package_id=mutation.package_id,
                paused=paused or False,
                changes_applied=changes_applied,
                buyer_package_ref=None,
            )
        )
    return ReconciliationResult(
        ReconciliationOutcome.APPLIED,
        UpdateMediaBuySuccess(
            media_buy_id=mutation.media_buy_id,
            affected_packages=affected_packages,
            implementation_date=mutation.implementation_date,
        ),
    )


def classify_observed_values(observed: list[bool]) -> ReconciliationOutcome:
    """Classify all/none/mixed provider state without risking a blind retry."""
    if not observed or all(observed):
        return ReconciliationOutcome.APPLIED
    if not any(observed):
        return ReconciliationOutcome.NOT_APPLIED
    return ReconciliationOutcome.UNKNOWN


def reconcile_local_package_update(
    mutation: DownstreamMutation,
    packages: list[MediaPackageSnapshot],
) -> ReconciliationResult | None:
    """Reconcile provider-local updates from their durable package fields."""
    if mutation.action not in {"update_package_budget", "update_package_impressions"}:
        return None
    if len(packages) != 1 or mutation.budget is None:
        return ReconciliationResult(ReconciliationOutcome.NOT_APPLIED)
    field = "budget" if mutation.action == "update_package_budget" else "impressions"
    value = packages[0].package_config.get(field)
    if value is None or float(value) != float(mutation.budget):
        return ReconciliationResult(ReconciliationOutcome.NOT_APPLIED)
    return applied_update_result(mutation, changes_applied={field: mutation.budget})


def _reconcile_gam_status(
    mutation: DownstreamMutation,
    package_specs: list[tuple[str, str, dict[str, Any]]],
    by_id: dict[str, Any],
    safe_get: Callable[..., Any],
) -> ReconciliationResult:
    desired_status = "PAUSED" if mutation.action.startswith("pause_") else "READY"
    observed = [
        str(safe_get(by_id.get(line_item_id), "status", default="")).upper() == desired_status
        for _, line_item_id, _ in package_specs
        if line_item_id
    ]
    outcome = classify_observed_values(observed)
    if outcome is not ReconciliationOutcome.APPLIED:
        return ReconciliationResult(outcome)
    if mutation.package_id:
        return applied_update_result(mutation, paused=desired_status == "PAUSED")
    return ReconciliationResult(
        ReconciliationOutcome.APPLIED,
        UpdateMediaBuySuccess(
            media_buy_id=mutation.media_buy_id,
            implementation_date=mutation.implementation_date,
            affected_packages=[
                AffectedPackage(
                    package_id=package_id,
                    paused=desired_status == "PAUSED",
                    changes_applied=None,
                    buyer_package_ref=None,
                )
                for package_id, _, _ in package_specs
            ],
        ),
    )


def _reconcile_gam_budget(
    mutation: DownstreamMutation,
    package_specs: list[tuple[str, str, dict[str, Any]]],
    by_id: dict[str, Any],
    safe_get: Callable[..., Any],
) -> ReconciliationResult:
    if mutation.budget is None or len(package_specs) != 1:
        return ReconciliationResult(ReconciliationOutcome.UNKNOWN)
    _, line_item_id, pricing = package_specs[0]
    line_item = by_id.get(line_item_id)
    if line_item is None:
        return ReconciliationResult(ReconciliationOutcome.NOT_APPLIED)
    pricing_model = str(pricing.get("model", "cpm")).lower()
    if pricing_model == "flat_rate":
        return ReconciliationResult(ReconciliationOutcome.UNKNOWN)
    rate = float(safe_get(line_item, "costPerUnit", "microAmount", default=0)) / 1_000_000
    if rate <= 0:
        return ReconciliationResult(ReconciliationOutcome.UNKNOWN)
    expected_units = (
        int((mutation.budget * 1000) / rate) if pricing_model in {"cpm", "vcpm"} else int(mutation.budget / rate)
    )
    observed_units = int(safe_get(line_item, "primaryGoal", "units", default=-1))
    if observed_units != expected_units:
        return ReconciliationResult(ReconciliationOutcome.NOT_APPLIED)
    return applied_update_result(mutation)


def reconcile_gam_line_items(
    mutation: DownstreamMutation,
    package_specs: list[tuple[str, str, dict[str, Any]]],
    line_items: list[Any],
    safe_get: Callable[..., Any],
) -> ReconciliationResult:
    """Classify GAM observations without another consequential mutation."""
    by_id = {str(safe_get(item, "id", default="")): item for item in line_items}
    if mutation.action in {"pause_package", "resume_package", "pause_media_buy", "resume_media_buy"}:
        return _reconcile_gam_status(mutation, package_specs, by_id, safe_get)
    if mutation.action == "update_package_budget":
        return _reconcile_gam_budget(mutation, package_specs, by_id, safe_get)
    return ReconciliationResult(ReconciliationOutcome.UNKNOWN)


def reconcile_campaign_flight_update(
    mutation: DownstreamMutation,
    *,
    dry_run: bool,
    get_campaign: Callable[[], dict[str, Any]],
    list_flights: Callable[[], list[dict[str, Any]]],
    campaign_active: Callable[[dict[str, Any]], bool],
    flight_name: Callable[[dict[str, Any]], str],
    flight_active: Callable[[dict[str, Any]], bool],
    flight_rate: Callable[[dict[str, Any]], float],
    flight_impressions: Callable[[dict[str, Any]], int],
    default_rate: float,
) -> ReconciliationResult:
    """Reconcile the common campaign/flight update shape used by HTTP adapters."""
    if dry_run:
        return applied_update_result(
            mutation,
            paused=mutation.action.startswith("pause_") if mutation.package_id else None,
        )
    try:
        if mutation.action in {"pause_media_buy", "resume_media_buy"}:
            desired_active = mutation.action == "resume_media_buy"
            if campaign_active(get_campaign()) != desired_active:
                return ReconciliationResult(ReconciliationOutcome.NOT_APPLIED)
            return applied_update_result(mutation)

        flights = list_flights()
        flight = next((item for item in flights if flight_name(item) == mutation.package_id), None)
        if flight is None:
            return ReconciliationResult(ReconciliationOutcome.NOT_APPLIED)
        if mutation.action in {"pause_package", "resume_package"}:
            desired_active = mutation.action == "resume_package"
            if flight_active(flight) != desired_active:
                return ReconciliationResult(ReconciliationOutcome.NOT_APPLIED)
            return applied_update_result(mutation, paused=not desired_active)
        if mutation.action in {"update_package_budget", "update_package_impressions"} and mutation.budget is not None:
            rate = flight_rate(flight) or default_rate
            expected = (
                int((mutation.budget / rate) * 1000) if mutation.action == "update_package_budget" else mutation.budget
            )
            if flight_impressions(flight) != expected:
                return ReconciliationResult(ReconciliationOutcome.NOT_APPLIED)
            return applied_update_result(mutation)
    except Exception:
        return ReconciliationResult(ReconciliationOutcome.UNKNOWN)
    return ReconciliationResult(ReconciliationOutcome.UNKNOWN)
