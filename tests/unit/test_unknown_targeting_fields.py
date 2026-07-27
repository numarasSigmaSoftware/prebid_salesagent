"""Tests for AdCP targeting extension handling.

AdCP 3.1.1 ``core/targeting.json`` declares ``additionalProperties: true``.
Unknown buyer-submitted dimensions are accepted and ignored consistently,
while known managed-only fields remain available to access-control validation.
"""

from src.core.schemas import Targeting


class TestTargetingExtensions:
    """Unknown targeting extensions are accepted but not persisted."""

    def test_unknown_field_ignored(self):
        targeting = Targeting(totally_bogus="hello", geo_countries=["US"])

        assert targeting.model_dump(exclude_none=True) == {"geo_countries": ["US"]}

    def test_known_field_accepted(self):
        """Known model fields remain accepted and extensions are not retained."""
        t = Targeting(geo_countries=["US"], device_type_any_of=["mobile"])
        assert t.geo_countries is not None
        assert t.model_extra is None

    def test_managed_field_accepted(self):
        """Managed-only fields are real model fields, accepted normally."""
        t = Targeting(axe_include_segment="foo", key_value_pairs={"k": "v"})
        assert t.axe_include_segment == "foo"
        assert t.model_extra is None

    def test_v2_normalized_field_accepted(self):
        """v2 field names consumed by normalizer should not cause rejection."""
        t = Targeting(geo_country_any_of=["CA"])
        assert t.geo_countries is not None
        assert t.model_extra is None

    def test_multiple_unknown_fields_ignored(self):
        targeting = Targeting(bogus_one="a", bogus_two="b")

        assert targeting.model_dump(exclude_none=True) == {}


class TestValidateUnknownTargetingFields:
    """validate_unknown_targeting_fields should report model_extra keys.

    Unknown extension fields are ignored at parse time, so ``model_extra``
    remains empty and capability validation sees only recognized dimensions.
    """

    def test_accepts_all_known_fields(self):
        from src.services.targeting_capabilities import validate_unknown_targeting_fields

        t = Targeting(geo_countries=["US"], device_type_any_of=["mobile"])
        violations = validate_unknown_targeting_fields(t)
        assert violations == []

    def test_accepts_managed_fields(self):
        """Managed fields are known model fields — they should NOT be flagged here.
        (They are caught separately by validate_overlay_targeting's access checks.)"""
        from src.services.targeting_capabilities import validate_unknown_targeting_fields

        t = Targeting(key_value_pairs={"k": "v"}, axe_include_segment="seg")
        violations = validate_unknown_targeting_fields(t)
        assert violations == []

    def test_accepts_v2_normalized_fields(self):
        """v2 fields converted by normalizer should not be flagged."""
        from src.services.targeting_capabilities import validate_unknown_targeting_fields

        t = Targeting(geo_country_any_of=["US"])
        violations = validate_unknown_targeting_fields(t)
        assert violations == []

    def test_empty_targeting_no_violations(self):
        from src.services.targeting_capabilities import validate_unknown_targeting_fields

        t = Targeting()
        violations = validate_unknown_targeting_fields(t)
        assert violations == []
