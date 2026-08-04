"""Sandbox accounts must never reach a real ad-server adapter.

AdCP 3.1.1 ``dist/docs/3.1.1/media-buy/advanced-topics/sandbox.mdx`` §Seller
implementation, for any request referencing a sandbox account:

    - MUST NOT make real ad platform API calls (no real orders, line items, etc.)
    - MUST NOT charge real money or create real billing records
    - MUST return realistic response shapes with simulated data

Before this fix a ``sandbox: true`` create_media_buy produced a byte-identical outbound
call to the live path (verified against a real Kevel tenant), because adapters are
selected per-tenant and the account never reached ``get_adapter``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.adapters.mock_ad_server import MockAdServer as MockAdServerAdapter
from src.core.helpers.adapter_helpers import get_adapter
from tests.factories.principal import PrincipalFactory


@pytest.fixture
def kevel_tenant() -> MagicMock:
    """A tenant whose real ad server is Kevel — the live-dispatch configuration."""
    tenant = MagicMock()
    tenant.tenant_id = "tenant_acme"
    tenant.ad_server = "kevel"
    return tenant


@pytest.fixture
def principal() -> MagicMock:
    principal = MagicMock()
    principal.principal_id = "principal_1"
    principal.platform_mappings = {}
    return principal


class TestSandboxForcesMockAdapter:
    def test_sandbox_account_gets_mock_adapter_on_a_kevel_tenant(self, kevel_tenant, principal) -> None:
        """The whole defect in one assertion: sandbox must not select the tenant's real adapter."""
        adapter = get_adapter(principal, dry_run=False, tenant=kevel_tenant, sandbox=True)

        assert isinstance(adapter, MockAdServerAdapter), (
            f"sandbox account selected {type(adapter).__name__}; a sandbox request must never "
            "reach a real ad-platform adapter (sandbox.mdx §Seller implementation)"
        )

    def test_sandbox_short_circuits_before_reading_adapter_config(self, kevel_tenant, principal) -> None:
        """Sandbox must not even load the real adapter's credentials.

        Pins the *ordering*, not just the outcome: returning mock after fetching Kevel's
        API key would still satisfy the test above while touching live configuration.
        """
        with patch("src.core.helpers.adapter_helpers.get_db_session") as mock_session:
            adapter = get_adapter(principal, dry_run=False, tenant=kevel_tenant, sandbox=True)

        assert isinstance(adapter, MockAdServerAdapter)
        mock_session.assert_not_called()

    def test_live_account_still_selects_the_tenant_adapter(self, kevel_tenant, principal) -> None:
        """Negative control: without sandbox the tenant's configured adapter is still chosen.

        Without this, a fix that returned mock unconditionally would pass the suite while
        silently disabling every real campaign.
        """
        with patch("src.core.helpers.adapter_helpers.get_db_session") as mock_session:
            mock_session.return_value.__enter__.return_value = MagicMock()
            get_adapter(principal, dry_run=False, tenant=kevel_tenant, sandbox=False)

        # The live path opens a session to read AdapterConfig; the sandbox path never does.
        mock_session.assert_called_once_with()


class TestSandboxRidesOnIdentity:
    def test_identity_defaults_to_live(self) -> None:
        """An identity with no resolved account is live — sandbox suppresses side effects,
        so the safe default is the one that does not claim suppression."""
        assert PrincipalFactory.make_identity().sandbox is False

    def test_identity_is_immutable_but_copyable(self) -> None:
        """The transport funnel sets sandbox via model_copy on a frozen identity."""
        identity = PrincipalFactory.make_identity()
        updated = identity.model_copy(update={"account_id": "acc_sandbox", "sandbox": True})

        assert updated.sandbox is True
        assert identity.sandbox is False, "the original identity must not be mutated"


class TestResolvedAccountCarriesMode:
    """The flag was previously discarded at the one place it was known."""

    def test_null_sandbox_column_is_live(self) -> None:
        """Account.sandbox is nullable; NULL means live, matching AccountRepository's own
        ``sandbox IS NULL OR sandbox = False`` filter."""
        from src.core.helpers.account_helpers import ResolvedAccount

        assert ResolvedAccount("acc_1", bool(None)).sandbox is False
        assert ResolvedAccount("acc_2", bool(True)).sandbox is True
