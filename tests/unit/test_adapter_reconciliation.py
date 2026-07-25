"""Provider reconciliation helpers classify observed state conservatively."""

from src.adapters.base import DownstreamMutation, ReconciliationOutcome
from src.adapters.reconciliation import reconcile_campaign_flight_update


def _mutation(action: str, *, package_id: str | None = None, budget: int | None = None) -> DownstreamMutation:
    return DownstreamMutation(
        downstream_request_id="request-1",
        media_buy_id="campaign-1",
        action=action,
        package_id=package_id,
        budget=budget,
    )


def test_campaign_state_proves_applied_without_mutation() -> None:
    result = reconcile_campaign_flight_update(
        _mutation("pause_media_buy"),
        dry_run=False,
        get_campaign=lambda: {"active": False},
        list_flights=lambda: [],
        campaign_active=lambda item: item["active"],
        flight_name=lambda item: item["name"],
        flight_active=lambda item: item["active"],
        flight_rate=lambda item: item["rate"],
        flight_impressions=lambda item: item["impressions"],
        default_rate=10,
    )

    assert result.outcome is ReconciliationOutcome.APPLIED
    assert result.response is not None
    assert result.response.media_buy_id == "campaign-1"


def test_flight_state_proves_not_applied_when_value_differs() -> None:
    result = reconcile_campaign_flight_update(
        _mutation("update_package_impressions", package_id="pkg-1", budget=500),
        dry_run=False,
        get_campaign=lambda: {},
        list_flights=lambda: [{"name": "pkg-1", "active": True, "rate": 10, "impressions": 400}],
        campaign_active=lambda item: item["active"],
        flight_name=lambda item: item["name"],
        flight_active=lambda item: item["active"],
        flight_rate=lambda item: item["rate"],
        flight_impressions=lambda item: item["impressions"],
        default_rate=10,
    )

    assert result.outcome is ReconciliationOutcome.NOT_APPLIED


def test_provider_read_failure_is_unknown() -> None:
    def fail() -> dict:
        raise RuntimeError("provider unavailable")

    result = reconcile_campaign_flight_update(
        _mutation("resume_media_buy"),
        dry_run=False,
        get_campaign=fail,
        list_flights=lambda: [],
        campaign_active=lambda item: item["active"],
        flight_name=lambda item: item["name"],
        flight_active=lambda item: item["active"],
        flight_rate=lambda item: item["rate"],
        flight_impressions=lambda item: item["impressions"],
        default_rate=10,
    )

    assert result.outcome is ReconciliationOutcome.UNKNOWN
