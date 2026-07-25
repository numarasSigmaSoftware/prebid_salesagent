"""Durable reporting events use random wire IDs and session-affine fences."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from src.core.database.repositories.delivery import DeliveryRepository
from src.core.database.repositories.uow import WebhookDeliveryUoW
from src.services.downstream_reconciliation import _downstream_operation_fence
from tests.factories import MediaBuyFactory, PrincipalFactory, TenantFactory

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


def _new_reporting_scope() -> tuple[str, str, str]:
    tenant = TenantFactory(tenant_id=f"event-tenant-{uuid4().hex[:8]}")
    principal = PrincipalFactory(tenant=tenant, principal_id=f"event-principal-{uuid4().hex[:8]}")
    media_buy = MediaBuyFactory(
        tenant=tenant,
        principal=principal,
        media_buy_id=f"event-buy-{uuid4().hex[:8]}",
    )
    return tenant.tenant_id, principal.principal_id, media_buy.media_buy_id


def _claim_reporting_event(
    tenant_id: str,
    principal_id: str,
    media_buy_id: str,
    logical_event_key: str,
    barrier: Barrier,
):
    barrier.wait(timeout=5)
    with WebhookDeliveryUoW(tenant_id) as uow:
        assert uow.webhook_delivery_logs is not None
        return uow.webhook_delivery_logs.claim_event(
            principal_id=principal_id,
            media_buy_id=media_buy_id,
            webhook_url="https://buyer.example/reporting",
            logical_event_key=logical_event_key,
            task_type="media_buy_delivery",
            notification_type="scheduled",
        )


def test_reporting_event_claim_is_random_and_retry_stable(integration_db) -> None:
    from tests.harness._base import BareIntegrationEnv

    with BareIntegrationEnv():
        tenant = TenantFactory(tenant_id=f"event-tenant-{uuid4().hex[:8]}")
        principal = PrincipalFactory(tenant=tenant, principal_id=f"event-principal-{uuid4().hex[:8]}")
        media_buy = MediaBuyFactory(
            tenant=tenant,
            principal=principal,
            media_buy_id=f"event-buy-{uuid4().hex[:8]}",
        )

        with WebhookDeliveryUoW(tenant.tenant_id) as uow:
            assert uow.webhook_delivery_logs is not None
            first = uow.webhook_delivery_logs.claim_event(
                principal_id=principal.principal_id,
                media_buy_id=media_buy.media_buy_id,
                webhook_url="https://buyer.example/reporting",
                logical_event_key="a" * 64,
                task_type="media_buy_delivery",
                notification_type="scheduled",
            )
        with WebhookDeliveryUoW(tenant.tenant_id) as uow:
            assert uow.webhook_delivery_logs is not None
            retry = uow.webhook_delivery_logs.claim_event(
                principal_id=principal.principal_id,
                media_buy_id=media_buy.media_buy_id,
                webhook_url="https://buyer.example/reporting",
                logical_event_key="a" * 64,
                task_type="media_buy_delivery",
                notification_type="scheduled",
            )
            next_event = uow.webhook_delivery_logs.claim_event(
                principal_id=principal.principal_id,
                media_buy_id=media_buy.media_buy_id,
                webhook_url="https://buyer.example/reporting",
                logical_event_key="b" * 64,
                task_type="media_buy_delivery",
                notification_type="scheduled",
            )

        assert retry == first
        assert UUID(first.idempotency_key).version == 4
        assert UUID(next_event.idempotency_key).version == 4
        assert next_event.idempotency_key != first.idempotency_key
        assert (first.sequence_number, next_event.sequence_number) == (1, 2)


def test_success_update_preserves_logical_claim_identity(integration_db) -> None:
    """Recording delivery success must not turn the next retry into a new event."""
    from datetime import UTC, datetime

    from tests.harness._base import BareIntegrationEnv

    with BareIntegrationEnv():
        tenant_id, principal_id, media_buy_id = _new_reporting_scope()
        with WebhookDeliveryUoW(tenant_id) as uow:
            assert uow.webhook_delivery_logs is not None
            assert uow.session is not None
            first = uow.webhook_delivery_logs.claim_event(
                principal_id=principal_id,
                media_buy_id=media_buy_id,
                webhook_url="https://buyer.example/reporting",
                logical_event_key="c" * 64,
                task_type="media_buy_delivery",
                notification_type="scheduled",
            )
            DeliveryRepository(uow.session, tenant_id).create_log(
                log_id=first.idempotency_key,
                principal_id=principal_id,
                media_buy_id=media_buy_id,
                webhook_url="https://buyer.example/reporting",
                task_type="media_buy_delivery",
                status="success",
                sequence_number=first.sequence_number,
                notification_type="scheduled",
                completed_at=datetime.now(UTC),
            )

        with WebhookDeliveryUoW(tenant_id) as uow:
            assert uow.webhook_delivery_logs is not None
            retry = uow.webhook_delivery_logs.claim_event(
                principal_id=principal_id,
                media_buy_id=media_buy_id,
                webhook_url="https://buyer.example/reporting",
                logical_event_key="c" * 64,
                task_type="media_buy_delivery",
                notification_type="scheduled",
            )

        assert retry.idempotency_key == first.idempotency_key
        assert retry.sequence_number == first.sequence_number
        assert retry.status == "success"


@pytest.mark.parametrize(
    ("event_keys", "expected_same_id"),
    [
        (("same" * 16, "same" * 16), True),
        (("left" * 16, "rght" * 16), False),
    ],
)
def test_concurrent_reporting_claims_are_stable_and_monotonic(
    integration_db,
    event_keys: tuple[str, str],
    expected_same_id: bool,
) -> None:
    """Admission serialization closes both empty-table allocation races."""
    from tests.harness._base import BareIntegrationEnv

    with BareIntegrationEnv():
        tenant_id, principal_id, media_buy_id = _new_reporting_scope()
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    _claim_reporting_event,
                    tenant_id,
                    principal_id,
                    media_buy_id,
                    event_key,
                    barrier,
                )
                for event_key in event_keys
            ]
            claims = [future.result(timeout=5) for future in futures]

    if expected_same_id:
        assert claims[0] == claims[1]
        assert claims[0].sequence_number == 1
    else:
        assert {claim.sequence_number for claim in claims} == {1, 2}
        assert claims[0].idempotency_key != claims[1].idempotency_key


def test_operation_fence_keeps_backend_affinity_across_commits(integration_db) -> None:
    with _downstream_operation_fence(f"pid-{uuid4()}") as session:
        first_pid = session.scalar(select(func.pg_backend_pid()))
        session.commit()
        second_pid = session.scalar(select(func.pg_backend_pid()))
        session.commit()
        third_pid = session.scalar(select(func.pg_backend_pid()))

    assert first_pid == second_pid == third_pid


def test_same_scope_operation_fences_serialize(integration_db) -> None:
    scope = f"serial-{uuid4()}"
    first_entered = Event()
    release_first = Event()
    second_entered = Event()

    def hold_first() -> None:
        with _downstream_operation_fence(scope):
            first_entered.set()
            assert release_first.wait(timeout=5)

    def enter_second() -> None:
        assert first_entered.wait(timeout=5)
        with _downstream_operation_fence(scope):
            second_entered.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(hold_first)
        second_future = executor.submit(enter_second)
        assert first_entered.wait(timeout=5)
        assert not second_entered.wait(timeout=0.2)
        release_first.set()
        first_future.result(timeout=5)
        second_future.result(timeout=5)

    assert second_entered.is_set()
