"""Test schema validation modes (production vs development).

Validation mode is set at class definition time via get_pydantic_extra_mode():
- Dev/test (default): extra='forbid' — rejects unknown fields with ValidationError
- Production (ENVIRONMENT=production): extra='ignore' — silently drops unknown fields

To test production-mode behavior, run:
    ENVIRONMENT=production pytest tests/unit/test_schema_validation_modes.py -v
"""

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from src.core.schemas import (
    CreateMediaBuyRequest,
    Creative,
    GetMediaBuyDeliveryRequest,
    GetProductsRequest,
    ListCreativeFormatsRequest,
    ListCreativesRequest,
    PackageRequest,
    Targeting,
)

# Minimal valid data for constructing test models
# adcp 3.6.0: brand replaced brand_manifest
_VALID_CMR_DATA = {
    "brand": {"domain": "testproduct.com"},
    "packages": [{"product_id": "prod_1", "budget": 5000.0, "pricing_option_id": "test"}],
    "start_time": "2025-02-15T00:00:00Z",
    "end_time": "2025-02-28T23:59:59Z",
    "idempotency_key": "unit-test-key-cmr-shared-data",
}

_VALID_PACKAGE_DATA = {"product_id": "prod_1", "budget": 5000.0, "pricing_option_id": "test"}


class TestBuyerModelRejectsExtraInDev:
    """All buyer-facing request models reject unknown fields in dev mode (default)."""

    def test_create_media_buy_request_rejects_extra(self):
        with pytest.raises(ValidationError, match="bogus"):
            CreateMediaBuyRequest(**_VALID_CMR_DATA, bogus="injected")

    def test_package_request_rejects_extra(self):
        with pytest.raises(ValidationError, match="bogus"):
            PackageRequest(**_VALID_PACKAGE_DATA, bogus="injected")

    def test_targeting_rejects_extra(self):
        with pytest.raises(ValidationError, match="bogus"):
            Targeting(geo_country_any_of=["US"], bogus="injected")

    def test_creative_rejects_extra(self):
        with pytest.raises(ValidationError, match="bogus"):
            Creative(
                creative_id="c_1",
                variants=[],
                name="Test",
                format_id={"agent_url": "https://example.com", "id": "display/banner"},
                bogus="injected",
            )

    def test_list_creative_formats_request_rejects_extra(self):
        with pytest.raises(ValidationError, match="bogus"):
            ListCreativeFormatsRequest(bogus="injected")

    def test_list_creatives_request_rejects_extra(self):
        with pytest.raises(ValidationError, match="bogus"):
            ListCreativesRequest(bogus="injected")

    def test_get_media_buy_delivery_request_rejects_extra(self):
        with pytest.raises(ValidationError, match="bogus"):
            GetMediaBuyDeliveryRequest(bogus="injected")


class TestNestedModelRejectsExtraInDev:
    """Extra fields on nested models within CreateMediaBuyRequest are rejected."""

    def test_nested_package_rejects_extra(self):
        """Bogus field on PackageRequest within CMR.packages is rejected."""
        data = {**_VALID_CMR_DATA, "packages": [{**_VALID_PACKAGE_DATA, "bogus_pkg_field": "injected"}]}
        with pytest.raises(ValidationError, match="bogus_pkg_field"):
            CreateMediaBuyRequest(**data)

    def test_nested_targeting_rejects_extra(self):
        """Bogus field on targeting_overlay within a package is rejected."""
        data = {
            **_VALID_CMR_DATA,
            "packages": [
                {
                    **_VALID_PACKAGE_DATA,
                    "targeting_overlay": {"geo_country_any_of": ["US"], "bogus_targeting": "injected"},
                }
            ],
        }
        with pytest.raises(ValidationError, match="bogus_targeting"):
            CreateMediaBuyRequest(**data)


class TestExtFieldAccepted:
    """The AdCP ext field is the sanctioned extension mechanism and must be accepted."""

    def test_ext_field_accepted_on_cmr(self):
        cmr = CreateMediaBuyRequest(
            **_VALID_CMR_DATA,
            ext={"vendor": {"custom": "value"}},
        )
        assert cmr.ext is not None


