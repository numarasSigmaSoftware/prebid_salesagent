"""Delivery targets survive the UoW that produced them.

`claim_delivery_targets` returns `PushNotificationTarget` — a plain
dataclass — specifically so the outbound worker can read `url` and
`authentication_token` AFTER the session is closed. `webhook_delivery_service`
does exactly that: it snapshots inside `with PushNotificationConfigUoW(...)`
and then closes the session before any outbound request, retry sleep, or queue
operation, so a detached ORM instance would raise `DetachedInstanceError` on
first attribute access — on the delivery path, in a background thread.

Nothing graded that seam. Reverting the repository to return ORM rows left
`tests/unit` at 6110 passed, and no test imported `PushNotificationConfigUoW`
at all.
"""

from __future__ import annotations

import pytest

from tests.harness._base import IntegrationEnv

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


class _PushConfigEnv(IntegrationEnv):
    """Bare integration env: binds the factories, patches nothing."""

    EXTERNAL_PATCHES: dict[str, str] = {}


# claim_delivery_targets binds a delivery to one media buy's callbacks, so the
# seeded registration has to carry the media_buy_id the claim asks for.
_MEDIA_BUY_ID = "snap-media-buy"
_EVENT_KEY = "snap-event-key"


def _seed_config(tenant_id: str, principal_id: str) -> None:
    from tests.factories import MediaBuyFactory, PrincipalFactory, PushNotificationConfigFactory, TenantFactory

    tenant = TenantFactory(tenant_id=tenant_id)
    principal = PrincipalFactory(tenant=tenant, principal_id=principal_id)
    # push_notification_configs.media_buy_id carries a real FK.
    MediaBuyFactory(tenant=tenant, principal=principal, media_buy_id=_MEDIA_BUY_ID)
    PushNotificationConfigFactory(
        tenant=tenant,
        principal=principal,
        media_buy_id=_MEDIA_BUY_ID,
        url="https://buyer.example/webhook",
        authentication_type="Bearer",
        authentication_token="tok-123456789012345",
        is_active=True,
    )


class TestDeliveryTargetsOutliveTheirSession:
    def test_every_field_is_readable_after_the_uow_closes(self, integration_db):
        """The exact access pattern webhook_delivery_service uses."""
        from src.core.database.repositories.uow import PushNotificationConfigUoW

        tenant_id, principal_id = "snap-tenant", "snap-principal"
        with _PushConfigEnv(tenant_id=tenant_id, principal_id=principal_id) as env:
            _seed_config(tenant_id, principal_id)
            env._commit_factory_data()

            # The seam under test is the UoW closing, not the env's. Stay inside
            # the env (which owns the seeded rows for the test's lifetime) and
            # let the UoW's own session open and close around the read.
            with PushNotificationConfigUoW(tenant_id) as uow:
                assert uow.push_notification_configs is not None
                targets = uow.push_notification_configs.claim_delivery_targets(principal_id, _MEDIA_BUY_ID, _EVENT_KEY)

        # Session is closed here. A detached ORM row raises on first attribute
        # read; a snapshot does not.
        assert len(targets) == 1
        target = targets[0]
        assert target.url == "https://buyer.example/webhook"
        assert target.authentication_type == "Bearer"
        assert target.authentication_token == "tok-123456789012345"
        # Touch the remaining fields too — the worker reads these on the
        # circuit-breaker and auth-blocked paths.
        assert target.webhook_secret is None or isinstance(target.webhook_secret, str)
        assert target.auth_blocked_at is None or hasattr(target.auth_blocked_at, "year")

    def test_the_snapshot_is_not_an_orm_instance(self, integration_db):
        """Pins the mechanism, not just the symptom.

        A lazily-loaded ORM row can read fine inside an expire_on_commit=False
        session and still fail in the worker, so "it worked" is not enough —
        the returned object must carry no session state at all.
        """
        from sqlalchemy import inspect as sa_inspect

        from src.core.database.repositories.push_notification_config import PushNotificationTarget
        from src.core.database.repositories.uow import PushNotificationConfigUoW

        tenant_id, principal_id = "snap-tenant-2", "snap-principal-2"
        with _PushConfigEnv(tenant_id=tenant_id, principal_id=principal_id) as env:
            _seed_config(tenant_id, principal_id)
            env._commit_factory_data()

            # The seam under test is the UoW closing, not the env's. Stay inside
            # the env (which owns the seeded rows for the test's lifetime) and
            # let the UoW's own session open and close around the read.
            with PushNotificationConfigUoW(tenant_id) as uow:
                assert uow.push_notification_configs is not None
                targets = uow.push_notification_configs.claim_delivery_targets(principal_id, _MEDIA_BUY_ID, _EVENT_KEY)

        assert isinstance(targets[0], PushNotificationTarget)
        try:
            sa_inspect(targets[0])
        except Exception:
            return  # Not an ORM-mapped object at all, which is the contract.
        pytest.fail("delivery target is SQLAlchemy-mapped, so it carries session state into the worker")
