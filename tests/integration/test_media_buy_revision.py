"""Persisted media-buy revision counter on the production read path (AdCP 3.1.1 fields).

``revision`` is a persisted monotonic counter (``media_buys.revision``), not a
timestamp-derived value: two updates within the same second MUST still yield
strictly increasing revisions, because buyers treat the field as an
optimistic-concurrency token. ``confirmed_at`` on the create response and on
get_media_buys items both report the same persisted ``created_at``.

Full production paths against real PostgreSQL: _create_media_buy_impl →
_update_media_buy_impl → _get_media_buys_impl via the harness dual env.
"""

from __future__ import annotations

import pytest

from src.core.exceptions import AdCPGoneError
from src.core.schemas import UpdateMediaBuyRequest
from src.core.schemas._base import (
    CreateMediaBuySuccess,
    UpdateMediaBuySuccess,
)
from tests.harness.transport import Transport
from tests.helpers.media_buy import read_back_media_buy

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


def _create_buy(env, product) -> CreateMediaBuySuccess:
    """Drive a real create through the impl; returns the success response."""
    return env.create_default_buy(product, brand_domain="revision-test.example")


def _update_budget(env, media_buy_id: str, budget: float) -> UpdateMediaBuySuccess:
    """Drive a real budget update through the impl; returns the success response."""
    req = UpdateMediaBuyRequest(media_buy_id=media_buy_id, budget=budget)
    result = env.call_impl(req=req)
    # _update_media_buy_impl returns UpdateMediaBuyResult wrapping the success
    # response (the return type is unified).
    assert isinstance(result.response, UpdateMediaBuySuccess), f"update must succeed, got {result!r}"
    return result.response


