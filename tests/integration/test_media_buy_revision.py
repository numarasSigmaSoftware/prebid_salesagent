"""The `revision` optimistic-concurrency token, enforced by the update flow.

The pinned update-media-buy-request.json says of `revision`:

    Expected current revision for optimistic concurrency. ... When provided, sellers
    MUST reject the update with CONFLICT if the media buy's current revision does not
    match, and MUST enforce that comparison atomically with the write.

and of the value it reports back, the pinned update-media-buy-response.json says:

    Revision number after this update.

These tests grade the impl layer, plus the one cross-transport pin that belongs with
the arithmetic it grades (``test_a_field_writing_update_emits_one_advance_on_every_wire``):
a request that supplies a token AND writes a field must advance the counter exactly
once, and every wire must carry that value. The rest of the wire-level assertions -- what
each transport accepts, and the conflict shape -- live in
test_update_media_buy_revision_validation_wire.py.
"""

import pytest

from src.core.config_loader import set_current_tenant
from src.core.database.repositories import MediaBuyUoW
from src.core.exceptions import AdCPGoneError, AdCPRevisionConflictError
from src.core.resolved_identity import ResolvedIdentity
from src.core.schemas import UpdateMediaBuyRequest
from src.core.tools.media_buy_update import _update_media_buy_impl
from tests.factories import MediaBuyFactory, PrincipalFactory, TenantFactory
from tests.harness.media_buy_dual import MediaBuyDualEnv
from tests.harness.transport import Transport

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


