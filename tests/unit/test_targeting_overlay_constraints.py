"""Cross-field constraints for the AdCP 3.1.1 targeting overlay."""

import pytest
from pydantic import ValidationError

from src.core.schemas import AdCPPackageUpdate, PackageRequest


@pytest.mark.parametrize(
    "overlay",
    [
        {"device_type": ["mobile"], "device_type_exclude": ["mobile"]},
        {
            "geo_proximity": [
                {
                    "lat": 40.7128,
                    "lng": -74.006,
                    "travel_time": {"value": 30, "unit": "min"},
                    "transport_mode": "driving",
                    "radius": {"value": 10, "unit": "km"},
                }
            ]
        },
        {"frequency_cap": {"max_impressions": 3}},
        {
            "keyword_targets": [
                {"keyword": "shoes", "match_type": "broad"},
                {"keyword": "shoes", "match_type": "broad"},
            ]
        },
    ],
)
def test_update_package_rejects_cross_field_constraint_violations(overlay):
    with pytest.raises(ValidationError):
        AdCPPackageUpdate.model_validate({"package_id": "pkg_1", "targeting_overlay": overlay})


@pytest.mark.parametrize(
    "overlay",
    [
        {"device_type": ["mobile"], "device_type_exclude": ["desktop"]},
        {
            "geo_proximity": [
                {
                    "lat": 40.7128,
                    "lng": -74.006,
                    "radius": {"value": 10, "unit": "km"},
                }
            ]
        },
        {
            "frequency_cap": {
                "max_impressions": 3,
                "per": "devices",
                "window": {"interval": 1, "unit": "days"},
            }
        },
        {
            "keyword_targets": [
                {"keyword": "shoes", "match_type": "broad"},
                {"keyword": "shoes", "match_type": "exact"},
            ]
        },
    ],
)
def test_update_package_accepts_valid_cross_field_combinations(overlay):
    AdCPPackageUpdate.model_validate({"package_id": "pkg_1", "targeting_overlay": overlay})


def test_create_package_enforces_targeting_overlay_constraints():
    with pytest.raises(ValidationError):
        PackageRequest.model_validate(
            {
                "budget": 100,
                "pricing_option_id": "pricing_1",
                "product_id": "product_1",
                "targeting_overlay": {
                    "device_type": ["mobile"],
                    "device_type_exclude": ["mobile"],
                },
            }
        )