@pytest.mark.requires_db
class TestPersistedRevisionOnReadPath:
    """create → update → get report the persisted counter consistently."""

    def test_create_reports_revision_1_and_persisted_confirmed_at(self, integration_db):
        from tests.harness.media_buy_dual import MediaBuyDualEnv

        with MediaBuyDualEnv() as env:
            _tenant, _principal, product, _pricing = env.setup_media_buy_data()
            created = _create_buy(env, product)

            assert created.revision == 1
            assert created.confirmed_at is not None

            item = read_back_media_buy(env.identity, created.media_buy_id)
            assert item.revision == 1
            # Same persisted source everywhere: create's confirmed_at is the
            # row's created_at, and get_media_buys echoes both.
            assert item.confirmed_at == item.created_at
            assert item.confirmed_at == created.confirmed_at

    def test_create_then_update_shows_1_then_2_across_tools(self, integration_db):
        from tests.harness.media_buy_dual import MediaBuyDualEnv

        with MediaBuyDualEnv() as env:
            _tenant, _principal, product, _pricing = env.setup_media_buy_data()
            created = _create_buy(env, product)
            assert created.revision == 1

            updated = _update_budget(env, created.media_buy_id, 6000.0)
            assert updated.revision == 2

            item = read_back_media_buy(env.identity, created.media_buy_id)
            assert item.revision == 2
            # confirmed_at is stable after set — an update never moves it.
            assert item.confirmed_at == created.confirmed_at

    def test_single_update_touching_budget_and_dates_bumps_once(self, integration_db):
        """One accepted update that changes budget AND dates advances revision by
        exactly 1 — not once per changed field group.

        The update path bumped independently for the
        package set, the budget, and the date range (3 sites), so a combined
        update jumped the revision by 2-3. AdCP 3.1.1 ``revision`` is a per-resource
        version token, so one update must advance it by exactly one.
        """
        from datetime import UTC, datetime, timedelta

        from tests.harness.media_buy_dual import MediaBuyDualEnv

        with MediaBuyDualEnv() as env:
            _tenant, _principal, product, _pricing = env.setup_media_buy_data()
            created = _create_buy(env, product)
            assert created.revision == 1

            new_start = datetime.now(UTC) + timedelta(days=2)
            new_end = new_start + timedelta(days=20)
            req = UpdateMediaBuyRequest(
                media_buy_id=created.media_buy_id,
                budget=8000.0,
                start_time=new_start.isoformat(),
                end_time=new_end.isoformat(),
            )
            result = env.call_impl(req=req)
            assert isinstance(result.response, UpdateMediaBuySuccess), f"update must succeed, got {result!r}"
            # Budget + dates in one call → exactly one increment.
            assert result.response.revision == 2

            item = read_back_media_buy(env.identity, created.media_buy_id)
            assert item.revision == 2

    def test_rapid_consecutive_updates_yield_strictly_increasing_revisions(self, integration_db):
        """Two back-to-back updates (same wall-clock second) must not collide.

        A time-derived formula (e.g. 1 + whole seconds between created_at and
        updated_at) returns the SAME revision for updates landing within one
        second — this pins the persisted counter's strict monotonicity.
        """
        from tests.harness.media_buy_dual import MediaBuyDualEnv

        with MediaBuyDualEnv() as env:
            _tenant, _principal, product, _pricing = env.setup_media_buy_data()
            created = _create_buy(env, product)

            first = _update_budget(env, created.media_buy_id, 6000.0)
            second = _update_budget(env, created.media_buy_id, 7000.0)

            assert first.revision is not None and second.revision is not None
            assert created.revision is not None
            assert created.revision < first.revision < second.revision
            assert (first.revision, second.revision) == (2, 3)

            # And the read tool agrees with the last write.
            item = read_back_media_buy(env.identity, created.media_buy_id)
            assert item.revision == second.revision

    def test_pause_bumps_persisted_revision(self, integration_db):
        """A campaign-level pause through the real impl bumps the persisted counter.

        The value the buyer sees is produced by PostgreSQL (the server-side
        increment), not echoed from a mock — the real-DB half of the pause-bump
        contract whose plumbing half lives in
        ``tests/unit/test_update_media_buy_behavioral.py``.
        """
        from src.core.database.repositories import MediaBuyUoW
        from tests.harness.media_buy_dual import MediaBuyDualEnv

        with MediaBuyDualEnv() as env:
            tenant, _principal, product, _pricing = env.setup_media_buy_data()
            created = _create_buy(env, product)

            # 'pause' is only a valid action for an 'active' buy — transition the
            # setup state there (itself a bump), then capture the pre-pause value.
            with MediaBuyUoW(tenant.tenant_id) as uow:
                uow.media_buys.update_status(created.media_buy_id, "active")
            pre_pause = read_back_media_buy(env.identity, created.media_buy_id).revision

            result = env.call_impl(req=UpdateMediaBuyRequest(media_buy_id=created.media_buy_id, paused=True))
            assert isinstance(result.response, UpdateMediaBuySuccess), f"pause must succeed, got {result!r}"

            # Pause advanced the revision by exactly one, and the read tool agrees.
            assert result.response.revision == pre_pause + 1
            assert read_back_media_buy(env.identity, created.media_buy_id).revision == result.response.revision

    def test_pause_success_echoes_the_buyer_context(self, integration_db):
        """The pause arm must echo ``context`` like every other update success.

        The pause/resume arm builds its own ``UpdateMediaBuySuccess`` rather than
        falling through to the finalizer, and it omitted ``context=req.context`` —
        so a buyer correlating responses by the context it sent got nothing back
        on exactly this path, while the dry-run and finalizer arms echoed it.
        """
        from adcp.types import ContextObject

        from src.core.database.repositories import MediaBuyUoW
        from tests.harness.media_buy_dual import MediaBuyDualEnv

        with MediaBuyDualEnv() as env:
            tenant, _principal, product, _pricing = env.setup_media_buy_data()
            created = _create_buy(env, product)

            with MediaBuyUoW(tenant.tenant_id) as uow:
                uow.media_buys.update_status(created.media_buy_id, "active")

            sent = ContextObject(campaign_id="ctx-echo-pause", session="sess-pause-1")
            result = env.call_impl(
                req=UpdateMediaBuyRequest(media_buy_id=created.media_buy_id, paused=True, context=sent)
            )
            assert isinstance(result.response, UpdateMediaBuySuccess), f"pause must succeed, got {result!r}"

            echoed = result.response.context
            assert echoed is not None, "the pause success dropped the buyer's context echo entirely"
            assert echoed.campaign_id == "ctx-echo-pause"
            assert echoed.session == "sess-pause-1"


