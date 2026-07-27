"""Test schema validation modes (production vs development).

Validation mode is set at class definition time via get_pydantic_extra_mode():
- Dev/test (default): extra='forbid' — rejects unknown fields with ValidationError
- Production (ENVIRONMENT=production): extra='ignore' — silently drops unknown fields

To test production-mode behavior, run:
    ENVIRONMENT=production pytest tests/unit/test_schema_validation_modes.py -v
"""

import os
import subprocess
import sys
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
    """Buyer models are strict unless their AdCP schema permits extensions."""

    def test_create_media_buy_request_rejects_extra(self):
        with pytest.raises(ValidationError, match="bogus"):
            CreateMediaBuyRequest(**_VALID_CMR_DATA, bogus="injected")

    def test_package_request_rejects_extra(self):
        with pytest.raises(ValidationError, match="bogus"):
            PackageRequest(**_VALID_PACKAGE_DATA, bogus="injected")

    def test_targeting_ignores_extension(self):
        targeting = Targeting(geo_country_any_of=["US"], bogus="injected")

        assert "bogus" not in targeting.model_dump(exclude_none=True)

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


class TestNestedModelExtraFieldsInDev:
    """Nested models follow their authoritative additional-properties contracts."""

    def test_nested_package_rejects_extra(self):
        """Bogus field on PackageRequest within CMR.packages is rejected."""
        data = {**_VALID_CMR_DATA, "packages": [{**_VALID_PACKAGE_DATA, "bogus_pkg_field": "injected"}]}
        with pytest.raises(ValidationError, match="bogus_pkg_field"):
            CreateMediaBuyRequest(**data)

    def test_nested_targeting_ignores_extension(self):
        """Targeting additional properties are accepted and ignored by AdCP 3.1.1."""
        data = {
            **_VALID_CMR_DATA,
            "packages": [
                {
                    **_VALID_PACKAGE_DATA,
                    "targeting_overlay": {"geo_country_any_of": ["US"], "bogus_targeting": "injected"},
                }
            ],
        }
        request = CreateMediaBuyRequest(**data)

        assert request.packages[0].targeting_overlay is not None
        assert "bogus_targeting" not in request.packages[0].targeting_overlay.model_dump(exclude_none=True)


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
    """Test the config helper functions directly."""

    def test_development_mode(self):
        from src.core.config import get_pydantic_extra_mode, is_production

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENVIRONMENT", None)
            os.environ.pop("PRODUCTION", None)
            assert not is_production()
            assert get_pydantic_extra_mode() == "forbid"

    def test_production_mode(self):
        from src.core.config import get_pydantic_extra_mode, is_production

        with patch.dict(os.environ, {"ENVIRONMENT": "production"}):
            assert is_production()
            assert get_pydantic_extra_mode() == "ignore"

    def test_staging_defaults_to_strict(self):
        from src.core.config import get_pydantic_extra_mode, is_production

        with patch.dict(os.environ, {"ENVIRONMENT": "staging"}):
            os.environ.pop("PRODUCTION", None)
            assert not is_production()
            assert get_pydantic_extra_mode() == "forbid"

    def test_case_insensitive(self):
        from src.core.config import is_production

        with patch.dict(os.environ, {"ENVIRONMENT": "PRODUCTION"}):
            assert is_production()
        with patch.dict(os.environ, {"ENVIRONMENT": "Production"}):
            assert is_production()

    def test_production_via_production_env_var_only(self):
        """A deploy setting PRODUCTION=true but not ENVIRONMENT is still production.

        is_production() takes the UNION of the two conventions so the admin
        security gates (which historically keyed on PRODUCTION) and the test-header
        gate (which keys on ENVIRONMENT) cannot diverge.
        """
        from src.core.config import is_production

        with patch.dict(os.environ, {"PRODUCTION": "true"}):
            os.environ.pop("ENVIRONMENT", None)
            assert is_production()
        # case-insensitive
        with patch.dict(os.environ, {"PRODUCTION": "TRUE"}):
            os.environ.pop("ENVIRONMENT", None)
            assert is_production()

    def test_not_production_when_neither_set(self):
        from src.core.config import is_production

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENVIRONMENT", None)
            os.environ.pop("PRODUCTION", None)
            assert not is_production()
        # PRODUCTION set to a non-"true" value is not production
        with patch.dict(os.environ, {"PRODUCTION": "false"}):
            os.environ.pop("ENVIRONMENT", None)
            assert not is_production()

    def test_is_admin_production_delegates_to_is_production(self):
        """is_admin_production() is a thin admin-facing alias of is_production()."""
        from src.admin.utils import is_admin_production
        from src.core.config import is_production

        with patch.dict(os.environ, {"PRODUCTION": "true"}):
            os.environ.pop("ENVIRONMENT", None)
            assert is_admin_production() == is_production() is True
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}):
            os.environ.pop("PRODUCTION", None)
            assert is_admin_production() == is_production() is True
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENVIRONMENT", None)
            os.environ.pop("PRODUCTION", None)
            assert is_admin_production() == is_production() is False


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

    def test_production_targeting_ignores_future_nested_field(self):
        """The concrete Targeting model preserves the production compatibility policy."""
        code = (
            "from src.core.schemas import Targeting; "
            "targeting = Targeting.model_validate({'future_dimension': ['value']}); "
            "assert targeting.model_dump(exclude_none=True) == {}"
        )
        env = {**os.environ, "ENVIRONMENT": "production"}

        result = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr

    def test_development_targeting_ignores_future_nested_field(self):
        """AdCP Targeting extensions are accepted consistently in strict development."""
        from src.core.schemas import Targeting

        targeting = Targeting.model_validate({"future_dimension": ["value"]})

        assert targeting.model_dump(exclude_none=True) == {}