def _identity(*, dry_run: bool = False) -> ResolvedIdentity:
    return PrincipalFactory.make_identity(
        principal_id=PRINCIPAL_ID,
        tenant_id=TENANT_ID,
        auth_token=TOKEN,
        dry_run=dry_run,
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

    def test_a_field_writing_update_advances_the_revision_exactly_once(self, seed_media_buy):
        """Honouring a token must not cost the buyer a second revision, nor none.

        The response field is defined as "Revision number after this update" --
        singular -- and the buyer sends the reported value back as its next token.

        Graded against the SAME request without a token, not against a literal: the
        obligation is that supplying a token changes nothing about how far the
        counter moves, and a hardcoded 8 would still pass if both shapes moved two.

        This is the oracle for the UNDER-count. The update flow's nested
        ``get_db_session()`` blocks close the unit of work's own session mid-request,
        which can roll the compare-and-set's advance back; a suppression rule that
        merely remembers "already advanced" then declines the write's advance too and
        the token stands still, which this assertion catches at 0. The OVER-count --
        the compare-and-set and the write each advancing -- is graded by
        ``test_a_field_writing_update_emits_one_advance_on_every_wire`` below, which
        runs against a harness that does not close the session and so sees both.
        """
        seed_media_buy("mb_rev_untokened")
        untokened_before = read_revision("mb_rev_untokened")
        _update_media_buy_impl(
            req=UpdateMediaBuyRequest(media_buy_id="mb_rev_untokened", budget=9000.0),
            identity=_identity(),
        )
        untokened_advance = read_revision("mb_rev_untokened") - untokened_before

        seed_media_buy("mb_rev_tokened")
        before = read_revision("mb_rev_tokened")
        result = _update_media_buy_impl(
            req=UpdateMediaBuyRequest(media_buy_id="mb_rev_tokened", budget=9000.0, revision=before),
            identity=_identity(),
        )

        after = read_revision("mb_rev_tokened")
        assert after - before == untokened_advance == 1, (
            f"the untokened update advanced the revision by {untokened_advance} and the "
            f"tokened one by {after - before}; both must advance by exactly 1"
        )
        assert result.response.revision == after, (
            f"the response reported revision {result.response.revision} while the row holds "
            f"{after} -- the buyer's next token must be the value AFTER this update"
        )

    def test_a_dry_run_with_a_token_reports_the_current_revision_and_moves_nothing(self, seed_media_buy):
        """A simulation applies nothing -- including the token spend.

        The compare-and-set runs before the dry-run early return, deliberately: a
        simulated update that WOULD be rejected has to report the rejection. But the
        branch it precedes writes nothing, so an advance taken here would be the one
        persistent side effect of a request that promises none, and it would hand the
        buyer a token for a state the seller never entered.
        """
        seed_media_buy("mb_rev_dry")
        before = read_revision("mb_rev_dry")

        result = _update_media_buy_impl(
            req=UpdateMediaBuyRequest(media_buy_id="mb_rev_dry", budget=9000.0, revision=before),
            identity=_identity(dry_run=True),
        )

        assert read_revision("mb_rev_dry") == before, (
            f"the dry run moved the persisted revision to {read_revision('mb_rev_dry')}; a "
            f"simulation must leave the row exactly as it found it"
        )
        assert result.response.revision == before

    def test_a_dry_run_still_rejects_a_stale_token(self, seed_media_buy):
        """Not advancing must not become not comparing.

        A simulation of an update that would be rejected has to report the rejection,
        or dry_run becomes a way to get a 200 for a request the seller would refuse.
        """
        seed_media_buy("mb_rev_dry_stale")
        move_revision_on("mb_rev_dry_stale")

        with pytest.raises(AdCPRevisionConflictError) as exc_info:
            _update_media_buy_impl(
                req=UpdateMediaBuyRequest(media_buy_id="mb_rev_dry_stale", budget=9000.0, revision=1),
                identity=_identity(dry_run=True),
            )
        assert exc_info.value.details["expected_version"] == 1

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


#: A distinctive seed, so the assertion below discriminates the three wrong answers
#: this arithmetic can give: the ``UpdateMediaBuySuccess.revision`` schema default (1),
#: the pre-write value (7) and the double advance (9).
_WIRE_SEED_REVISION = 7

#: How each transport renders a JSON number. A2A carries its payload through a
#: protobuf Struct, whose only numeric type is ``double``, so it emits 8.0 where MCP
#: and REST emit 8 -- both conformant under draft-07 ``integer``. Mirrors the table in
#: test_update_media_buy_revision_validation_wire.py; the comparison below is by value,
#: so the fork does not need repeating here.
_WIRE_TRANSPORTS = [Transport.MCP, Transport.REST, Transport.A2A]


@pytest.mark.parametrize("transport", _WIRE_TRANSPORTS)
def test_a_field_writing_update_emits_one_advance_on_every_wire(integration_db, transport):
    """The post-write revision is a protocol contract, so it is graded on wire bytes.

    The impl-level pin above cannot see a boundary that re-serializes the field or
    substitutes the schema default, and the existing wire pin in
    test_update_media_buy_revision_validation_wire.py drives a bare pause -- which
    writes no row at all, so it never reaches the second advance that a field write
    used to take. This is the shape that did: token honoured, then a budget written.
    """
    media_buy_id = "mb_rev_wire_post_write"
    with MediaBuyDualEnv() as env:
        tenant, principal, _product, _pricing = env.setup_media_buy_data()
        MediaBuyFactory(
            tenant=tenant,
            principal=principal,
            media_buy_id=media_buy_id,
            status="active",
            revision=_WIRE_SEED_REVISION,
        )
        env._commit_factory_data()  # noqa: SLF001 — the harness's factory/session flush seam
        # REST builds its PUT URL from this attribute; leaving it unset points the
        # request at a buy that does not exist, which answers MEDIA_BUY_NOT_FOUND.
        env._seeded_media_buy_id = media_buy_id  # noqa: SLF001 — harness routing seam

        result = env.call_via(
            transport,
            media_buy_id=media_buy_id,
            budget=9000.0,
            revision=_WIRE_SEED_REVISION,
        )

    assert not result.is_error, f"{transport} rejected a matching token: {result.wire_error_envelope or result.error!r}"
    wire = result.wire_response
    assert wire is not None, f"{transport} captured no success wire body to grade"
    assert wire["revision"] == _WIRE_SEED_REVISION + 1, (
        f"{transport} emitted revision={wire['revision']!r} for an update of a buy at "
        f"{_WIRE_SEED_REVISION}. The pinned update-media-buy-response.json defines the field as "
        f"the revision AFTER this update, so it must be {_WIRE_SEED_REVISION + 1}: "
        f"{_WIRE_SEED_REVISION} is the pre-write value, {_WIRE_SEED_REVISION + 2} is the token "
        f"being spent twice (compare-and-set plus the write), and 1 is the schema default"
    )