@pytest.mark.requires_db
class TestRevisionOptimisticConcurrency:
    """req.revision gate: mismatch MUST reject with CONFLICT; match/absent proceed.

    AdCP 3.1.1 update-media-buy-request.json ``properties.revision``:
    "When provided, sellers MUST reject the update with CONFLICT if the media
    buy's current revision does not match." (Schema MUST; no conformance
    storyboard step grades it — ungraded.)
    """

    def test_stale_revision_rejected_with_conflict_and_nothing_written(self, integration_db):
        from src.core.exceptions import AdCPConflictError
        from tests.harness.media_buy_dual import MediaBuyDualEnv

        with MediaBuyDualEnv() as env:
            _tenant, _principal, product, _pricing = env.setup_media_buy_data()
            created = _create_buy(env, product)  # persisted revision 1

            with pytest.raises(AdCPConflictError) as exc_info:
                env.call_impl(req=UpdateMediaBuyRequest(media_buy_id=created.media_buy_id, budget=9000.0, revision=5))

            # The typed CONFLICT carries both sides of the mismatch so the buyer
            # can re-read and retry (spec: "Obtain from get_media_buys or the
            # most recent update response").
            assert exc_info.value.error_code == "CONFLICT"
            assert exc_info.value.details is not None
            assert exc_info.value.details["expected_version"] == 5
            assert exc_info.value.details["current_version"] == 1
            assert exc_info.value.details["resource_id"] == created.media_buy_id

            # Rejected update wrote nothing: revision unchanged on a fresh read.
            assert read_back_media_buy(env.identity, created.media_buy_id).revision == 1

    @pytest.mark.parametrize("transport", [Transport.A2A, Transport.MCP], ids=lambda t: t.value)
    def test_stale_revision_in_a_request_model_still_conflicts_on_a2a_and_mcp(self, integration_db, transport):
        """A stale token carried by the REQUEST MODEL must reach the gate on a2a/mcp.

        The other a2a/mcp CONFLICT pins pass ``revision=`` as an explicit kwarg, and
        the harness's flatten step overlays explicit kwargs AFTER dropping its
        wrapper-unsupported list — so those dispatches never exercised the drop. A
        ``req=UpdateMediaBuyRequest(..., revision=N)`` dispatch is the only shape
        that does: the token lives inside the model, gets stripped in flattening,
        and the update then silently proceeds under last-write-wins. That is a
        harness-manufactured pass — the spec MUST ("sellers MUST reject the update
        with CONFLICT if the media buy's current revision does not match") would go
        ungraded on both wire transports.
        """
        from tests.harness.media_buy_dual import MediaBuyDualEnv
        from tests.helpers import assert_envelope_shape

        with MediaBuyDualEnv() as env:
            _tenant, _principal, product, _pricing = env.setup_media_buy_data()
            created = _create_buy(env, product)  # persisted revision 1

            result = env.call_via(
                transport,
                req=UpdateMediaBuyRequest(media_buy_id=created.media_buy_id, budget=9000.0, revision=999),
            )

            assert result.is_error, (
                f"a stale revision inside the request model must reject on {transport.value}, got {result!r}"
            )
            assert_envelope_shape(result.wire_error_envelope, "CONFLICT", recovery="transient")

            # Rejected update wrote nothing: the strip let the budget through.
            assert read_back_media_buy(env.identity, created.media_buy_id).revision == 1

    def test_vanished_buy_with_a_revision_token_fails_before_any_side_effect(self, integration_db):
        """A row that vanishes before the locked read must not skip the CONFLICT gate.

        The gate used to be guarded by ``and _current_mb is not None``, so a buy
        deleted between ``_verify_principal`` and the locked re-read skipped the
        spec-MUST compare-and-set entirely — and skipped the terminal gate too,
        since ``is_terminal_status("")`` is False. The request then went on to
        resolve the ad-server adapter and open a workflow step, and only failed
        later, mid-mutation. Graded on those side effects, not on the raised
        message alone: with the guard restored ``get_adapter`` is resolved once
        and a workflow step IS created before the failure.
        """
        from unittest.mock import patch

        from sqlalchemy import delete
        from sqlalchemy.orm import Session as SASession

        import src.core.tools.media_buy_update as media_buy_update_module
        from src.core.database.database_session import get_engine
        from src.core.database.models import MediaBuy, MediaPackage
        from tests.harness.media_buy_dual import MediaBuyDualEnv

        with MediaBuyDualEnv() as env:
            _tenant, _principal, product, _pricing = env.setup_media_buy_data()
            created = _create_buy(env, product)

            real_verify = media_buy_update_module._verify_principal
            # An independent session, like the other out-of-band DB work in these
            # suites — test bodies must not open get_db_session().
            racer = SASession(bind=get_engine())

            def _verify_then_delete(*args, **kwargs):
                """Real ownership check, then delete the row — the exact race window."""
                outcome = real_verify(*args, **kwargs)
                racer.execute(delete(MediaPackage).where(MediaPackage.media_buy_id == created.media_buy_id))
                racer.execute(delete(MediaBuy).where(MediaBuy.media_buy_id == created.media_buy_id))
                racer.commit()
                return outcome

            try:
                with patch.object(media_buy_update_module, "_verify_principal", side_effect=_verify_then_delete):
                    with pytest.raises(AdCPGoneError) as exc_info:
                        env.call_impl(
                            req=UpdateMediaBuyRequest(media_buy_id=created.media_buy_id, budget=9000.0, revision=1)
                        )
                # The vanished row is GONE, not a retryable server fault, and the
                # operator-facing invariant sentence stays out of the buyer's message.
                assert exc_info.value.wire_error_code == "INVALID_STATE"
                assert "update flow continued" not in exc_info.value.message
            finally:
                racer.close()

            # Rejected at the gate: no adapter was even resolved, and no workflow
            # step was opened. Both flip the moment the guard comes back.
            env.mock["update_adapter"].assert_not_called()
            env.mock["update_context_mgr"].return_value.create_workflow_step.assert_not_called()

    def test_vanished_buy_reaches_the_buyer_as_a_gone_envelope(self, integration_db):
        """The vanished-row invariant reaches the buyer as a two-layer GONE envelope.

        The guard used to raise a bare ``RuntimeError``. Untyped exceptions have no
        place in the typed cascade: A2A and MCP render them as
        ``SERVICE_UNAVAILABLE``/``transient`` — instructing a buyer agent to RETRY a
        request whose target row no longer exists — and REST emitted a bare 500 with
        no envelope at all. Drives a REAL REST request (FastAPI TestClient over
        ``src.app.app``) through the same delete-mid-flight race and asserts on the
        HTTP body, not on a reconstructed exception.
        """
        from unittest.mock import patch

        from sqlalchemy import delete
        from sqlalchemy.orm import Session as SASession

        import src.core.tools.media_buy_update as media_buy_update_module
        from src.core.database.database_session import get_engine
        from src.core.database.models import MediaBuy, MediaPackage
        from tests.harness.media_buy_dual import MediaBuyDualEnv
        from tests.helpers import assert_envelope_shape

        with MediaBuyDualEnv() as env:
            _tenant, _principal, product, _pricing = env.setup_media_buy_data()
            created = _create_buy(env, product)

            real_verify = media_buy_update_module._verify_principal
            racer = SASession(bind=get_engine())

            def _verify_then_delete(*args, **kwargs):
                outcome = real_verify(*args, **kwargs)
                racer.execute(delete(MediaPackage).where(MediaPackage.media_buy_id == created.media_buy_id))
                racer.execute(delete(MediaBuy).where(MediaBuy.media_buy_id == created.media_buy_id))
                racer.commit()
                return outcome

            try:
                with patch.object(media_buy_update_module, "_verify_principal", side_effect=_verify_then_delete):
                    result = env.call_via(
                        Transport.REST,
                        req=UpdateMediaBuyRequest(media_buy_id=created.media_buy_id, budget=9000.0, revision=1),
                    )
            finally:
                racer.close()

        assert result.is_error, f"a vanished media buy must reject, got {result!r}"
        assert_envelope_shape(
            result.wire_error_envelope,
            "INVALID_STATE",
            recovery="correctable",
        )

    def test_terminal_and_stale_revision_prefers_conflict_over_gone(self, integration_db):
        """CONFLICT precedence: a stale token against a buy that has since reached a
        terminal state MUST reject with CONFLICT (refetch-and-retry), not GONE.

        The pinned update-media-buy-request schema mandates CONFLICT on ANY revision
        mismatch unconditionally, so the optimistic-concurrency gate runs before the
        terminal-state gate. Pre-fix the terminal check ran first and returned GONE,
        masking the stale write and denying the buyer the refetch recovery.
        """
        from src.core.database.repositories import MediaBuyUoW
        from src.core.exceptions import AdCPConflictError
        from tests.harness.media_buy_dual import MediaBuyDualEnv

        with MediaBuyDualEnv() as env:
            _tenant, _principal, product, _pricing = env.setup_media_buy_data()
            created = _create_buy(env, product)  # persisted revision 1

            # Drive the buy into a terminal state out-of-band (revision 1 → 2)
            # through the repository/UoW seam (commits on context exit).
            with MediaBuyUoW(env.identity.tenant_id) as uow:
                uow.media_buys.update_status(created.media_buy_id, "completed")

            # Buyer sends a STALE token (1) against the now-terminal buy (revision 2).
            with pytest.raises(AdCPConflictError) as exc_info:
                env.call_impl(req=UpdateMediaBuyRequest(media_buy_id=created.media_buy_id, budget=9000.0, revision=1))

            assert exc_info.value.error_code == "CONFLICT"
            assert exc_info.value.details is not None
            assert exc_info.value.details["expected_version"] == 1
            assert exc_info.value.details["current_version"] == 2

    def test_matching_revision_proceeds_and_increments(self, integration_db):
        from tests.harness.media_buy_dual import MediaBuyDualEnv

        with MediaBuyDualEnv() as env:
            _tenant, _principal, product, _pricing = env.setup_media_buy_data()
            created = _create_buy(env, product)  # persisted revision 1

            result = env.call_impl(
                req=UpdateMediaBuyRequest(media_buy_id=created.media_buy_id, budget=9000.0, revision=1)
            )
            assert isinstance(result.response, UpdateMediaBuySuccess), f"matching revision must succeed, got {result!r}"
            assert result.response.revision == 2

    def test_database_failure_reaches_the_buyer_without_sql_or_bound_parameters(self, integration_db):
        """A raw SQLAlchemy error must never render its statement or bound values.

        ``str()`` on a DBAPI-wrapping SQLAlchemy exception renders the failing
        statement AND its bound parameter values. REST also had no handler for
        the family at all, so the buyer got a plaintext 500 carrying that text
        instead of the two-layer envelope. Drives a REAL REST request (FastAPI
        TestClient over ``src.app.app``) and asserts on the HTTP body.
        """
        import json

        from sqlalchemy.exc import OperationalError

        from src.core.database.database_session import reset_health_state
        from tests.harness.media_buy_dual import MediaBuyDualEnv
        from tests.helpers import assert_envelope_shape

        secret = "pw-do-not-leak-9f3c"
        try:
            with MediaBuyDualEnv() as env:
                _tenant, _principal, product, _pricing = env.setup_media_buy_data()
                created = _create_buy(env, product)

                env.mock["update_adapter"].side_effect = OperationalError(
                    "SELECT * FROM media_buys WHERE token = %(token)s",
                    {"token": secret},
                    Exception("server closed the connection unexpectedly"),
                )
                result = env.call_via(
                    Transport.REST,
                    req=UpdateMediaBuyRequest(media_buy_id=created.media_buy_id, budget=9000.0),
                )
        finally:
            # An OperationalError escaping a UoW is a genuine DB-outage signal, so
            # get_db_session opens the process-wide circuit breaker. Reset it or
            # every later test in this process fails fast on an injected fault.
            reset_health_state()

        assert result.is_error, f"a database failure must reject, got {result!r}"
        assert_envelope_shape(
            result.wire_error_envelope,
            "SERVICE_UNAVAILABLE",
            recovery="transient",
        )
        body = json.dumps(result.wire_error_envelope)
        assert "SQL:" not in body, f"the failing statement leaked to the buyer: {body}"
        assert secret not in body, f"a bound parameter value leaked to the buyer: {body}"
        assert "media_buys" not in body, f"SQL statement text leaked to the buyer: {body}"

    def test_absent_revision_keeps_last_write_wins(self, integration_db):
        """Omitting the token preserves LWW semantics — the gate only fires when provided."""
        from tests.harness.media_buy_dual import MediaBuyDualEnv

        with MediaBuyDualEnv() as env:
            _tenant, _principal, product, _pricing = env.setup_media_buy_data()
            created = _create_buy(env, product)

            result = _update_budget(env, created.media_buy_id, 9000.0)
            assert result.revision == 2


