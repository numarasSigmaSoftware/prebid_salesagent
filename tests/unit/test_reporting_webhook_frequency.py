"""Reporting webhook frequencies are rejected before create/update persistence."""

from types import SimpleNamespace

import pytest

from src.core.exceptions import AdCPCapabilityNotSupportedError
from src.core.tools._reporting_webhook import validate_reporting_webhook_frequency
from src.core.tools.media_buy_create import _create_media_buy_impl
from src.core.tools.media_buy_update import _update_media_buy_impl
from tests.factories.principal import PrincipalFactory


@pytest.mark.parametrize("frequency", ["hourly", "monthly"])
def test_shared_validator_rejects_unsupported_frequency(frequency):
    webhook = SimpleNamespace(reporting_frequency=frequency)

    with pytest.raises(AdCPCapabilityNotSupportedError, match=frequency):
        validate_reporting_webhook_frequency(webhook)


def test_shared_validator_accepts_daily():
    validate_reporting_webhook_frequency(SimpleNamespace(reporting_frequency="daily"))


@pytest.mark.asyncio
async def test_create_rejects_after_auth_but_before_database_work():
    request = SimpleNamespace(
        reporting_webhook=SimpleNamespace(reporting_frequency="hourly"),
        context=None,
    )
    identity = PrincipalFactory.make_identity(principal_id="p1", tenant_id="t1")

    with pytest.raises(AdCPCapabilityNotSupportedError, match="hourly"):
        await _create_media_buy_impl(request, identity=identity)


def test_update_rejects_after_auth_but_before_database_work():
    request = SimpleNamespace(
        reporting_webhook=SimpleNamespace(reporting_frequency="hourly"),
        context=None,
    )
    identity = PrincipalFactory.make_identity(principal_id="p1", tenant_id="t1")

    with pytest.raises(AdCPCapabilityNotSupportedError, match="hourly"):
        _update_media_buy_impl(request, identity=identity)