class TestInternalModelsRejectExtra:
    """Models inheriting from our AdCPBaseModel also reject extra fields in dev."""

    def test_get_products_request_rejects_extra(self):
        with pytest.raises(ValidationError, match="unknown_field"):
            GetProductsRequest(
                brief="test",
                brand={"domain": "test.com"},
                unknown_field="should_fail",
            )


class TestConfigHelperFunctions:
    """Test the config helper functions directly.

    is_production() recognizes THREE independent signals (ENVIRONMENT=production,
    PRODUCTION set, FLY_APP_NAME set) -- matching what scripts/run_server.py (the
    real server bootstrap) and several src/core modules already treat as
    production. Every test here that asserts `not is_production()` pops all THREE
    env vars, not just ENVIRONMENT -- otherwise it would only be pinning the first
    signal and silently stop covering the others the moment any one of them
    changed.
    """

    def _clear_production_signals(self):
        os.environ.pop("ENVIRONMENT", None)
        os.environ.pop("PRODUCTION", None)
        os.environ.pop("FLY_APP_NAME", None)

    def test_development_mode(self):
        from src.core.config import get_pydantic_extra_mode, is_production

        with patch.dict(os.environ, {}, clear=False):
            self._clear_production_signals()
            assert not is_production()
            assert get_pydantic_extra_mode() == "forbid"

    def test_production_mode(self):
        from src.core.config import get_pydantic_extra_mode, is_production

        with patch.dict(os.environ, {"ENVIRONMENT": "production"}):
            assert is_production()
            assert get_pydantic_extra_mode() == "ignore"

    def test_staging_defaults_to_strict(self):
        from src.core.config import get_pydantic_extra_mode, is_production

        with patch.dict(os.environ, {}, clear=False):
            self._clear_production_signals()
            os.environ["ENVIRONMENT"] = "staging"
            assert not is_production()
            assert get_pydantic_extra_mode() == "forbid"

    def test_case_insensitive(self):
        from src.core.config import is_production

        with patch.dict(os.environ, {"ENVIRONMENT": "PRODUCTION"}):
            assert is_production()
        with patch.dict(os.environ, {"ENVIRONMENT": "Production"}):
            assert is_production()

    def test_production_env_var_is_recognized_even_with_environment_at_its_default(self):
        """The gap this pins: a deployment that sets PRODUCTION (an established
        signal -- see scripts/run_server.py, src/core/auth.py, src/core/logging_config.py,
        src/core/audit_logger.py) but never explicitly sets ENVIRONMENT=production
        must still be recognized as production, not silently read as development."""
        from src.core.config import is_production

        with patch.dict(os.environ, {}, clear=False):
            self._clear_production_signals()
            os.environ["PRODUCTION"] = "true"
            assert is_production()

    def test_fly_app_name_is_recognized_even_with_environment_at_its_default(self):
        """FLY_APP_NAME is set automatically by Fly.io on every deploy -- a Fly
        deployment that never explicitly sets ENVIRONMENT=production must still be
        recognized as production."""
        from src.core.config import is_production

        with patch.dict(os.environ, {}, clear=False):
            self._clear_production_signals()
            os.environ["FLY_APP_NAME"] = "salesagent-prod"
            assert is_production()

    @pytest.mark.parametrize("truthy_value", ["true", "TRUE", "True", "1", "yes", "on", "  true  "])
    def test_production_recognizes_the_truthy_vocabulary(self, truthy_value):
        from src.core.config import is_production

        with patch.dict(os.environ, {}, clear=False):
            self._clear_production_signals()
            os.environ["PRODUCTION"] = truthy_value
            assert is_production(), f"PRODUCTION={truthy_value!r} should be production"

    @pytest.mark.parametrize("falsy_value", ["false", "FALSE", "False", "0", "no", "off", "", "banana"])
    def test_production_false_is_not_treated_as_production(self, falsy_value):
        """The gap this pins: bool(os.environ.get("PRODUCTION")) treats ANY
        non-empty string as true, so PRODUCTION=false -- an operator EXPLICITLY
        turning it off -- used to flip is_production() to True. Since
        is_production() also controls schema strictness (get_pydantic_extra_mode)
        and the WEBHOOK_AUDIT_HMAC_KEY production requirement, that silently
        switched a schema from strict extra="forbid" to permissive extra="ignore"
        and demanded a key the operator never intended to need."""
        from src.core.config import is_production

        with patch.dict(os.environ, {}, clear=False):
            self._clear_production_signals()
            os.environ["PRODUCTION"] = falsy_value
            assert not is_production(), f"PRODUCTION={falsy_value!r} should NOT be production"


