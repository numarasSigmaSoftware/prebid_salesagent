"""Task callbacks resolve only their exact durable originating registration."""

from uuid import uuid4

import pytest

from src.core.database.repositories.uow import PushNotificationConfigUoW
from tests.factories import MediaBuyFactory, PrincipalFactory, PushNotificationConfigFactory, TenantFactory
from tests.harness._base import BareIntegrationEnv

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


def test_deleted_origin_is_not_resurrected_by_same_url_sibling(integration_db) -> None:
    with BareIntegrationEnv():
        tenant = TenantFactory(tenant_id=f"task-push-{uuid4().hex[:8]}")
        principal = PrincipalFactory(tenant=tenant, principal_id=f"task-principal-{uuid4().hex[:8]}")
        media_buy = MediaBuyFactory(tenant=tenant, principal=principal)
        shared_url = "https://buyer.example/task-callback"
        origin = PushNotificationConfigFactory(
            id=f"origin-{uuid4().hex[:8]}",
            tenant=tenant,
            principal=principal,
            session_id="context-origin",
            media_buy_id=media_buy.media_buy_id,
            url=shared_url,
            is_active=False,
        )
        PushNotificationConfigFactory(
            id=f"sibling-{uuid4().hex[:8]}",
            tenant=tenant,
            principal=principal,
            session_id="context-sibling",
            media_buy_id=media_buy.media_buy_id,
            url=shared_url,
            is_active=True,
        )

        with PushNotificationConfigUoW(tenant.tenant_id) as uow:
            assert uow.push_notification_configs is not None
            resolved = uow.push_notification_configs.get_active_for_task(
                config_id=origin.id,
                principal_id=principal.principal_id,
                media_buy_id=media_buy.media_buy_id,
                session_id="context-origin",
            )

    assert resolved is None
