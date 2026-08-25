"""Real-Postgres coverage for WorkflowRepository's notification lease + CAS.

Mirrors ``test_a2a_task_notification_claims.py``'s technique (``threading.Event``
-based hold-and-block, independent connections racing the same row) for the
SIBLING claim mechanism: ``WorkflowRepository.claim_notification_publication`` /
``finalize_notification_claim``. The two mechanisms are NOT identical —
``WorkflowRepository`` leases the OLDEST matching occurrence by
``(step_id, status)`` (optionally ``event_id``), takes a ``lease_seconds`` param,
and has cancellation-supersession logic the A2A version does not. These tests
cover the lease+CAS mechanism only; supersession is already covered separately
by ``test_workflow_notification_outbox.py``.

Prior coverage exercised ``WorkflowRepository`` claim/CAS only with
``session = MagicMock()`` — same-process, sequential, no real row contention.
"""

from threading import Event
from uuid import uuid4

import pytest

from src.core.context_manager import ContextManager
from src.core.database.repositories.uow import WorkflowUoW
from tests.factories import PrincipalFactory, TenantFactory
from tests.helpers import run_hold_and_block_race

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


def _new_workflow_scope(*, status: str = "completed") -> tuple[str, str, str]:
    """Create a tenant + context + workflow step whose transition to *status*
    enqueues one notifiable event, via the repository's own public entry point
    (``update_status``) rather than hand-constructing ``WorkflowNotificationEvent``
    rows.
    """
    tenant = TenantFactory(tenant_id=f"wf-claim-tenant-{uuid4().hex[:8]}")
    principal = PrincipalFactory(tenant=tenant, principal_id=f"wf-claim-principal-{uuid4().hex[:8]}")
    ctx_mgr = ContextManager()
    context = ctx_mgr.create_context(tenant_id=tenant.tenant_id, principal_id=principal.principal_id)
    step = ctx_mgr.create_workflow_step(
        context_id=context.context_id,
        step_type="tool_call",
        owner="system",
        status="pending",
        tool_name="create_media_buy",
    )
    with WorkflowUoW(tenant.tenant_id) as uow:
        assert uow.workflows is not None
        updated = uow.workflows.update_status(step.step_id, status=status)
    assert updated is not None
    return tenant.tenant_id, step.step_id, status


def test_concurrent_claim_publication_only_one_thread_wins(integration_db) -> None:
    """Two threads racing to lease the same (step_id, status) occurrence: the
    loser is genuinely blocked on the winner's real row lock (not merely
    not-yet-scheduled), mirroring the A2A file's Event-based technique —
    a ``threading.Barrier`` race was mutation-proven sequential there.
    """
    from tests.harness._base import BareIntegrationEnv

    with BareIntegrationEnv():
        tenant_id, step_id, status = _new_workflow_scope()

        winner_result: dict[str, object] = {}

        def hold_winner_claim(claimed_event: Event, release_event: Event) -> None:
            with WorkflowUoW(tenant_id) as uow:
                assert uow.workflows is not None
                winner_result["claim"] = uow.workflows.claim_notification_publication(step_id, status)
                claimed_event.set()
                assert release_event.wait(timeout=5)

        def attempt_loser_claim(claimed_event: Event):
            assert claimed_event.wait(timeout=5)
            with WorkflowUoW(tenant_id) as uow:
                assert uow.workflows is not None
                return uow.workflows.claim_notification_publication(step_id, status)

        loser_result = run_hold_and_block_race(hold_winner_claim, attempt_loser_claim)

    winner_claim = winner_result["claim"]
    assert winner_claim is not None and winner_claim[1] is not None  # (event_id, claim_token, response_data)
    assert loser_result is None, f"the loser must observe the now-live lease as unclaimable, got: {loser_result}"


