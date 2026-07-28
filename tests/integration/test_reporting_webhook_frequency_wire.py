"""Reporting-webhook capability validation through the real wire transports."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.core.reporting_capabilities import build_daily_reporting_capabilities
from tests.harness.transport import Transport, extract_wire_suggestion
from tests.helpers.delivery_assertions import MediaBuyIdMatcher, SessionMatcher
from tests.helpers.delivery_fixtures import DAILY_REPORTING_WEBHOOK

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

_WIRE_TRANSPORTS = [Transport.MCP, Transport.A2A, Transport.REST]


def _hourly_webhook() -> dict[str, Any]:
    return {**DAILY_REPORTING_WEBHOOK, "reporting_frequency": "hourly"}


def _requested_metrics_webhook(*metrics: str) -> dict[str, Any]:
    return {**DAILY_REPORTING_WEBHOOK, "requested_metrics": list(metrics)}


def _assert_unsupported_capability(result: Any, message: str, suggestion: str) -> None:
    assert result.is_error, f"Expected error, got payload: {result.payload}"
    result.assert_wire_error(
        "UNSUPPORTED_FEATURE",
        message_substr=message,
        require_suggestion=True,
    )
    wire_suggestion = extract_wire_suggestion(result.wire_error_envelope)
    assert wire_suggestion is not None
    assert suggestion in wire_suggestion.lower()


class TestCreateReportingWebhookFrequencyWire:
    @pytest.fixture
    def env_with_product(self, integration_db):
        from tests.harness.media_buy_create import MediaBuyCreateEnv

        with MediaBuyCreateEnv() as env:
            _tenant, _principal, product, pricing_option = env.setup_media_buy_data()
            yield env, product, pricing_option

    @staticmethod
    def _request(product: Any, pricing_option: Any, reporting_webhook: dict[str, Any]):
        from src.core.schemas import CreateMediaBuyRequest

        now = datetime.now(UTC)
        return CreateMediaBuyRequest(
            brand={"domain": "reporting-frequency.example.com"},
            start_time=now + timedelta(days=1),
            end_time=now + timedelta(days=8),
            packages=[
                {
                    "product_id": product.product_id,
                    "budget": 5000.0,
                    "pricing_option_id": (
                        f"{pricing_option.pricing_model}_{pricing_option.currency.lower()}_"
                        f"{'fixed' if pricing_option.is_fixed else 'auction'}"
                    ),
                }
            ],
            idempotency_key=f"reporting-frequency-{uuid.uuid4().hex}",
            reporting_webhook=reporting_webhook,
        )

    @pytest.mark.parametrize("transport", _WIRE_TRANSPORTS)
    def test_unsupported_frequency_has_typed_wire_error(self, env_with_product, transport: Transport) -> None:
        env, product, pricing_option = env_with_product

        result = env.call_via(
            transport,
            req=self._request(product, pricing_option, _hourly_webhook()),
        )

        _assert_unsupported_capability(result, "hourly", "daily")

    @pytest.mark.parametrize("transport", _WIRE_TRANSPORTS)
    def test_product_without_webhook_support_is_rejected(
        self,
        env_with_product,
        transport: Transport,
    ) -> None:
        env, product, pricing_option = env_with_product
        product.reporting_capabilities = build_daily_reporting_capabilities(
            supports_webhooks=False,
            available_metrics=("impressions", "spend"),
        )

        result = env.call_via(
            transport,
            req=self._request(product, pricing_option, dict(DAILY_REPORTING_WEBHOOK)),
        )

        _assert_unsupported_capability(result, "daily", "daily")

    @pytest.mark.parametrize("transport", _WIRE_TRANSPORTS)
    def test_unavailable_requested_metric_has_typed_wire_error(
        self,
        env_with_product,
        transport: Transport,
    ) -> None:
        env, product, pricing_option = env_with_product

        result = env.call_via(
            transport,
            req=self._request(product, pricing_option, _requested_metrics_webhook("clicks")),
        )

        _assert_unsupported_capability(result, "clicks", "requested_metrics")

    @pytest.mark.parametrize("transport", _WIRE_TRANSPORTS)
    def test_implicit_base_metrics_are_accepted(
        self,
        env_with_product,
        transport: Transport,
    ) -> None:
        env, product, pricing_option = env_with_product
        product.reporting_capabilities = build_daily_reporting_capabilities(
            supports_webhooks=True,
            available_metrics=("clicks",),
        )

        result = env.call_via(
            transport,
            req=self._request(
                product,
                pricing_option,
                _requested_metrics_webhook("impressions", "spend"),
            ),
        )

        assert not result.is_error, result.wire_error_envelope


class TestUpdateReportingWebhookFrequencyWire:
    @pytest.fixture
    def env_with_media_buy(self, integration_db):
        from tests.bdd.conftest import _setup_existing_media_buy
        from tests.harness.media_buy_dual import MediaBuyDualEnv

        with MediaBuyDualEnv() as env:
            tenant, principal, product, _pricing_option = env.setup_media_buy_data()
            ctx: dict[str, Any] = {}
            _setup_existing_media_buy(ctx, env, tenant, principal, product)
            media_buy = ctx["existing_media_buy"]
            env._seeded_media_buy_id = media_buy.media_buy_id
            yield env, media_buy, product

    @staticmethod
    def _request(media_buy: Any, reporting_webhook: dict[str, Any]):
        from src.core.schemas import UpdateMediaBuyRequest

        return UpdateMediaBuyRequest(
            media_buy_id=media_buy.media_buy_id,
            reporting_webhook=reporting_webhook,
        )

    @pytest.mark.parametrize("transport", _WIRE_TRANSPORTS)
    def test_unsupported_frequency_has_typed_wire_error(self, env_with_media_buy, transport: Transport) -> None:
        env, media_buy, _product = env_with_media_buy

        result = env.call_via(
            transport,
            req=self._request(media_buy, _hourly_webhook()),
        )

        _assert_unsupported_capability(result, "hourly", "daily")

    @pytest.mark.parametrize("transport", _WIRE_TRANSPORTS)
    def test_product_without_webhook_support_is_rejected(
        self,
        env_with_media_buy,
        transport: Transport,
    ) -> None:
        env, media_buy, product = env_with_media_buy
        product.reporting_capabilities = build_daily_reporting_capabilities(
            supports_webhooks=False,
            available_metrics=("impressions", "spend"),
        )

        result = env.call_via(
            transport,
            req=self._request(media_buy, dict(DAILY_REPORTING_WEBHOOK)),
        )

        _assert_unsupported_capability(result, "daily", "daily")

    @pytest.mark.parametrize("transport", _WIRE_TRANSPORTS)
    def test_unavailable_requested_metric_has_typed_wire_error(
        self,
        env_with_media_buy,
        transport: Transport,
    ) -> None:
        env, media_buy, _product = env_with_media_buy

        result = env.call_via(
            transport,
            req=self._request(media_buy, _requested_metrics_webhook("clicks")),
        )

        _assert_unsupported_capability(result, "clicks", "requested_metrics")

    @pytest.mark.parametrize("transport", _WIRE_TRANSPORTS)
    def test_implicit_base_metrics_are_accepted(
        self,
        env_with_media_buy,
        transport: Transport,
    ) -> None:
        env, media_buy, product = env_with_media_buy
        product.reporting_capabilities = build_daily_reporting_capabilities(
            supports_webhooks=True,
            available_metrics=("clicks",),
        )

        result = env.call_via(
            transport,
            req=self._request(
                media_buy,
                _requested_metrics_webhook("impressions", "spend"),
            ),
        )

        assert not result.is_error, result.wire_error_envelope

    @pytest.mark.parametrize(
        "existing_webhook",
        [
            None,
            {**DAILY_REPORTING_WEBHOOK, "url": "https://old.example.com/reporting"},
        ],
        ids=["none-to-webhook", "replace-webhook"],
    )
    @pytest.mark.parametrize("transport", _WIRE_TRANSPORTS)
    def test_completed_update_persists_webhook_for_scheduler_readback(
        self,
        env_with_media_buy,
        existing_webhook: dict[str, Any] | None,
        transport: Transport,
    ) -> None:
        from src.core.database.repositories.media_buy import MediaBuyRepository
        from src.services.delivery_webhook_scheduler import DeliveryWebhookScheduler

        env, media_buy, _product = env_with_media_buy
        media_buy_id = media_buy.media_buy_id
        tenant_id = media_buy.tenant_id
        original_raw_request = {
            "brand": {"domain": "preserved.example.com"},
            **({"reporting_webhook": existing_webhook} if existing_webhook is not None else {}),
        }
        media_buy.raw_request = original_raw_request
        env._commit_factory_data()
        replacement = {**DAILY_REPORTING_WEBHOOK, "url": "https://new.example.com/reporting"}

        result = env.call_via(
            transport,
            req=self._request(media_buy, replacement),
        )

        assert not result.is_error, result.payload
        session = env.get_session()
        session.expire_all()
        persisted = MediaBuyRepository(session, tenant_id).get_by_id(media_buy_id)
        assert persisted is not None
        assert persisted.raw_request["brand"] == {"domain": "preserved.example.com"}
        assert persisted.raw_request["reporting_webhook"] == replacement

        scheduler = DeliveryWebhookScheduler()
        with patch.object(
            scheduler,
            "_send_report_for_media_buy",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_send:
            triggered = asyncio.run(
                scheduler.trigger_report_for_media_buy_by_id(
                    media_buy_id,
                    tenant_id,
                )
            )

        assert triggered is True
        mock_send.assert_awaited_once_with(
            MediaBuyIdMatcher(media_buy_id),
            replacement,
            SessionMatcher(),
            force=True,
        )

    @pytest.mark.parametrize("transport", _WIRE_TRANSPORTS)
    def test_approval_gated_update_does_not_mutate_active_webhook(
        self,
        env_with_media_buy,
        transport: Transport,
    ) -> None:
        from src.core.database.repositories.media_buy import MediaBuyRepository

        env, media_buy, _product = env_with_media_buy
        media_buy_id = media_buy.media_buy_id
        tenant_id = media_buy.tenant_id
        current = {**DAILY_REPORTING_WEBHOOK, "url": "https://current.example.com/reporting"}
        media_buy.raw_request = {"reporting_webhook": current}
        env._commit_factory_data()
        adapter = env.mock["update_adapter"].return_value
        adapter.manual_approval_required = True
        adapter.manual_approval_operations = ["update_media_buy"]
        replacement = {**DAILY_REPORTING_WEBHOOK, "url": "https://pending.example.com/reporting"}

        result = env.call_via(
            transport,
            req=self._request(media_buy, replacement),
        )

        assert not result.is_error, result.payload
        assert result.payload.status == "submitted"
        session = env.get_session()
        session.expire_all()
        persisted = MediaBuyRepository(session, tenant_id).get_by_id(media_buy_id)
        assert persisted is not None
        assert persisted.raw_request["reporting_webhook"] == current
