"""DeliveryPollEnv — integration test environment for _get_media_buy_delivery_impl.

Patches: get_adapter (external ad server) and DNS resolution for the outbound
SSRF gate (see "dns" below).
Real: MediaBuyUoW, get_principal_object, _get_pricing_options (all hit real DB),
and the full SSRF check itself -- literal-IP handling, blocked-hostname list,
and blocked-IP-range rejection are all still live; only the DNS lookup step
is faked.

Requires: integration_db fixture (creates test PostgreSQL DB).

Usage::

    @pytest.mark.requires_db
    def test_something(self, integration_db):
        with DeliveryPollEnv() as env:
            tenant = TenantFactory(tenant_id="t1")
            principal = PrincipalFactory(tenant=tenant, principal_id="p1")
            buy = MediaBuyFactory(tenant=tenant, principal=principal)
            env.set_adapter_response(buy.media_buy_id, impressions=5000)

            response = env.call_impl(media_buy_ids=[buy.media_buy_id])
            assert response.aggregated_totals.impressions == 5000.0

Available mocks via env.mock:
    "adapter"    -- get_adapter mock (only ad-server-external mock)
    "dns"        -- socket.gethostbyname mock (see _DNS_RESOLVED_IP); a test
                    driving a real webhook send (send_delivery_webhook /
                    run_delivery_batch) no longer depends on live network/DNS
                    to get past the outbound SSRF gate's hostname-resolution
                    step. The IP-range/blocked-hostname rejection logic this
                    gate exists for stays fully real and testable -- see
                    TestDeliveryPollEnvSsrfGate in
                    tests/integration/test_delivery_poll_behavioral.py.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.schemas import AdapterGetMediaBuyDeliveryResponse, GetMediaBuyDeliveryResponse
from tests.harness._base import IntegrationEnv
from tests.harness._mixins import DeliveryPollMixin

if TYPE_CHECKING:  # annotations only — keeps the harness import graph unchanged at runtime
    from src.core.database.models import MediaBuy
    from src.services.delivery_webhook_scheduler import DeliveryWebhookScheduler

# A genuinely public, non-blocked IP (example.com's own real address) --
# matches the existing repo-wide idiom for stubbing socket.gethostbyname in
# tests/unit/test_property_list_resolver.py, tests/unit/test_webhook_security.py,
# and tests/unit/test_ssrf_url_validator.py. Any hostname resolves to this
# under DeliveryPollEnv; the SSRF gate's IP-range check still runs on it for
# real and allows it, same as it would for a real public address.
_DNS_RESOLVED_IP = "93.184.216.34"


@contextmanager
def mock_webhook_post(
    scheduler: DeliveryWebhookScheduler,
    *,
    responses: Sequence[Any] | None = None,
    skip_retry_delays: bool = False,
) -> Iterator[MagicMock]:
    """Stub a scheduler's outbound webhook POST.

    Single source of truth for the mocked-POST shape every delivery-webhook
    integration test needs (CLAUDE.md DRY invariant — ``send_delivery_webhook``
    and ``run_delivery_batch`` share it instead of each building the
    ``MagicMock(status_code=200, ...)`` + ``patch.object(..._session, "post", ...)``
    pair). Reaches into
    ``webhook_service._session`` (private) because that is the only seam where
    the serialized outbound body is observable; if the service ever swaps its
    HTTP client this AttributeErrors loudly rather than silently no-op'ing.
    Everything above the HTTP call (delivery impl, derivation, sequence,
    serialization, the atomic final claim) runs for real.

    Yields the ``mock_post`` patch object so callers can assert on
    ``call_count`` / ``call_args_list`` after driving one or more sends.

    By default every call receives a 200 response. Pass ``responses`` to drive
    a finite sequence of transport outcomes while keeping the HTTP mock seam
    centralized in this harness. Set ``skip_retry_delays`` when those responses
    exercise retry backoff and wall-clock sleeps are irrelevant to the test.
    """
    patch_kwargs: dict[str, Any]
    if responses is None:
        mock_response = MagicMock(status_code=200, text="OK")
        mock_response.raise_for_status.return_value = None
        patch_kwargs = {"return_value": mock_response}
    else:
        patch_kwargs = {"side_effect": list(responses)}
    with ExitStack() as stack:
        mock_post = stack.enter_context(patch.object(scheduler.webhook_service._session, "post", **patch_kwargs))
        if skip_retry_delays:
            stack.enter_context(patch("src.services.protocol_webhook_service.asyncio.sleep", new_callable=AsyncMock))
        yield mock_post


@contextmanager
def mock_delivery_response(response: Any) -> Iterator[Any]:
    """Stub the delivery lookup the scheduler performs, with a fixed result.

    One seam ABOVE ``mock_send_notification``: that one short-circuits the send,
    this one short-circuits what the scheduler *reads* before deciding to send —
    letting a test drive the lookup-failure branches (a non-response result, or a
    response carrying advisory errors) without a real adapter failure.

    Lives here rather than as a ``patch`` in the test file so the behavioural-mock
    cap keeps shrinking (tests/unit/test_architecture_behavioral_mock_cap.py) and
    the patch target is stated once.
    """
    with patch(
        "src.services.delivery_webhook_scheduler._get_media_buy_delivery_impl",
        return_value=response,
    ) as mock_impl:
        yield mock_impl


@contextmanager
def mock_send_notification(scheduler: DeliveryWebhookScheduler, *, delivered: bool = True) -> Iterator[AsyncMock]:
    """Stub a scheduler's ``webhook_service.send_notification`` with a fixed outcome.

    Single source of truth for the mocked-``send_notification`` shape the claim/dedup
    integration tests need (CLAUDE.md DRY invariant — they were each hand-rolling the
    identical ``patch.object(..., new_callable=AsyncMock, return_value=<bool>)``). This
    is one seam ABOVE ``mock_webhook_post``: it short-circuits the whole send (payload
    build, POST, delivery-log write) with a boolean, where ``delivered=False`` models a
    permanent failure that makes ``_deliver_report`` raise ``RuntimeError``.

    Yields the ``AsyncMock`` so callers keep ``await_count`` / ``assert_not_awaited()``.
    """
    with patch.object(
        scheduler.webhook_service, "send_notification", new_callable=AsyncMock, return_value=delivered
    ) as mock_send:
        yield mock_send


class DeliveryPollEnv(DeliveryPollMixin, IntegrationEnv):
    """Integration test environment for _get_media_buy_delivery_impl.

    Mocks the adapter (external ad server) and DNS resolution (see module
    docstring). Everything else is real:
    - Real MediaBuyUoW -> real DB queries
    - Real get_principal_object -> real DB queries
    - Real _get_pricing_options -> real DB queries
    - Real SSRF gate (only its DNS-resolution step is stubbed -- see
      _DNS_RESOLVED_IP and the "dns" mock above)

    Fluent API (from DeliveryPollMixin):
        set_adapter_response(...)  -- configure adapter return for a media_buy_id
        set_adapter_error(exc)     -- make the adapter raise an exception
        call_impl(...)             -- call _get_media_buy_delivery_impl with real DB
    """

    EXTERNAL_PATCHES = {
        "adapter": "src.core.tools.media_buy_delivery.get_adapter",
        "dns": "src.core.security.url_validator.socket.gethostbyname",
    }
    REST_ENDPOINT = "/api/v1/media-buys/delivery"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._adapter_responses: dict[str, AdapterGetMediaBuyDeliveryResponse] = {}

    def _configure_mocks(self) -> None:
        self._configure_adapter_mock()
        self._configure_dns_mock()

    def _configure_dns_mock(self) -> None:
        """Stub DNS resolution so a real webhook send doesn't depend on live
        network access to clear the outbound SSRF gate's hostname-resolution
        step. See module docstring / _DNS_RESOLVED_IP for what stays real."""
        self.mock["dns"].return_value = _DNS_RESOLVED_IP

    def call_a2a(self, **kwargs: Any) -> GetMediaBuyDeliveryResponse:
        """Call get_media_buy_delivery via real AdCPRequestHandler — full A2A pipeline."""
        return self._run_a2a_handler("get_media_buy_delivery", GetMediaBuyDeliveryResponse, **kwargs)

    def call_mcp(self, **kwargs: Any) -> GetMediaBuyDeliveryResponse:
        """Call get_media_buy_delivery via Client(mcp) — full pipeline dispatch."""
        return self._run_mcp_client("get_media_buy_delivery", GetMediaBuyDeliveryResponse, **kwargs)

    def build_rest_body(self, **kwargs: Any) -> dict[str, Any]:
        """Convert kwargs to GetMediaBuyDeliveryBody shape for REST POST."""
        # Forward all request fields that the REST body accepts
        _BODY_FIELDS = (
            "media_buy_ids",
            "status_filter",
            "start_date",
            "end_date",
            "reporting_dimensions",
            "attribution_window",
            "include_package_daily_breakdown",
            "account",
        )
        return {k: kwargs[k] for k in _BODY_FIELDS if k in kwargs and kwargs[k] is not None}

    def parse_rest_response(self, data: dict[str, Any]) -> GetMediaBuyDeliveryResponse:
        """Parse REST JSON into GetMediaBuyDeliveryResponse."""
        return GetMediaBuyDeliveryResponse(**data)

    async def send_delivery_webhook(self, buy: MediaBuy) -> dict[str, Any]:
        """Force one delivery-webhook scheduler send for ``buy``; return the wire payload.

        Drives the REAL scheduler path (``_send_report_for_media_buy`` with
        ``force=True``) — delivery impl, sequence computation from
        WebhookDeliveryLog, payload serialization — mocking only the outbound
        HTTP POST. Returns the JSON body the buyer's webhook would receive
        (the webhook-only fields notification_type / sequence_number /
        next_expected_at live under ``result``).

        ``buy.raw_request`` must contain a ``reporting_webhook`` config.
        """
        from src.services.delivery_webhook_scheduler import DeliveryWebhookScheduler

        scheduler = DeliveryWebhookScheduler()
        with mock_webhook_post(scheduler) as mock_post:
            await scheduler._send_report_for_media_buy(
                buy, buy.raw_request["reporting_webhook"], self.get_session(), force=True
            )
        self.mock["post"] = mock_post
        assert mock_post.call_count == 1, "scheduler must send exactly one webhook"
        return mock_post.call_args.kwargs["json"]

    async def run_delivery_batch(self) -> list[dict[str, Any]]:
        """Run one REAL delivery-webhook batch (``_send_reports``); return the wire bodies sent.

        Drives the cross-tenant batch — selection, per-buy dedup gate, delivery
        impl, serialization — mocking only the outbound HTTP POST. Returns one
        JSON body per webhook actually sent (empty when the batch sends nothing),
        so a test can assert both the count and each wire ``result`` (the
        webhook-only fields live under ``result``) without hand-rolling a
        mock in the test body.
        """
        from src.services.delivery_webhook_scheduler import DeliveryWebhookScheduler

        scheduler = DeliveryWebhookScheduler()
        with mock_webhook_post(scheduler) as mock_post:
            await scheduler._send_reports()
        self.mock["post"] = mock_post
        return [call.kwargs["json"] for call in mock_post.call_args_list]
