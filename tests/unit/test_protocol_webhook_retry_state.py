"""Durable retry-state invariants for protocol webhook delivery."""

from unittest.mock import patch

import pytest

from src.core.database.models import DELIVERY_TASK_TYPE
from src.services.protocol_webhook_service import DeliveryLogPersistenceError, ProtocolWebhookService


@pytest.mark.asyncio
async def test_delivery_is_not_sent_when_initial_retry_state_cannot_be_persisted() -> None:
    service = ProtocolWebhookService()
    payload = {
        "task_id": "media-buy-1",
        "idempotency_key": "stable-delivery-key",
        "result": {
            "notification_type": "scheduled",
            "sequence_number": 1,
        },
    }
    metadata = {
        "task_type": DELIVERY_TASK_TYPE,
        "tenant_id": "tenant-1",
        "principal_id": "principal-1",
        "media_buy_id": "media-buy-1",
    }

    with (
        patch.object(
            service,
            "_write_delivery_log",
            side_effect=DeliveryLogPersistenceError("database unavailable"),
        ),
        patch.object(service._session, "post") as mock_post,
        pytest.raises(DeliveryLogPersistenceError, match="database unavailable"),
    ):
        await service._send_with_retry_and_logging(
            url="https://buyer.example.com/reporting",
            payload=payload,
            headers={"Content-Type": "application/json"},
            metadata=metadata,
        )

    mock_post.assert_not_called()
    await service.close()