def test_finalize_notification_claim_rejects_stale_token_after_real_ack(integration_db) -> None:
    """A second ack/release with the SAME (now-cleared) token is a no-op against a real row."""
    from tests.harness._base import BareIntegrationEnv

    with BareIntegrationEnv():
        tenant_id, step_id, status = _new_workflow_scope()

        with WorkflowUoW(tenant_id) as uow:
            assert uow.workflows is not None
            claimed = uow.workflows.claim_notification_publication(step_id, status)
        assert claimed is not None
        event_id, claim_token, _response_data = claimed
        assert claim_token is not None
        stale_token = claim_token

        with WorkflowUoW(tenant_id) as uow:
            assert uow.workflows is not None
            assert uow.workflows.mark_notifications_published(event_id, claim_token=stale_token) is True

        # Retrying the ACK with the same token the caller already spent must be a
        # no-op: finalize_notification_claim's CAS compares against the row's
        # CURRENT claim_token, which mark_notifications_published just cleared.
        with WorkflowUoW(tenant_id) as uow:
            assert uow.workflows is not None
            assert uow.workflows.mark_notifications_published(event_id, claim_token=stale_token) is False
            assert uow.workflows.release_notification_claim(event_id, claim_token=stale_token) is False


def test_finalize_notification_claim_rejects_wrong_token_from_a_second_claimant(integration_db) -> None:
    """A wrong claim_token can never release the true owner's live lease."""
    from tests.harness._base import BareIntegrationEnv

    with BareIntegrationEnv():
        tenant_id, step_id, status = _new_workflow_scope()

        with WorkflowUoW(tenant_id) as uow:
            assert uow.workflows is not None
            claimed = uow.workflows.claim_notification_publication(step_id, status)
        assert claimed is not None
        event_id, real_token, _response_data = claimed
        assert real_token is not None
        wrong_token = str(uuid4())
        assert wrong_token != real_token

        with WorkflowUoW(tenant_id) as uow:
            assert uow.workflows is not None
            assert uow.workflows.release_notification_claim(event_id, claim_token=wrong_token) is False
            # The real owner's lease is untouched — it can still finalize with its own token.
            assert uow.workflows.mark_notifications_published(event_id, claim_token=real_token) is True


def test_claim_notification_publication_cannot_cross_tenant_boundary(integration_db) -> None:
    """A tenant scope can never observe or claim another tenant's real notification event.

    ``claim_notification_publication`` filters on ``tenant_id`` as well as
    ``(step_id, status)`` — a caller (buggy or malicious) that somehow learns
    another tenant's ``step_id`` must not be able to claim that tenant's
    occurrence. Prove the guard directly: tenant B, given tenant A's real
    step_id/status, must not be able to claim it.
    """
    from tests.harness._base import BareIntegrationEnv

    with BareIntegrationEnv():
        tenant_a_id, step_id, status = _new_workflow_scope()
        tenant_b = TenantFactory(tenant_id=f"wf-claim-tenant-{uuid4().hex[:8]}")

        with WorkflowUoW(tenant_b.tenant_id) as uow:
            assert uow.workflows is not None
            cross_tenant_claim = uow.workflows.claim_notification_publication(step_id, status)

    assert cross_tenant_claim is None, "a tenant must not observe another tenant's notification event"

    # Sanity: the SAME (step_id, status) is genuinely claimable by its actual
    # owner — proves the None above is the tenant filter, not a mismatched
    # step_id/status.
    with WorkflowUoW(tenant_a_id) as uow:
        assert uow.workflows is not None
        own_claim = uow.workflows.claim_notification_publication(step_id, status)
    assert own_claim is not None and own_claim[1] is not None


def test_claim_notification_publication_steals_an_expired_lease(integration_db) -> None:
    """A claim past its lease_seconds boundary can be re-claimed, not rejected as live.

    Sibling of the A2A file's equivalent test — only the live/fresh case was
    previously covered here too (``git grep lease_seconds -- tests/`` returned
    nothing before this order). A near-zero ``lease_seconds`` on the SECOND
    call treats the first claim as already past its boundary, genuinely
    driving the ``claimed_at >= now - lease_seconds`` steal path.
    """
    from tests.harness._base import BareIntegrationEnv

    with BareIntegrationEnv():
        tenant_id, step_id, status = _new_workflow_scope()

        with WorkflowUoW(tenant_id) as uow:
            assert uow.workflows is not None
            first = uow.workflows.claim_notification_publication(step_id, status)
        assert first is not None and first[1] is not None

        with WorkflowUoW(tenant_id) as uow:
            assert uow.workflows is not None
            second = uow.workflows.claim_notification_publication(step_id, status, lease_seconds=0)

    assert second is not None and second[1] is not None
    assert second[1] != first[1], "the steal must mint a fresh claim_token, not reuse the stale one"
