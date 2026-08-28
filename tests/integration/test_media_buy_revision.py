"""The `revision` optimistic-concurrency token, enforced by the update flow.

The pinned update-media-buy-request.json says of `revision`:

    Expected current revision for optimistic concurrency. ... When provided, sellers
    MUST reject the update with CONFLICT if the media buy's current revision does not
    match, and MUST enforce that comparison atomically with the write.

These tests grade the impl layer. The wire-level assertions for all three transports
live in test_update_media_buy_revision_validation_wire.py.
"""

import pytest

from src.core.config_loader import set_current_tenant
from src.core.database.repositories import MediaBuyUoW
from src.core.exceptions import AdCPGoneError, AdCPRevisionConflictError
from src.core.resolved_identity import ResolvedIdentity
from src.core.schemas import UpdateMediaBuyRequest
from src.core.tools.media_buy_update import _update_media_buy_impl
from tests.factories import MediaBuyFactory, PrincipalFactory, TenantFactory

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

TENANT_ID = "test_revision_tenant"
PRINCIPAL_ID = "test_revision_principal"
TOKEN = "test_revision_token_abc"


@pytest.fixture
def revision_tenant(integration_db, bound_factory_session):
    """Tenant + principal, built by the shared factories.

    TenantFactory already provisions the USD CurrencyLimit that budget validation
    requires, so this must not create a second one.
    """
    tenant = TenantFactory(
        tenant_id=TENANT_ID,
        name="Revision Tenant",
        subdomain="revision-tenant",
        ad_server="mock",
    )
    principal = PrincipalFactory(
        tenant=tenant,
        principal_id=PRINCIPAL_ID,
        name="Revision Advertiser",
        access_token=TOKEN,
        platform_mappings={"mock": {"id": "adv_revision"}},
    )
    bound_factory_session.commit()

    set_current_tenant(
        {
            "tenant_id": TENANT_ID,
            "name": "Revision Tenant",
            "subdomain": "revision-tenant",
            "ad_server": "mock",
            "is_active": True,
        }
    )

    return tenant, principal


def _identity() -> ResolvedIdentity:
    return PrincipalFactory.make_identity(
        principal_id=PRINCIPAL_ID,
        tenant_id=TENANT_ID,
        auth_token=TOKEN,
    )


@pytest.fixture
def seed_media_buy(revision_tenant, bound_factory_session):
    """Return a helper that persists a media buy owned by the fixture's principal.

    The principal OBJECT is passed, not its id: MediaBuyFactory declares ``principal``
    as a SubFactory, so supplying only ``principal_id`` mints a SECOND principal and
    the buy ends up owned by someone the test never authenticates as.

    ``revision`` is a repository-managed seam field that ``MediaBuy.__init__`` refuses
    outright; the factory assigns it the way the repository does, so a test can start
    from a row in a state production actually reaches. Seeding it also makes the factory
    flush AFTER its own commit, which leaves this session holding an open transaction --
    and therefore a ROW LOCK. The compare-and-set under test takes SELECT ... FOR UPDATE
    on that row, so the commit below is required, not tidiness.
    """
    tenant, principal = revision_tenant

    def _seed(media_buy_id: str, *, status: str = "active", revision: int = 1):
        media_buy = MediaBuyFactory(
            tenant=tenant,
            principal=principal,
            media_buy_id=media_buy_id,
            status=status,
            revision=revision,
        )
        bound_factory_session.commit()
        return media_buy

    return _seed


def move_revision_on(media_buy_id: str) -> None:
    """Advance the revision the way any other writer would -- through the repository.

    Deliberately not another update_media_buy call: not every update action bumps the
    counter (a bare pause does not), so driving the setup through the tool would make
    the fixture depend on which action happens to write.
    """
    with MediaBuyUoW(TENANT_ID) as uow:
        uow.media_buys.update_fields(media_buy_id, order_name="moved on by another writer")


def read_revision(media_buy_id: str) -> int:
    """Read the COMMITTED revision through a fresh unit of work.

    Deliberately not the bound factory session: that one holds the row it wrote, so it
    would answer from its own identity map rather than from what the update actually
    persisted.
    """
    with MediaBuyUoW(TENANT_ID) as uow:
        return uow.media_buys.get_by_id_or_raise(media_buy_id).revision


class TestRevisionEnforcedByUpdateFlow:
    def test_matching_token_succeeds_and_advances_the_revision(self, seed_media_buy):
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

    def test_absent_token_still_updates(self, seed_media_buy):
        """revision is optional; omitting it skips the check rather than failing."""
        seed_media_buy("mb_rev_absent")
        result = _update_media_buy_impl(
            req=UpdateMediaBuyRequest(media_buy_id="mb_rev_absent", paused=True),
            identity=_identity(),
        )
        assert result.response.media_buy_id == "mb_rev_absent"

    def test_stale_token_is_rejected_with_conflict_naming_both_versions(self, seed_media_buy):
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

    def test_rejected_update_is_not_applied(self, seed_media_buy):
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

    def test_stale_token_on_a_terminal_buy_yields_conflict_not_gone(self, seed_media_buy):
        """Order matters: the CONFLICT check runs BEFORE the terminal-state gate.

        A buyer holding a stale token against a completed buy has a stale-token
        problem first. CONFLICT names both versions and says "re-read and retry";
        the terminal answer (INVALID_STATE via AdCPGoneError) hides the version pair
        and misdescribes the cause.
        """
        seed_media_buy("mb_rev_terminal", status="completed", revision=4)

        with pytest.raises(AdCPRevisionConflictError) as exc_info:
            _update_media_buy_impl(
                req=UpdateMediaBuyRequest(media_buy_id="mb_rev_terminal", paused=True, revision=1),
                identity=_identity(),
            )
        assert exc_info.value.details["current_version"] == 4

    def test_terminal_gate_still_applies_when_the_token_matches(self, seed_media_buy):
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
