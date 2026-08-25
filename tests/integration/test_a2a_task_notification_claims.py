"""Real-Postgres coverage for the A2A task notification outbox's lease + CAS.

Mirrors ``test_webhook_event_claims.py``'s technique (``threading.Event``-based
hold-and-block, independent connections racing the same row) for a DIFFERENT
claim mechanism: ``A2ATaskRepository.claim_notification_publication`` /
``finalize_notification_claim``. Prior coverage (``test_creative_unblock_recovery.py``)
only exercised the caller with ``session = MagicMock()`` — same-process,
sequential, no real row contention — and ``finalize_notification_claim`` itself
had zero test references. These tests drive the actual ``SELECT ... FOR UPDATE``
lease and the claim-token CAS against a real database.
"""

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest

from src.core.database.repositories.uow import A2ATaskUoW
from tests.factories import PrincipalFactory, TenantFactory

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


def _new_task_scope() -> tuple[str, str, str]:
    tenant = TenantFactory(tenant_id=f"a2a-claim-tenant-{uuid4().hex[:8]}")
    principal = PrincipalFactory(tenant=tenant, principal_id=f"a2a-claim-principal-{uuid4().hex[:8]}")
    task_id = f"a2a-claim-task-{uuid4().hex[:8]}"
    with A2ATaskUoW(tenant.tenant_id) as uow:
        assert uow.tasks is not None
        uow.tasks.upsert(
            task_id=task_id,
            principal_id=principal.principal_id,
            context_id=None,
            workflow_step_id=None,
            status="completed",
            task_payload={"ok": True},
        )
        event_id = uow.tasks.enqueue_notification(
            task_id=task_id,
            principal_id=principal.principal_id,
            status="completed",
            task_payload={"ok": True},
        )
    return tenant.tenant_id, task_id, event_id


def test_concurrent_claim_publication_only_one_thread_wins(integration_db) -> None:
    """Two threads racing to lease the same event: the loser is genuinely
    blocked on the winner's real row lock (not merely not-yet-scheduled), and
    only the winner ends up with a live claim once the winner's transaction
    (and thus its ``SELECT ... FOR UPDATE`` lock) commits.
    """
    from tests.harness._base import BareIntegrationEnv

    with BareIntegrationEnv():
        tenant_id, _task_id, event_id = _new_task_scope()

        claimed_event = Event()
        release_event = Event()
        winner_result: dict[str, object] = {}

        def hold_winner_claim() -> None:
            with A2ATaskUoW(tenant_id) as uow:
                assert uow.tasks is not None
                winner_result["claim"] = uow.tasks.claim_notification_publication(event_id)
                claimed_event.set()
                assert release_event.wait(timeout=5)

        def attempt_loser_claim():
            assert claimed_event.wait(timeout=5)
            with A2ATaskUoW(tenant_id) as uow:
                assert uow.tasks is not None
                return uow.tasks.claim_notification_publication(event_id)

        with ThreadPoolExecutor(max_workers=2) as executor:
            winner_future = executor.submit(hold_winner_claim)
            assert claimed_event.wait(timeout=5)
            loser_future = executor.submit(attempt_loser_claim)
            time.sleep(0.2)
            assert not loser_future.done(), "loser must be blocked on the winner's row lock, not just unscheduled"
            release_event.set()
            winner_future.result(timeout=5)
            loser_result = loser_future.result(timeout=5)

    winner_claim = winner_result["claim"]
    assert winner_claim is not None and winner_claim.claim_token is not None
    assert loser_result is None, f"the loser must observe the now-live lease as unclaimable, got: {loser_result}"


def test_finalize_notification_claim_rejects_stale_token_after_real_ack(integration_db) -> None:
    """A second ack/release with the SAME (now-cleared) token is a no-op against a real row."""
    from tests.harness._base import BareIntegrationEnv

    with BareIntegrationEnv():
        tenant_id, _task_id, event_id = _new_task_scope()

        with A2ATaskUoW(tenant_id) as uow:
            assert uow.tasks is not None
            claimed = uow.tasks.claim_notification_publication(event_id)
        assert claimed is not None and claimed.claim_token is not None
        stale_token = claimed.claim_token

        with A2ATaskUoW(tenant_id) as uow:
            assert uow.tasks is not None
            assert uow.tasks.mark_notification_published(event_id, claim_token=stale_token) is True

        # Retrying the ACK with the same token the caller already spent must be a
        # no-op: finalize_notification_claim's CAS compares against the row's
        # CURRENT claim_token, which mark_notification_published just cleared.
        with A2ATaskUoW(tenant_id) as uow:
            assert uow.tasks is not None
            assert uow.tasks.mark_notification_published(event_id, claim_token=stale_token) is False
            assert uow.tasks.release_notification_claim(event_id, claim_token=stale_token) is False


def test_finalize_notification_claim_rejects_wrong_token_from_a_second_claimant(integration_db) -> None:
    """A wrong claim_token can never release the true owner's live lease."""
    from tests.harness._base import BareIntegrationEnv

    with BareIntegrationEnv():
        tenant_id, _task_id, event_id = _new_task_scope()

        with A2ATaskUoW(tenant_id) as uow:
            assert uow.tasks is not None
            claimed = uow.tasks.claim_notification_publication(event_id)
        assert claimed is not None and claimed.claim_token is not None
        real_token = claimed.claim_token
        wrong_token = str(uuid4())
        assert wrong_token != real_token

        with A2ATaskUoW(tenant_id) as uow:
            assert uow.tasks is not None
            assert uow.tasks.release_notification_claim(event_id, claim_token=wrong_token) is False
            # The real owner's lease is untouched — it can still finalize with its own token.
            assert uow.tasks.mark_notification_published(event_id, claim_token=real_token) is True