class TestProductionModeBehavior:
    """Verify production mode end-to-end: env var → config helper → model behavior.

    model_config is evaluated at class definition time, so pre-imported models
    can't change mode at runtime. We create a fresh model class inside the
    patched environment to test the full chain.
    """

    def test_production_model_accepts_extra_fields(self):
        """Model defined under ENVIRONMENT=production silently drops extra fields."""
        from pydantic import BaseModel, ConfigDict

        from src.core.config import get_pydantic_extra_mode

        with patch.dict(os.environ, {"ENVIRONMENT": "production"}):

            class ProductionModel(BaseModel):
                model_config = ConfigDict(extra=get_pydantic_extra_mode())
                brief: str

            obj = ProductionModel(brief="test", unknown_field="should_be_ignored")
            assert obj.brief == "test"
            assert not hasattr(obj, "unknown_field")

    def test_dev_model_rejects_extra_fields(self):
        """Model defined under dev mode rejects extra fields."""
        from pydantic import BaseModel, ConfigDict

        from src.core.config import get_pydantic_extra_mode

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENVIRONMENT", None)

            class DevModel(BaseModel):
                model_config = ConfigDict(extra=get_pydantic_extra_mode())
                brief: str

            with pytest.raises(ValidationError, match="unknown_field"):
                DevModel(brief="test", unknown_field="should_fail")


class TestExtraModeFollowsDeclaredProductionOnly:
    """Schema strictness follows declares_production_explicitly(), not is_production().

    is_production() also answers True for a deployment that merely has FLY_APP_NAME
    populated or PRODUCTION truthy. Gating extra-mode on it silently switched such a
    deployment -- one that never declared production and changed nothing -- from
    strict extra="forbid" to permissive extra="ignore", so unknown request fields it
    had been rejecting would start being accepted on upgrade.

    These assert the two predicates DIVERGE here: is_production() is True in the
    inferred cases while the mode stays "forbid". Pointing get_pydantic_extra_mode
    back at is_production() reddens the two inferred cases.
    """

    @staticmethod
    def _clear_production_signals(monkeypatch):
        for var in ("ENVIRONMENT", "PRODUCTION", "FLY_APP_NAME"):
            monkeypatch.delenv(var, raising=False)

    def test_declared_production_gets_forward_compatible_ignore(self, monkeypatch):
        from src.core.config import get_pydantic_extra_mode

        self._clear_production_signals(monkeypatch)
        monkeypatch.setenv("ENVIRONMENT", "production")
        assert get_pydantic_extra_mode() == "ignore"

    @pytest.mark.parametrize(
        ("signal", "value"),
        [("FLY_APP_NAME", "salesagent-prod"), ("PRODUCTION", "true")],
    )
    def test_inferred_production_keeps_strict_forbid(self, monkeypatch, signal, value):
        """The upgrade-safety case: inferred production must not loosen the schema."""
        from src.core.config import get_pydantic_extra_mode, is_production

        self._clear_production_signals(monkeypatch)
        monkeypatch.setenv(signal, value)

        # Negative control: the broad predicate DOES fire here, so this is a real
        # divergence and not a case where both predicates happen to agree.
        assert is_production() is True
        assert get_pydantic_extra_mode() == "forbid", (
            f"{signal} alone must not loosen schema validation -- that deployment never "
            "declared production and changed nothing"
        )
