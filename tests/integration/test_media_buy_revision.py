"""The `revision` optimistic-concurrency token, enforced by the update flow.

The pinned update-media-buy-request.json says of `revision`:

    Expected current revision for optimistic concurrency. ... When provided, sellers
    MUST reject the update with CONFLICT if the media buy's current revision does not
    match, and MUST enforce that comparison atomically with the write.

These tests grade the impl layer. The wire-level assertions for all three transports
live in test_update_media_buy_revision_validation_wire.py.
"""

from datetime import date, timedelta

import pytest

from src.core.config_loader import set_current_tenant
from src.core.database.database_session import get_db_session
from src.core.database.models import CurrencyLimit, MediaBuy, Tenant
from src.core.database.models import Principal as ModelPrincipal
from src.core.database.repositories import MediaBuyUoW
from src.core.exceptions import AdCPGoneError, AdCPRevisionConflictError
from src.core.resolved_identity import ResolvedIdentity
from src.core.schemas import UpdateMediaBuyRequest
from src.core.tools.media_buy_update import _update_media_buy_impl

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

TENANT_ID = "test_revision_tenant"
PRINCIPAL_ID = "test_revision_principal"
TOKEN = "test_revision_token_abc"


@pytest.fixture
def revision_tenant(integration_db):
    """Tenant + principal + USD currency limit, torn down after each test."""
    with get_db_session() as session:
        session.add(
            Tenant(
                tenant_id=TENANT_ID,
                name="Revision Tenant",
                subdomain="revision-tenant",
                ad_server="mock",
                is_active=True,
                human_review_required=False,
                auto_approve_format_ids=[],
                policy_settings={},
            )
        )
        session.add(
            ModelPrincipal(
                tenant_id=TENANT_ID,
                principal_id=PRINCIPAL_ID,
                name="Revision Advertiser",
                access_token=TOKEN,
                platform_mappings={"mock": {"id": "adv_revision"}},
            )
        )
        session.add(
            CurrencyLimit(
                tenant_id=TENANT_ID,
                currency_code="USD",
                max_daily_package_spend=10000.0,
            )
        )
        session.commit()

    set_current_tenant(
        {
            "tenant_id": TENANT_ID,
            "name": "Revision Tenant",
            "subdomain": "revision-tenant",
            "ad_server": "mock",
            "is_active": True,
        }
    )

    yield TENANT_ID

    with get_db_session() as session:
        session.query(MediaBuy).filter_by(tenant_id=TENANT_ID).delete()
        session.query(CurrencyLimit).filter_by(tenant_id=TENANT_ID).delete()
        session.query(ModelPrincipal).filter_by(tenant_id=TENANT_ID).delete()
        session.query(Tenant).filter_by(tenant_id=TENANT_ID).delete()
        session.commit()


def _identity() -> ResolvedIdentity:
    return ResolvedIdentity(
        principal_id=PRINCIPAL_ID,
        tenant_id=TENANT_ID,
        tenant={"tenant_id": TENANT_ID},
        auth_token=TOKEN,
        protocol="mcp",
    )


def seed_media_buy(media_buy_id: str, *, status: str = "active") -> None:
    """Persist a media buy at revision 1 (the column default)."""
    today = date.today()
    with get_db_session() as session:
        session.add(
            MediaBuy(
                tenant_id=TENANT_ID,
                principal_id=PRINCIPAL_ID,
                media_buy_id=media_buy_id,
                order_name="Revision Order",
                advertiser_name="Revision Advertiser",
                status=status,
                start_date=today,
                end_date=today + timedelta(days=30),
                start_time=today,
                end_time=today + timedelta(days=30),
                budget=1000.0,
                currency="USD",
                raw_request={},
            )
        )
        session.commit()


def move_revision_on(media_buy_id: str) -> None:
    """Advance the revision the way any other writer would -- through the repository.

    Deliberately not another update_media_buy call: not every update action bumps the
    counter (a bare pause does not), so driving the setup through the tool would make
    the fixture depend on which action happens to write.
    """
    with MediaBuyUoW(TENANT_ID) as uow:
        uow.media_buys.update_fields(media_buy_id, order_name="moved on by another writer")


