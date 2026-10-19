"""Internal adapter/repository contracts have no wire; lifecycle coverage lives in BDD.

AdCP 3.1.1 sandbox.mdx requires normal validation and simulated execution.
Vendor-call suppression is ungraded by the inspected upstream storyboards.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.adapters.base import AdapterCreateRequest
from src.adapters.google_ad_manager import GoogleAdManager
from src.adapters.mock_ad_server import MockAdServer
from src.adapters.sandbox import SandboxAdServerAdapter
from src.core.exceptions import AdCPMediaBuyNotFoundError
from src.core.helpers.adapter_helpers import get_adapter
from src.core.resolved_identity import identity_of
from src.core.schemas import MediaPackage, ReportingPeriod
from tests.harness.sandbox_adapter import SandboxAdapterEnv

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


def test_sandbox_factory_works_without_live_credentials(integration_db):
    with SandboxAdapterEnv() as env:
        tenant, principal = env.seed_configuration()
        configuration_reads = env.observe_configuration_reads()
        adapter = get_adapter(identity_of(tenant, principal, "sandbox"))
        assert isinstance(adapter, SandboxAdServerAdapter)
        assert adapter.configured_adapter_type == "google_ad_manager"
        assert adapter.manual_approval_required is True
        assert len(configuration_reads) == 1
        assert all(
            secret_column not in configuration_reads[0]
            for secret_column in ("gam_refresh_token", "gam_service_account_json", "kevel_api_key", "triton_api_key")
        )
        assert env.mock["gam_client"].call_count == 0
        assert env.mock["gam_oauth"].call_count == 0


@pytest.mark.parametrize(
    "adapter_type,approval,expected",
    [
        ("google_ad_manager", True, True),
        ("google_ad_manager", False, False),
        ("mock", True, True),
        ("mock", False, False),
        ("kevel", True, True),
        ("kevel", False, False),
        ("triton", True, False),
    ],
)
def test_sandbox_preserves_configured_approval(integration_db, adapter_type, approval, expected):
    with SandboxAdapterEnv() as env:
        tenant, principal = env.seed_configuration(adapter_type, approval)
        adapter = get_adapter(identity_of(tenant, principal, "sandbox"))
        assert isinstance(adapter, SandboxAdServerAdapter)
        assert adapter.manual_approval_required is expected
        assert adapter.configured_adapter_type == adapter_type


@pytest.mark.parametrize("adapter_type,adapter_class", [("google_ad_manager", GoogleAdManager), ("mock", MockAdServer)])
@pytest.mark.parametrize("approval", [True, False])
def test_live_control_keeps_configured_adapter_and_policy(integration_db, adapter_type, adapter_class, approval):
    with SandboxAdapterEnv() as env:
        tenant, principal = env.seed_configuration(adapter_type, approval, credentials=True)
        live = get_adapter(identity_of(tenant, principal, "live"))
        sandbox = get_adapter(identity_of(tenant, principal, "sandbox"))
        assert isinstance(live, adapter_class)
        assert live.manual_approval_required is sandbox.manual_approval_required
        assert env.mock["gam_client"].call_count == (1 if adapter_type == "google_ad_manager" else 0)
        assert env.mock["gam_oauth"].call_count == (1 if adapter_type == "google_ad_manager" else 0)


def test_missing_configuration_retains_tenant_fallback_and_no_approval_default(integration_db):
    with SandboxAdapterEnv() as env:
        tenant, principal = env.seed_configuration(tenant_adapter="mock", configured=False)
        sandbox = get_adapter(identity_of(tenant, principal, "sandbox"))
        live = get_adapter(identity_of(tenant, principal, "live"))
        assert sandbox.configured_adapter_type == "mock"
        assert sandbox.manual_approval_required is live.manual_approval_required is False


@pytest.mark.parametrize(
    "adapter_type,pricing,expected",
    [
        ("google_ad_manager", "cpm", []),
        ("google_ad_manager", "cpcv", [("UNSUPPORTED_FEATURE", "packages[0].pricing_option_id")]),
        ("mock", "cpm", [("VALIDATION_ERROR", "packages[0].impressions"), ("VALIDATION_ERROR", "budget")]),
    ],
)
def test_validation_uses_configured_rules_without_vendor_instance(integration_db, adapter_type, pricing, expected):
    with SandboxAdapterEnv() as env:
        tenant, principal = env.seed_configuration(adapter_type, credentials=True)
        adapter = get_adapter(identity_of(tenant, principal, "sandbox"))
        start = datetime.now(UTC) + timedelta(days=1)
        package = MediaPackage(
            package_id="package",
            name="Package",
            delivery_type="guaranteed",
            cpm=10,
            impressions=2_000_000,
            budget=2_000_000,
            format_ids=[],
        )
        problems = adapter.validate_media_buy_request(
            AdapterCreateRequest(total_budget=2_000_000),
            [package],
            start,
            start + timedelta(days=7),
            {"package": {"pricing_model": pricing}},
        )
        assert [(str(p.code), p.field) for p in problems] == expected
        assert env.mock["gam_client"].call_count == 0
        assert env.mock["gam_oauth"].call_count == 0

        live = get_adapter(identity_of(tenant, principal, "live"))
        live_problems = live.validate_media_buy_request(
            AdapterCreateRequest(total_budget=2_000_000),
            [package],
            start,
            start + timedelta(days=7),
            {"package": {"pricing_model": pricing}},
        )
        assert [(str(p.code), p.field) for p in live_problems] == expected


def test_synthetic_ids_do_not_alias_purchase_order_text(integration_db):
    with SandboxAdapterEnv() as env:
        tenant, principal = env.seed_configuration()
        start = datetime.now(UTC) + timedelta(days=1)
        request = AdapterCreateRequest(total_budget=100, po_number="same [REJECT]")
        first = get_adapter(identity_of(tenant, principal, "sandbox")).create_media_buy(
            request, [], start, start + timedelta(days=7)
        )
        second = get_adapter(identity_of(tenant, principal, "sibling")).create_media_buy(
            request, [], start, start + timedelta(days=7)
        )
        assert first.media_buy_id.startswith("sandbox_")
        assert first.media_buy_id != second.media_buy_id


@pytest.mark.parametrize("seeded", [False, True])
def test_fresh_adapter_reads_only_owning_account_state(integration_db, seeded):
    with SandboxAdapterEnv() as env:
        tenant, principal = env.seed_configuration()
        env.stored_buy(status="active")
        if seeded:
            env.seed_delivery()
        date_range = ReportingPeriod(start="2026-10-01T00:00:00Z", end="2026-10-10T00:00:00Z")
        now = datetime.now(UTC)
        owner = get_adapter(identity_of(tenant, principal, "sandbox"))
        delivery = owner.get_media_buy_delivery("stored", date_range, now)
        assert (delivery.totals.impressions, delivery.totals.spend) == ((4321, 43.21) if seeded else (0, 0))
        assert [package.package_id for package in delivery.by_package] == ["package"]
        assert owner.check_media_buy_status("stored", now).status == "active"
        snapshot = owner.get_packages_snapshot([("stored", "package", None)])["stored"]["package"]
        assert (snapshot.impressions, snapshot.spend) == ((4321, 43.21) if seeded else (0, 0))
        sibling = get_adapter(identity_of(tenant, principal, "sibling"))
        with pytest.raises(AdCPMediaBuyNotFoundError):
            sibling.get_media_buy_delivery("stored", date_range, now)
        assert sibling.get_packages_snapshot([("stored", "package", None)]) == {"stored": {"package": None}}
        assert owner.get_packages_snapshot([("stored", "missing-package", None)]) == {
            "stored": {"missing-package": None}
        }


def test_missing_buy_is_refused_even_with_seeded_delivery(integration_db):
    with SandboxAdapterEnv() as env:
        tenant, principal = env.seed_configuration()
        env.seed_delivery("missing")
        adapter = get_adapter(identity_of(tenant, principal, "sandbox"))
        period = ReportingPeriod(start="2026-10-01T00:00:00Z", end="2026-10-10T00:00:00Z")
        with pytest.raises(AdCPMediaBuyNotFoundError):
            adapter.get_media_buy_delivery("missing", period, datetime.now(UTC))


def test_repository_reads_preserve_callers_pending_domain_changes(integration_db):
    with SandboxAdapterEnv() as env:
        tenant, principal = env.seed_configuration()
        env.stored_buy(status="active")
        assert env.persist_through_adapter_reads(identity_of(tenant, principal, "sandbox")) == "Survived adapter reads"


def test_sandbox_execution_returns_package_and_creative_results_without_domain_writes(integration_db):
    with SandboxAdapterEnv() as env:
        tenant, principal = env.seed_configuration()
        env.stored_buy(status="active")
        adapter = get_adapter(identity_of(tenant, principal, "sandbox"))
        now = datetime.now(UTC)
        package = MediaPackage(
            package_id="package", name="P", delivery_type="guaranteed", cpm=10, impressions=1000, format_ids=[]
        )
        created = adapter.create_media_buy(
            AdapterCreateRequest(total_budget=100), [package], now, now + timedelta(days=1)
        )
        assert [package.package_id for package in created.packages] == ["package"]
        assert created.platform_line_item_ids["package"].startswith("sandbox_line_")
        assets = adapter.add_creative_assets("stored", [{"id": "creative", "name": "[REJECT]"}], now)
        assert [(asset.asset_id, asset.status) for asset in assets] == [("creative", "approved")]
        assert assets[0].creative_id.startswith("sandbox_creative_")
        assert adapter.associate_creatives(["line"], [assets[0].creative_id]) == [
            {"line_item_id": "line", "creative_id": assets[0].creative_id, "status": "success"}
        ]
        result = adapter.update_media_buy("stored", "update_package_budget", "package", 999, now)
        assert (result.media_buy_id, result.affected_packages) == ("stored", [])
        assert env.package_budget("stored", "package") == 100
        assert env.mock["gam_client"].call_count == 0
        assert env.mock["gam_oauth"].call_count == 0


@pytest.mark.parametrize(
    "tenant_scope,principal_scope,account_scope",
    [
        ("foreign-tenant", "test_principal", "sandbox"),
        ("test_tenant", "foreign-principal", "sandbox"),
        ("test_tenant", "test_principal", "sibling"),
    ],
)
def test_scoped_repository_lookup_requires_every_ownership_dimension(
    integration_db,
    tenant_scope,
    principal_scope,
    account_scope,
):
    from src.core.database.repositories.uow import MediaBuyUoW

    with SandboxAdapterEnv() as env:
        env.seed_configuration()
        env.stored_buy()
        with MediaBuyUoW(tenant_scope) as uow:
            assert uow.media_buys.get_by_id_for_account("stored", principal_scope, account_scope) is None


def test_configuration_row_precedes_gam_tenant_fallback(integration_db):
    with SandboxAdapterEnv() as env:
        tenant, principal = env.seed_configuration("mock", False, tenant_adapter="google_ad_manager")
        sandbox = get_adapter(identity_of(tenant, principal, "sandbox"))
        live = get_adapter(identity_of(tenant, principal, "live"))
        assert sandbox.configured_adapter_type == "mock"
        assert isinstance(live, MockAdServer)
        assert sandbox.manual_approval_required is live.manual_approval_required is False
