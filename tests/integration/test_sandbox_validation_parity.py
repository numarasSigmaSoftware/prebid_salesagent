"""Sandbox requests are validated against the TENANT's declared adapter, not the simulator.

The sandbox short-circuit in ``get_adapter`` substitutes ``MockAdServer`` so no real
ad-platform call is ever made. That substitution is correct for EXECUTION and wrong for
CONSTRAINT DECLARATION: the simulator can simulate all seven AdCP pricing models, so a
sandbox buy read its supported-pricing answer off the simulator and accepted ``cpcv`` on a
tenant whose real ad server rejects it.

AdCP 3.1.1 ``dist/docs/3.1.1/media-buy/advanced-topics/sandbox.mdx``, §Seller
implementation (:231):

    - **MUST** validate inputs the same way as production (reject invalid budgets, bad
      dates, etc.)

and §Protocol compliance (:243): "Apply normal input validation (sandbox does not bypass
validation)."

So: execution goes to the mock, constraints come from the adapter the tenant DECLARED.
Both production readers of ``get_supported_pricing_models()`` — the pre-create validation
at ``media_buy_create.py`` and the ``pricing_options[].supported`` annotation in
``products.py`` — ask the adapter instance ``get_adapter`` returned, so both are graded
here through that one seam.

Kevel is the declared adapter for the live-vs-sandbox parity comparison because
``get_adapter`` can build a real ``Kevel`` in dry-run mode without credentials, which lets
the sandbox error list be compared against the LIVE adapter's own error list rather than
against a string this test made up. GAM is used where only the declaration matters, since
building one requires a network code.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from src.adapters.google_ad_manager import GoogleAdManager
from src.adapters.kevel import Kevel
from src.adapters.mock_ad_server import MockAdServer
from src.core.helpers.adapter_helpers import get_adapter
from src.core.schemas import (
    CreateMediaBuyRequest,
    FormatId,
    MediaPackage,
    PackageRequest,
    Principal,
)
from tests.factories import (
    AccountFactory,
    AgentAccountAccessFactory,
    PricingOptionFactory,
    ProductFactory,
)
from tests.harness.product import ProductEnv
from tests.harness.transport import Transport

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

_FORMAT = FormatId(agent_url="https://creative.test", id="display_300x250")


def _principal() -> Principal:
    """A principal that can build any adapter get_adapter dispatches to."""
    return Principal(
        principal_id="p_sandbox_parity",
        name="Sandbox Parity Buyer",
        # Kevel's __init__ requires an advertiser id even in dry-run; the mock reads its
        # own key for HITL config and tolerates its absence.
        platform_mappings={"kevel": {"advertiser_id": "kevel-adv-1"}},
    )


def _adapter_for(ad_server: str, *, sandbox: bool):
    """Build the adapter get_adapter returns for a tenant declaring *ad_server*.

    Uses the dict-shaped tenant get_adapter already supports, so no tenant row is
    needed — the only DB touch on the live path is the AdapterConfig lookup, which
    returns nothing for an absent tenant and leaves tenant.ad_server as the selection.
    """
    return get_adapter(
        _principal(),
        dry_run=True,
        tenant={"tenant_id": "t_sandbox_parity", "ad_server": ad_server},
        sandbox=sandbox,
    )


def _request() -> CreateMediaBuyRequest:
    """A request that is valid on every axis EXCEPT the pricing model under test.

    Future dates and a mid-range budget matter: the mock layers GAM-like date, goal and
    budget checks on top of the shared pricing check, and those extra errors would mask
    what this module is grading.
    """
    start = datetime.now(UTC) + timedelta(days=1)
    return CreateMediaBuyRequest(
        brand={"domain": "sandbox-parity.example.com"},
        idempotency_key="sandbox-parity-key-0001",
        start_time=start,
        end_time=start + timedelta(days=30),
        packages=[
            PackageRequest(
                product_id="prod_parity",
                budget=5000.0,
                pricing_option_id="parity_pricing",
                format_ids=[_FORMAT],
            )
        ],
    )


def _packages() -> list[MediaPackage]:
    return [
        MediaPackage(
            package_id="pkg_parity",
            name="Parity Package",
            delivery_type="guaranteed",
            cpm=5.0,
            impressions=10000,
            format_ids=[_FORMAT],
            product_id="prod_parity",
        )
    ]


def _validate(adapter, pricing_model: str) -> list[str]:
    """Run the exact pre-create validation ``_create_media_buy_impl`` runs.

    ``start_time``/``end_time`` are passed as resolved datetimes, not read back off the
    request: ``req.start_time`` is a ``StartTiming`` union, and ``_create_media_buy_impl``
    resolves it to a datetime before handing it to the adapter.
    """
    start = datetime.now(UTC) + timedelta(days=1)
    end = start + timedelta(days=30)
    pricing_info: dict[str, dict[str, Any]] = {
        "pkg_parity": {"pricing_model": pricing_model, "rate": 5.0, "currency": "USD", "is_fixed": True}
    }
    return adapter.validate_media_buy_request(_request(), _packages(), start, end, pricing_info)


class TestSandboxDeclaresTheTenantsAdapterConstraints:
    """``get_supported_pricing_models()`` answers for the declared ad server."""

    def test_gam_tenant_sandbox_declares_gams_models(self, integration_db):
        """A sandbox buyer on a GAM tenant may buy exactly what GAM can execute.

        Reads the expected set off ``GoogleAdManager`` rather than restating it, because
        the obligation is "the DECLARED adapter's models", not a particular four — but
        the ``cpcv`` assertion below pins the concrete bite, so a declaration that
        silently widened to the simulator's seven cannot pass.
        """
        adapter = _adapter_for("google_ad_manager", sandbox=True)

        assert adapter.get_supported_pricing_models() == set(GoogleAdManager.supported_pricing_models)
        assert "cpcv" not in adapter.get_supported_pricing_models(), (
            "the simulator supports cpcv and GAM does not — a sandbox buy on a GAM tenant "
            "must not be told it may buy cpcv"
        )
        assert "cpm" in adapter.get_supported_pricing_models()

    def test_sandbox_still_executes_on_the_simulator(self, integration_db):
        """Constraint declaration moved; execution routing did NOT.

        The whole point of the short-circuit is that a sandbox request never reaches a
        real ad platform (AdCP 3.1.1 sandbox.mdx: "MUST NOT make real ad platform API
        calls"). If reading the declared adapter's constraints ever turned into building
        the declared adapter, this reddens.
        """
        assert isinstance(_adapter_for("google_ad_manager", sandbox=True), MockAdServer)
        assert isinstance(_adapter_for("kevel", sandbox=True), MockAdServer)

    def test_mock_tenant_sandbox_keeps_the_simulators_full_range(self, integration_db):
        """Negative control: the fix must not narrow tenants that really declared mock.

        Without this, "always answer {cpm}" would pass every other test in this class.
        """
        adapter = _adapter_for("mock", sandbox=True)

        assert adapter.get_supported_pricing_models() == set(MockAdServer.supported_pricing_models)
        assert _validate(adapter, "cpcv") == []

    @pytest.mark.parametrize(
        "ad_server",
        ["creative_engine", "not_a_real_adapter", ""],
        ids=["registered-but-not-an-ad-server", "unregistered", "empty"],
    )
    def test_unresolvable_ad_server_declares_the_simulators_models(self, integration_db, ad_server):
        """Mirror live, which runs these on the simulator too.

        ``get_adapter``'s final else-branch falls back to the mock for any adapter type
        it cannot dispatch, so declaring anything narrower here would make sandbox
        STRICTER than the production it mirrors — the opposite failure, but still a
        parity break.
        """
        adapter = _adapter_for(ad_server, sandbox=True)

        assert adapter.get_supported_pricing_models() == set(MockAdServer.supported_pricing_models)


class TestSandboxRejectsWhatTheDeclaredAdapterCannotExecute:
    """The pre-create validation ``media_buy_create`` runs rejects identically."""

    def test_sandbox_rejects_a_model_the_declared_adapter_cannot_execute(self, integration_db):
        errors = _validate(_adapter_for("kevel", sandbox=True), "cpcv")

        assert errors, "a sandbox buy for cpcv on a Kevel tenant must be rejected, not accepted"
        assert any("cpcv" in e for e in errors), errors

    def test_sandbox_rejection_is_the_live_adapters_own_error(self, integration_db):
        """Parity is graded against the LIVE adapter's error list, not a literal.

        ``get_adapter(..., sandbox=False)`` on this tenant builds a real ``Kevel``; the
        same request is validated by both and the two lists must be equal. This is the
        "with the same error a live request gets" half of the obligation — a fix that
        rejected cpcv with a different message or a different supported-models list
        would satisfy the test above and fail this one.
        """
        live = _adapter_for("kevel", sandbox=False)
        sandboxed = _adapter_for("kevel", sandbox=True)
        assert isinstance(live, Kevel), "this test is only meaningful against a real live adapter"

        live_errors = _validate(live, "cpcv")
        sandbox_errors = _validate(sandboxed, "cpcv")

        assert live_errors, "precondition: the live adapter must itself reject cpcv"
        assert sandbox_errors == live_errors

    def test_sandbox_accepts_a_model_the_declared_adapter_can_execute(self, integration_db):
        """Negative control: the constraint profile rejects by CONTENT, not always."""
        assert _validate(_adapter_for("kevel", sandbox=True), "cpm") == []
        assert _validate(_adapter_for("google_ad_manager", sandbox=True), "vcpm") == []


class TestSandboxCatalogAnnotatesTheDeclaredAdaptersSupport:
    """``get_products`` annotates ``pricing_options[].supported`` from the same seam.

    ``products.py`` reads ``get_supported_pricing_models()`` off the adapter
    ``get_adapter`` returned, so before this change a sandbox account received a catalog
    advertising pricing its tenant's ad server cannot execute — the buyer would then be
    rejected at create time for a product the catalog said was fine.

    Driven over a real transport rather than IMPL: ``identity.sandbox`` is set by
    ``enrich_identity_with_account`` at the MCP/A2A/REST boundary, which the in-process
    IMPL path does not run.
    """

    @staticmethod
    def _annotated_options() -> dict[str, dict[str, Any]]:
        """Catalog a sandbox account gets for a GAM tenant, keyed by pricing model.

        The product carries one model GAM CAN execute and one it cannot, so the two
        annotations in a single response are the control for each other: "annotate
        everything unsupported" fails on cpm, "annotate everything supported" fails on
        cpcv.
        """
        # ad_server is declared in BOTH places on purpose: the DB row is what a real
        # deployment reads, while the harness identity carries its own tenant dict built
        # by TenantFactory.make_tenant (whose ad_server defaults to "mock"), and it is
        # identity.tenant that reaches get_adapter.
        with ProductEnv(tenant_id="tgpsbxcap", principal_id="pgpsbxcap", ad_server="google_ad_manager") as env:
            tenant, principal = env.setup_default_data(ad_server="google_ad_manager")
            env.set_policy_approved()
            env.set_ranking_disabled()

            product = ProductFactory(tenant=tenant, product_id="prod-gp-sbx-cap")
            PricingOptionFactory(product=product, pricing_model="cpm")
            PricingOptionFactory(product=product, pricing_model="cpcv")

            AccountFactory(tenant=tenant, account_id="acc_gp_sbx", sandbox=True)
            AgentAccountAccessFactory(
                tenant_id=tenant.tenant_id,
                principal_id=principal.principal_id,
                account_id="acc_gp_sbx",
            )

            result = env.call_via(
                Transport.REST,
                brief="video ads",
                account={"account_id": "acc_gp_sbx"},
            )

        assert not result.is_error, f"dispatch failed: {result.error!r}"
        assert result.wire_response is not None, "REST must capture a real wire response"
        products = result.wire_response["products"]
        assert len(products) == 1, products
        options = {opt["pricing_model"]: opt for opt in products[0]["pricing_options"]}
        assert set(options) == {"cpm", "cpcv"}, options
        return options

    def test_sandbox_catalog_marks_cpcv_unsupported_on_a_gam_tenant(self, integration_db):
        options = self._annotated_options()

        assert options["cpcv"]["supported"] is False, (
            "the tenant declared GAM, which cannot execute cpcv — a sandbox catalog that "
            "advertises it hands the buyer a product create_media_buy will reject"
        )
        assert "CPCV" in options["cpcv"]["unsupported_reason"]
        assert options["cpm"]["supported"] is True, (
            "cpm is within GAM's declared models — annotating it unsupported would be the opposite failure"
        )
