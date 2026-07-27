"""Reporting-webhook capability validation through the real wire transports."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from src.core.reporting_capabilities import build_daily_reporting_capabilities
from tests.harness.transport import Transport
from tests.helpers.delivery_fixtures import DAILY_REPORTING_WEBHOOK

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

_WIRE_TRANSPORTS = [Transport.MCP, Transport.A2A, Transport.REST]


def _hourly_webhook() -> dict[str, Any]:
    return {**DAILY_REPORTING_WEBHOOK, "reporting_frequency": "hourly"}


def _assert_unsupported_frequency(result: Any, frequency: str) -> None:
    from tests.helpers import assert_envelope_shape

    assert result.is_error, f"Expected error, got payload: {result.payload}"
    assert_envelope_shape(
        result.wire_error_envelope,
        "UNSUPPORTED_FEATURE",
        recovery="correctable",
        message_substr=frequency,
    )
    errors = (result.wire_error_envelope or {}).get("errors", [])
    assert errors and "daily" in (errors[0].get("suggestion") or "").lower()


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

        _assert_unsupported_frequency(result, "hourly")

    def test_product_without_webhook_support_is_rejected(self, env_with_product) -> None:
        env, product, pricing_option = env_with_product
        product.reporting_capabilities = build_daily_reporting_capabilities(
            supports_webhooks=False,
            available_metrics=("impressions", "spend"),
        )

        result = env.call_via(
            Transport.REST,
            req=self._request(product, pricing_option, dict(DAILY_REPORTING_WEBHOOK)),
        )

        _assert_unsupported_frequency(result, "daily")


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

        _assert_unsupported_frequency(result, "hourly")

    def test_product_without_webhook_support_is_rejected(self, env_with_media_buy) -> None:
        env, media_buy, product = env_with_media_buy
        product.reporting_capabilities = build_daily_reporting_capabilities(
            supports_webhooks=False,
            available_metrics=("impressions", "spend"),
        )

        result = env.call_via(
            Transport.REST,
            req=self._request(media_buy, dict(DAILY_REPORTING_WEBHOOK)),
        )

        _assert_unsupported_frequency(result, "daily")
