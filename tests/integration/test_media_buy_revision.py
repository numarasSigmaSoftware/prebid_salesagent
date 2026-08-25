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

from src.core.exceptions import AdCPInternalError
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


@pytest.fixture
def env_and_buy(integration_db):
    """A dual env with one freshly created buy — the preamble every test here shared.

    Yields ``(env, created)`` where ``created`` is at persisted revision 1. The
    three-line ``MediaBuyDualEnv() -> setup_media_buy_data() -> _create_buy()``
    opening was repeated verbatim at fourteen sites; a change to the seeded shape
    had to be made fourteen times or the tests drifted apart.

    FUNCTION-scoped deliberately, not module-scoped: nearly every test here
    MUTATES the buy's revision and asserts an exact value (1, then 2, then 3), so
    a shared buy would make the assertions order-dependent. The tenant is
    reachable as ``env.identity.tenant_id``. Tests that need a fault-injected
    engine, a distinct brand, or a distinct flight still dispatch directly.
    """
    from tests.harness.media_buy_dual import MediaBuyDualEnv

    with MediaBuyDualEnv() as env:
        _tenant, _principal, product, _pricing = env.setup_media_buy_data()
        yield env, _create_buy(env, product)


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

    def test_create_reports_revision_1_and_persisted_confirmed_at(self, env_and_buy):
        env, created = env_and_buy

        assert created.revision == 1
        assert created.confirmed_at is not None

        item = read_back_media_buy(env.identity, created.media_buy_id)
        assert item.revision == 1
        # Same persisted source everywhere: create's confirmed_at is the
        # row's created_at, and get_media_buys echoes both.
        assert item.confirmed_at == item.created_at
        assert item.confirmed_at == created.confirmed_at

    def test_create_then_update_shows_1_then_2_across_tools(self, env_and_buy):
        env, created = env_and_buy

        assert created.revision == 1

        updated = _update_budget(env, created.media_buy_id, 6000.0)
        assert updated.revision == 2

        item = read_back_media_buy(env.identity, created.media_buy_id)
        assert item.revision == 2
        # confirmed_at is stable after set — an update never moves it.
        assert item.confirmed_at == created.confirmed_at

    def test_single_update_touching_budget_and_dates_bumps_once(self, env_and_buy):
        """One accepted update that changes budget AND dates advances revision by
        exactly 1 — not once per changed field group.

        The update path bumped independently for the
        package set, the budget, and the date range (3 sites), so a combined
        update jumped the revision by 2-3. AdCP 3.1.1 ``revision`` is a per-resource
        version token, so one update must advance it by exactly one.
        """
        env, created = env_and_buy
        from datetime import UTC, datetime, timedelta

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

    def test_rapid_consecutive_updates_yield_strictly_increasing_revisions(self, env_and_buy):
        """Two back-to-back updates (same wall-clock second) must not collide.

        A time-derived formula (e.g. 1 + whole seconds between created_at and
        updated_at) returns the SAME revision for updates landing within one
        second — this pins the persisted counter's strict monotonicity.
        """
        env, created = env_and_buy

        first = _update_budget(env, created.media_buy_id, 6000.0)
        second = _update_budget(env, created.media_buy_id, 7000.0)

        assert first.revision is not None and second.revision is not None
        assert created.revision is not None
        assert created.revision < first.revision < second.revision
        assert (first.revision, second.revision) == (2, 3)

        # And the read tool agrees with the last write.
        item = read_back_media_buy(env.identity, created.media_buy_id)
        assert item.revision == second.revision

    def test_pause_bumps_persisted_revision(self, env_and_buy):
        """A campaign-level pause through the real impl bumps the persisted counter.

        The value the buyer sees is produced by PostgreSQL (the server-side
        increment), not echoed from a mock — the real-DB half of the pause-bump
        contract whose plumbing half lives in
        ``tests/unit/test_update_media_buy_behavioral.py``.
        """
        env, created = env_and_buy
        from src.core.database.repositories import MediaBuyUoW

        # 'pause' is only a valid action for an 'active' buy — transition the
        # setup state there (itself a bump), then capture the pre-pause value.
        with MediaBuyUoW(env.identity.tenant_id) as uow:
            uow.media_buys.update_status(created.media_buy_id, "active")
        pre_pause = read_back_media_buy(env.identity, created.media_buy_id).revision

        result = env.call_impl(req=UpdateMediaBuyRequest(media_buy_id=created.media_buy_id, paused=True))
        assert isinstance(result.response, UpdateMediaBuySuccess), f"pause must succeed, got {result!r}"

        # Pause advanced the revision by exactly one, and the read tool agrees.
        assert result.response.revision == pre_pause + 1
        assert read_back_media_buy(env.identity, created.media_buy_id).revision == result.response.revision

    def test_pause_success_echoes_the_buyer_context(self, env_and_buy):
        """The pause arm must echo ``context`` like every other update success.

        The pause/resume arm builds its own ``UpdateMediaBuySuccess`` rather than
        falling through to the finalizer, and it omitted ``context=req.context`` —
        so a buyer correlating responses by the context it sent got nothing back
        on exactly this path, while the dry-run and finalizer arms echoed it.
        """
        env, created = env_and_buy
        from adcp.types import ContextObject

        from src.core.database.repositories import MediaBuyUoW

        with MediaBuyUoW(env.identity.tenant_id) as uow:
            uow.media_buys.update_status(created.media_buy_id, "active")

        sent = ContextObject(campaign_id="ctx-echo-pause", session="sess-pause-1")
        result = env.call_impl(req=UpdateMediaBuyRequest(media_buy_id=created.media_buy_id, paused=True, context=sent))
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
    buy's current revision does not match, and MUST enforce that comparison
    atomically with the write."

    The FIELD's presence on a response IS graded structurally by the conformance
    storyboard's ``check: response_schema`` steps. What is ungraded there is the
    BEHAVIOUR: no storyboard step sends a stale token or asserts that an accepted
    update incremented the counter. These tests are that grading.
    """

    def test_stale_revision_rejected_with_conflict_and_nothing_written(self, env_and_buy):
        env, created = env_and_buy
        from src.core.exceptions import AdCPConflictError

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
    def test_stale_revision_in_a_request_model_still_conflicts_on_a2a_and_mcp(self, env_and_buy, transport):
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
        env, created = env_and_buy
        from tests.helpers import assert_envelope_shape

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

    def test_vanished_buy_with_a_revision_token_fails_before_any_side_effect(self, env_and_buy):
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
        env, created = env_and_buy
        from unittest.mock import patch

        from sqlalchemy import delete
        from sqlalchemy.orm import Session as SASession

        import src.core.tools.media_buy_update as media_buy_update_module
        from src.core.database.database_session import get_engine
        from src.core.database.models import MediaBuy, MediaPackage

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
                with pytest.raises(AdCPInternalError) as exc_info:
                    env.call_impl(
                        req=UpdateMediaBuyRequest(media_buy_id=created.media_buy_id, budget=9000.0, revision=1)
                    )
            # _verify_principal already accepted this buy for this request, so the
            # buyer has nothing to correct — the breach is ours. It reaches them as
            # a retryable server fault, and the operator-facing invariant sentence
            # stays out of the buyer's message.
            assert exc_info.value.wire_error_code == "SERVICE_UNAVAILABLE"
            assert exc_info.value.recovery == "transient"
            assert "update flow continued" not in exc_info.value.message
        finally:
            racer.close()

        # Rejected at the gate: no adapter was even resolved, and no workflow
        # step was opened. Both flip the moment the guard comes back.
        env.mock["update_adapter"].assert_not_called()
        env.mock["update_context_mgr"].return_value.create_workflow_step.assert_not_called()

    def test_vanished_buy_reaches_the_buyer_as_a_server_fault_envelope(self, env_and_buy):
        """The vanished-row invariant reaches the buyer as a two-layer server-fault envelope.

        The guard used to raise a bare ``RuntimeError`` — REST emitted a bare 500
        with no envelope at all — and then an ``AdCPGoneError``, whose
        ``INVALID_STATE``/``correctable`` pair told a buyer agent to go fix a
        request it did not get wrong. The row vanishing under a read this request
        had already verified is our invariant, so the honest pair is
        ``SERVICE_UNAVAILABLE``/``transient``. Drives a REAL REST request (FastAPI
        TestClient over ``src.app.app``) through the same delete-mid-flight race
        and asserts on the HTTP body, not on a reconstructed exception.
        """
        env, created = env_and_buy
        from unittest.mock import patch

        from sqlalchemy import delete
        from sqlalchemy.orm import Session as SASession

        import src.core.tools.media_buy_update as media_buy_update_module
        from src.core.database.database_session import get_engine
        from src.core.database.models import MediaBuy, MediaPackage
        from tests.helpers import assert_envelope_shape

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
            "SERVICE_UNAVAILABLE",
            recovery="transient",
        )
        # The invariant sentence and the row id are operator-facing: neither may
        # ride out on the wire message the buyer actually reads.
        wire_message = result.wire_error_envelope["adcp_error"]["message"]
        assert "update flow continued" not in wire_message, (
            f"the internal invariant sentence leaked onto the wire: {wire_message!r}"
        )
        assert created.media_buy_id not in wire_message, f"the row id leaked onto the wire: {wire_message!r}"

    def test_terminal_and_stale_revision_prefers_conflict_over_gone(self, env_and_buy):
        """CONFLICT precedence: a stale token against a buy that has since reached a
        terminal state MUST reject with CONFLICT (refetch-and-retry), not GONE.

        The pinned update-media-buy-request schema mandates CONFLICT on ANY revision
        mismatch unconditionally, so the optimistic-concurrency gate runs before the
        terminal-state gate. Pre-fix the terminal check ran first and returned GONE,
        masking the stale write and denying the buyer the refetch recovery.
        """
        env, created = env_and_buy
        from src.core.database.repositories import MediaBuyUoW
        from src.core.exceptions import AdCPConflictError

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

    def test_matching_revision_proceeds_and_increments(self, env_and_buy):
        env, created = env_and_buy

        result = env.call_impl(req=UpdateMediaBuyRequest(media_buy_id=created.media_buy_id, budget=9000.0, revision=1))
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

    def test_absent_revision_keeps_last_write_wins(self, env_and_buy):
        """Omitting the token preserves LWW semantics — the gate only fires when provided."""
        env, created = env_and_buy

        result = _update_budget(env, created.media_buy_id, 9000.0)
        assert result.revision == 2


@pytest.mark.requires_db
class TestConflictDetailsShapeParity:
    """Every media-buy CONFLICT ``details`` body validates against the pinned schema.

    ``details`` reaches the buyer verbatim, so the contract it answers to is
    ``error-details/conflict.json`` at the pinned AdCP version, not an internal
    convention. That schema types ``expected_version``/``current_version`` as
    ``["number", "string"]`` and declares no ``required`` array: a version the
    seller never observed is expressed by OMITTING the key, and an explicit
    ``null`` is INVALID ("None is not of type 'number', 'string'").

    An earlier version of this class asserted the two arms exposed the same key
    set, which pinned exactly the defect — the lock-timeout arm carried both keys
    as nulls and shipped a schema-invalid body. The two arms legitimately differ:
    what both must satisfy is the schema.
    """

    @staticmethod
    def _assert_valid_conflict_details(details: dict, label: str) -> None:
        """Validate a details body against the PINNED conflict.json (draft-07)."""
        import json
        from pathlib import Path

        import adcp
        from jsonschema import Draft7Validator

        schema_path = Path(adcp.__file__).parent / "_schemas" / "3.1" / "error-details" / "conflict.json"
        assert schema_path.exists(), f"pinned conflict schema missing at {schema_path}"
        schema = json.loads(schema_path.read_text())
        errors = sorted(Draft7Validator(schema).iter_errors(details), key=lambda e: list(e.path))
        assert not errors, (
            f"{label} CONFLICT details is invalid against the pinned conflict.json: "
            + "; ".join(f"{list(error.path)}: {error.message}" for error in errors)
            + f" (body={details!r})"
        )

    def test_revision_mismatch_and_lock_timeout_conflict_details_match_the_pinned_schema(self, env_and_buy):
        env, created = env_and_buy
        from sqlalchemy import select
        from sqlalchemy.orm import Session as SASession

        from src.core.database.database_session import get_engine, reset_health_state
        from src.core.database.models import MediaBuy
        from src.core.database.repositories import MediaBuyUoW
        from src.core.exceptions import AdCPConflictError

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

        # Both bodies answer to the pinned schema — the buyer-facing contract.
        self._assert_valid_conflict_details(mismatch_details, "revision-mismatch")
        self._assert_valid_conflict_details(timeout_details, "lock-timeout")

        # The known-version arm carries both versions, with the real values.
        assert mismatch_details["expected_version"] == 5
        assert mismatch_details["current_version"] == 1

        # The unknown-version arm carries NEITHER: the lock timed out before the
        # row could be read, and absence is how the schema expresses "unknown".
        # A null here validates as invalid above; a number would be fabricated.
        assert "expected_version" not in timeout_details, (
            f"lock-timeout CONFLICT reported an expected_version it never observed: {timeout_details!r}"
        )
        assert "current_version" not in timeout_details, (
            f"lock-timeout CONFLICT reported a current_version it never observed: {timeout_details!r}"
        )
        assert timeout_details["resource_id"] == created.media_buy_id


@pytest.mark.requires_db
class TestConflictEnvelopeOnEveryTransport:
    """The CONFLICT's buyer-actionable payload must survive to the WIRE.

    ``details`` (both sides of the mismatch), ``field`` and ``suggestion`` were
    graded only on the reconstructed ``_impl`` exception. A boundary that dropped
    any of them — the A2A serializer has done exactly that to ``field`` and
    ``suggestion`` before — would leave every one of those pins green while the
    buyer received a bare code. Graded per transport on the real envelope.
    """

    @pytest.mark.parametrize("transport", [Transport.A2A, Transport.MCP, Transport.REST], ids=lambda t: t.value)
    def test_conflict_carries_details_field_and_suggestion_on_the_wire(self, env_with_media_buy, transport):
        env, media_buy = env_with_media_buy

        result = env.call_via(transport, media_buy_id=media_buy.media_buy_id, budget=9000.0, revision=999)

        assert result.is_error, f"a stale revision must reject on {transport.value}, got {result!r}"
        assert result.wire_error_envelope is not None, "wire envelope not captured"
        error = result.wire_error_envelope["errors"][0]

        assert error["code"] == "CONFLICT"
        assert error.get("field") == "revision", (
            f"the CONFLICT must name the offending field on the wire, got field={error.get('field')!r}"
        )
        suggestion = error.get("suggestion") or ""
        assert "get_media_buys" in suggestion, (
            f"the buyer must be told how to obtain a fresh token, got suggestion={suggestion!r}"
        )

        details = error.get("details")
        assert details is not None, "the CONFLICT dropped its details payload before the wire"
        # A generic optimistic-concurrency retry loop reads these three keys.
        # Numeric comparison, not identity: A2A delivers these as JSON doubles
        # (999.0) where MCP and REST deliver ints — see
        # TestEmittedRevisionAndConfirmedAtPerTransport for the grounding.
        assert details["expected_version"] == 999
        assert details["current_version"] == 1
        assert details["resource_id"] == media_buy.media_buy_id


class TestRawPayloadRoutesToUpdate:
    """Each operand of the harness's raw-payload routing predicate is graded.

    ``tests/harness/media_buy_dual._is_update_request`` falls back to
    ``"revision" in kwargs or "media_buy_id" in kwargs`` when no typed ``req`` is
    present — the shape a wire test sends when the revision is deliberately
    invalid, so ``UpdateMediaBuyRequest`` could not be constructed. With both
    operands ungraded, deleting either one left every caller green (they all send
    BOTH keys) while a raw payload carrying only the other silently routed to
    CREATE, which fails on missing brand/packages instead of grading the token.
    """

    @pytest.mark.parametrize(
        ("kwargs", "expected", "why"),
        [
            ({"revision": 1}, True, "revision is an update-only optimistic-concurrency token"),
            ({"media_buy_id": "mb_x"}, True, "media_buy_id targets an existing buy"),
            ({"brand": "acme.example", "packages": [], "start_time": "asap"}, False, "a create-shaped payload"),
        ],
        ids=["revision_only", "media_buy_id_only", "create_shaped"],
    )
    def test_raw_payload_routing(self, kwargs, expected, why):
        from tests.harness.media_buy_dual import _is_update_request

        assert _is_update_request(kwargs) is expected, why


@pytest.mark.requires_db
class TestEmittedRevisionAndConfirmedAtPerTransport:
    """The VALUES of ``revision`` and ``confirmed_at`` are graded on every wire.

    Both fields were graded only through typed response objects or a comparison
    that could not fail: ``payload["revision"] == 2`` is green whether the wire
    carried the int ``2`` or the double ``2.0``, and ``confirmed_at``'s emitted
    value was asserted on no transport at all. These tests read the real wire
    body per transport and pin both the value and its JSON type.

    THE PER-TRANSPORT JSON-TYPE FORK IS DELIBERATE AND CONFORMANT — do not
    "fix" it. A2A carries the payload through a protobuf ``Struct``, whose only
    numeric type is a double, so an integer arrives as ``2.0``; MCP and REST
    serialize plain JSON and emit ``2``. The pinned schemas type ``revision`` as
    draft-07 ``{"type": "integer", "minimum": 1}``, and draft-07 ``integer``
    matches ANY number with a zero fractional part — so ``2.0`` satisfies it
    exactly as ``2`` does. (This is the same draft-07 rule that makes an inbound
    ``revision: 7.0`` acceptable on the request side.)

    The double DOES impose a ceiling: an IEEE-754 double represents integers
    exactly only up to 2**53, so a counter beyond that would lose precision on
    A2A. At one bump per accepted update that bound is unreachable in practice,
    and normalising the A2A representation is a separate decision — these tests
    grade what is emitted, they do not change it.
    """

    #: Transports whose JSON numbers survive as Python ints, paired with the
    #: type the wire must carry. ``bool`` is excluded implicitly: it would be a
    #: different JSON type entirely.
    _INT_TRANSPORTS = [Transport.MCP, Transport.REST]

    @pytest.mark.parametrize("transport", [Transport.A2A, Transport.MCP, Transport.REST], ids=lambda t: t.value)
    def test_emitted_revision_value_and_json_type(self, env_with_media_buy, transport):
        env, media_buy = env_with_media_buy

        result = env.call_via(transport, media_buy_id=media_buy.media_buy_id, budget=9000.0)

        assert result.is_success, f"the update must succeed on {transport.value}, got {result!r}"
        wire = result.wire_response
        assert isinstance(wire, dict), f"no wire body captured on {transport.value}: {wire!r}"

        # The seeded buy is at revision 1; one accepted update bumps it to 2.
        assert wire["revision"] == 2, f"{transport.value} emitted revision={wire['revision']!r}, expected 2"

        emitted = wire["revision"]
        if transport in self._INT_TRANSPORTS:
            assert isinstance(emitted, int) and not isinstance(emitted, bool), (
                f"{transport.value} serializes plain JSON and must emit an integer, "
                f"got {type(emitted).__name__} ({emitted!r})"
            )
        else:
            # Pinned, not tolerated silently: if A2A ever starts emitting an int
            # this reddens, and whoever changed it reads the docstring above
            # before deciding whether the change was intended.
            assert isinstance(emitted, float), (
                "A2A carries the payload through a protobuf Struct (doubles only), so the "
                f"counter is expected as a float here; got {type(emitted).__name__} ({emitted!r})"
            )
            assert emitted.is_integer(), f"a revision must have no fractional part, got {emitted!r}"

    @pytest.mark.parametrize("transport", [Transport.A2A, Transport.MCP, Transport.REST], ids=lambda t: t.value)
    def test_emitted_confirmed_at_matches_the_persisted_instant(self, integration_db, transport):
        """The create response's ``confirmed_at`` must be the PERSISTED instant.

        A buy created straight into a seller-confirmed status is stamped with its
        create instant, and the create response carries that stamp. Nothing
        asserted the emitted VALUE on any transport, so a response that echoed
        "now", or a null, or the class default would have passed everywhere.
        """
        from datetime import datetime

        from tests.harness.media_buy_dual import MediaBuyDualEnv

        with MediaBuyDualEnv() as env:
            _tenant, _principal, product, _pricing = env.setup_media_buy_data()

            result = env.call_via(transport, **env.default_create_kwargs(product, brand_domain="i2.example"))

            assert result.is_success, f"the create must succeed on {transport.value}, got {result!r}"
            wire = result.wire_response
            assert isinstance(wire, dict), f"no wire body captured on {transport.value}: {wire!r}"

            assert "confirmed_at" in wire, f"{transport.value} dropped the required confirmed_at key"
            emitted = wire["confirmed_at"]
            assert emitted is not None, f"{transport.value} emitted a null confirmed_at for a committed buy"

            persisted = read_back_media_buy(env.identity, wire["media_buy_id"]).confirmed_at
            assert persisted is not None, "the created buy was not stamped in the database"
            assert datetime.fromisoformat(str(emitted).replace("Z", "+00:00")) == persisted, (
                f"{transport.value} emitted confirmed_at={emitted!r} but the row holds {persisted!r}"
            )