@pytest.mark.requires_db
class TestConflictDetailsShapeParity:
    """Every media-buy CONFLICT exposes the SAME ``details`` key set.

    A generic optimistic-concurrency retry loop reads
    ``details["current_version"]``. One raise site used to emit ``resource_id``
    alone, so that loop raised ``KeyError`` instead of reading "unknown". The
    keys are now uniform; a version that was never observed is an explicit
    ``None``, never a fabricated integer.
    """

    def test_revision_mismatch_and_lock_timeout_conflicts_share_a_details_key_set(self, integration_db):
        from sqlalchemy import select
        from sqlalchemy.orm import Session as SASession

        from src.core.database.database_session import get_engine, reset_health_state
        from src.core.database.models import MediaBuy
        from src.core.database.repositories import MediaBuyUoW
        from src.core.exceptions import AdCPConflictError
        from tests.harness.media_buy_dual import MediaBuyDualEnv

        with MediaBuyDualEnv() as env:
            _tenant, _principal, product, _pricing = env.setup_media_buy_data()
            created = _create_buy(env, product)
            tenant_id = env.identity.tenant_id

            # Shape A — the factory-built revision mismatch (both versions known).
            with pytest.raises(AdCPConflictError) as mismatch:
                env.call_impl(req=UpdateMediaBuyRequest(media_buy_id=created.media_buy_id, budget=9000.0, revision=5))

            # Shape B — the lock-timeout CONFLICT (neither version observable).
            holder = SASession(get_engine())
            try:
                holder.execute(select(MediaBuy).filter_by(media_buy_id=created.media_buy_id).with_for_update()).first()
                with pytest.raises(AdCPConflictError) as timeout:
                    with MediaBuyUoW(tenant_id) as waiter:
                        waiter.media_buys.get_by_id(created.media_buy_id, for_update=True, lock_timeout_seconds=1)
            finally:
                holder.rollback()
                holder.close()
                reset_health_state()

        mismatch_details = mismatch.value.details
        timeout_details = timeout.value.details
        assert mismatch_details is not None and timeout_details is not None
        assert mismatch_details.keys() == timeout_details.keys(), (
            "both CONFLICT shapes must expose the same details keys; "
            f"mismatch={sorted(mismatch_details)} lock_timeout={sorted(timeout_details)}"
        )
        # The unknown side says so explicitly rather than guessing a number.
        assert timeout_details["expected_version"] is None
        assert timeout_details["current_version"] is None
        # ...and the known side still carries the real values.
        assert mismatch_details["expected_version"] == 5
        assert mismatch_details["current_version"] == 1