def read_revision(media_buy_id: str) -> int:
    with get_db_session() as session:
        row = session.query(MediaBuy).filter_by(tenant_id=TENANT_ID, media_buy_id=media_buy_id).one()
        return row.revision


class TestRevisionEnforcedByUpdateFlow:
    def test_matching_token_succeeds_and_advances_the_revision(self, revision_tenant):
        seed_media_buy("mb_rev_match")
        before = read_revision("mb_rev_match")

        result = _update_media_buy_impl(
            req=UpdateMediaBuyRequest(media_buy_id="mb_rev_match", paused=True, revision=before),
            identity=_identity(),
        )

        assert result.response.media_buy_id == "mb_rev_match"
        after = read_revision("mb_rev_match")
        assert after > before, "an honoured token must be spent -- the revision has to move"
        # The buyer is told the value it must send next, so the reported revision has to
        # be the persisted one, not the token it just sent.
        assert result.response.revision == after

    def test_absent_token_still_updates(self, revision_tenant):
        """revision is optional; omitting it skips the check rather than failing."""
        seed_media_buy("mb_rev_absent")
        result = _update_media_buy_impl(
            req=UpdateMediaBuyRequest(media_buy_id="mb_rev_absent", paused=True),
            identity=_identity(),
        )
        assert result.response.media_buy_id == "mb_rev_absent"

    def test_stale_token_is_rejected_with_conflict_naming_both_versions(self, revision_tenant):
        seed_media_buy("mb_rev_stale")
        # Someone else writes first, moving the revision past the buyer's token.
        move_revision_on("mb_rev_stale")
        current = read_revision("mb_rev_stale")
        assert current > 1

        with pytest.raises(AdCPRevisionConflictError) as exc_info:
            _update_media_buy_impl(
                req=UpdateMediaBuyRequest(media_buy_id="mb_rev_stale", paused=False, revision=1),
                identity=_identity(),
            )

        exc = exc_info.value
        assert exc.wire_error_code == "CONFLICT"
        assert exc.details == {
            "resource_id": "mb_rev_stale",
            "expected_version": 1,
            "current_version": current,
        }

    def test_rejected_update_is_not_applied(self, revision_tenant):
        """A CONFLICT must be a no-op, not a partial write."""
        seed_media_buy("mb_rev_noop")
        move_revision_on("mb_rev_noop")
        after_first = read_revision("mb_rev_noop")

        with pytest.raises(AdCPRevisionConflictError):
            _update_media_buy_impl(
                req=UpdateMediaBuyRequest(media_buy_id="mb_rev_noop", paused=False, revision=1),
                identity=_identity(),
            )

        assert read_revision("mb_rev_noop") == after_first

    def test_stale_token_on_a_terminal_buy_yields_conflict_not_gone(self, revision_tenant):
        """Order matters: the CONFLICT check runs BEFORE the terminal-state gate.

        A buyer holding a stale token against a completed buy has a stale-token
        problem first. CONFLICT names both versions and says "re-read and retry";
        the terminal answer (INVALID_STATE via AdCPGoneError) hides the version pair
        and misdescribes the cause.
        """
        seed_media_buy("mb_rev_terminal", status="completed")
        with get_db_session() as session:
            row = session.query(MediaBuy).filter_by(media_buy_id="mb_rev_terminal").one()
            row.revision = 4
            session.commit()

        with pytest.raises(AdCPRevisionConflictError) as exc_info:
            _update_media_buy_impl(
                req=UpdateMediaBuyRequest(media_buy_id="mb_rev_terminal", paused=True, revision=1),
                identity=_identity(),
            )
        assert exc_info.value.details["current_version"] == 4

    def test_terminal_gate_still_applies_when_the_token_matches(self, revision_tenant):
        """Running the CONFLICT check first must not disable the terminal gate.

        With a CURRENT token there is no conflict to report, so the buy's terminal
        state is the real answer and must still be given.
        """
        seed_media_buy("mb_rev_terminal_ok", status="completed")
        current = read_revision("mb_rev_terminal_ok")

        with pytest.raises(AdCPGoneError):
            _update_media_buy_impl(
                req=UpdateMediaBuyRequest(media_buy_id="mb_rev_terminal_ok", paused=True, revision=current),
                identity=_identity(),
            )
