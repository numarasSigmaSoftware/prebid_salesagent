"""
Delivery Webhook Scheduler

Sends daily delivery reports via webhooks for media buys that have configured reporting_webhook.
This runs as a background task and sends reports when GAM data is fresh (after 4 AM PT daily).
"""

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from adcp import create_mcp_webhook_payload
from adcp.types import GeneratedTaskStatus as AdcpTaskStatus
from adcp.types import MediaBuyStatus
from adcp.types.generated_poc.media_buy.get_media_buy_delivery_response import (
    NotificationType,
)  # TODO: no stable alias — response-level NotificationType differs from top-level
from sqlalchemy.orm import Session

from src.core.database.database_session import get_db_session
from src.core.database.models import DELIVERY_TASK_TYPE, MediaBuy
from src.core.database.repositories import MediaBuyRepository
from src.core.database.repositories.delivery import DeliveryRepository
from src.core.database.repositories.push_notification_config import PushNotificationConfigRepository
from src.core.helpers import enum_value
from src.core.reporting_capabilities import SUPPORTED_REPORTING_FREQUENCIES
from src.core.schemas import GetMediaBuyDeliveryRequest, GetMediaBuyDeliveryResponse
from src.core.tools._media_buy_status import (
    SERVING_PERSISTED_STATUSES,
    WEBHOOK_REPORTABLE_CANONICAL_STATUSES,
    WEBHOOK_TERMINAL_CANONICAL_STATUSES,
    WEBHOOK_TERMINAL_PERSISTED_STATUSES,
    derive_notification_type,
    resolve_canonical_status,
)
from src.core.tools.media_buy_delivery import _get_media_buy_delivery_impl
from src.core.utils import utc_flight_start
from src.services.protocol_webhook_service import get_protocol_webhook_service

logger = logging.getLogger(__name__)

# The production batch runs hourly; tests may override the interval through the
# environment without changing the shipped default.
DEFAULT_SLEEP_INTERVAL_SECONDS = 3600
# Configurable via env var for testing
SLEEP_INTERVAL_SECONDS = int(os.getenv("DELIVERY_WEBHOOK_INTERVAL") or str(DEFAULT_SLEEP_INTERVAL_SECONDS))

# Lease for the best-effort atomic "final webhook" claim. A claim older
# than this is treated as stale (crashed/failed worker) and can be re-claimed, so
# a stuck claim never strands the final. Comfortably longer than a real send
# (seconds) so an in-flight send is never reclaimed, and shorter than the hourly
# batch so a failed/crashed final is retried on the next batch.
FINAL_WEBHOOK_CLAIM_LEASE = timedelta(minutes=15)


def _delivery_lookup_is_usable(media_buy: MediaBuy, delivery_response: object) -> bool:
    """Whether a delivery lookup result can be reported on.

    Returns True when the result is usable, False for a LEGITIMATE skip, and RAISES
    on a real failure — mirroring ``_send_report_for_media_buy``'s documented contract,
    which reserves False for "unsupported frequency, dedup, no data, no URL" and says a
    failed delivery raises so the batch counts an error rather than logging a send.

    Extracted from ``_deliver_report`` to keep that method under the PLR0915 statement
    ceiling; the discrimination below is the whole reason this is not a one-liner.
    """
    if not isinstance(delivery_response, GetMediaBuyDeliveryResponse):
        # %r-style detail, not %s: this branch proved the object is NOT the response
        # model, so its type is unknown. A result that is not the model at all is none
        # of the contract's legitimate skips — returning False here made the batch print
        # "0 sent, 0 errors" for a terminal buy whose spec-required final could not be
        # built, hiding the failure entirely.
        raise RuntimeError(
            f"Delivery lookup for media buy {media_buy.media_buy_id} returned "
            f"{type(delivery_response).__name__}, not GetMediaBuyDeliveryResponse"
        )

    if delivery_response.errors is not None:
        # Log the ERRORS, not the response: GetMediaBuyDeliveryResponse.__str__ is a
        # human-readable envelope summary ("No delivery data found for the specified
        # period."), so "%s" of the model renders a success-shaped sentence with the
        # error payload absent — the one diagnostic this branch exists to emit.
        logger.warning(
            "`Couldn't get media_delivery` for %s. We received an error in the result. errors=%s",
            media_buy.media_buy_id,
            delivery_response.errors,
        )
        # Discriminate, don't blanket-skip. The impl emits two advisory shapes here:
        # MEDIA_BUY_NOT_FOUND when a requested buy isn't reportable — which IS the "no
        # data" case the contract lists as a legitimate False — and SERVICE_UNAVAILABLE
        # when the adapter actually failed. Returning False for both let a real adapter
        # failure count as a skip; raising for both would make every no-data buy an error.
        if any(e.code != "MEDIA_BUY_NOT_FOUND" for e in delivery_response.errors):
            raise RuntimeError(
                f"Delivery lookup for media buy {media_buy.media_buy_id} failed: "
                f"{[e.code for e in delivery_response.errors]}"
            )
        return False

    return True


class DeliveryWebhookScheduler:
    """Scheduler for sending delivery reports via webhooks."""

    def __init__(self) -> None:
        self.webhook_service = get_protocol_webhook_service()
        self.is_running = False
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the scheduler background task."""
        async with self._lock:
            if self.is_running:
                logger.warning("Delivery webhook scheduler is already running")
                return

            self.is_running = True
            self._task = asyncio.create_task(self._run_scheduler())
            logger.info("Delivery webhook scheduler started")

    async def stop(self) -> None:
        """Stop the scheduler background task."""
        async with self._lock:
            if not self.is_running:
                return

            self.is_running = False
            if self._task:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            logger.info("Delivery webhook scheduler stopped")

    async def _run_scheduler(self) -> None:
        """Main scheduler loop - runs on a fixed hourly cadence.

        Sends immediately on startup (duplicate check prevents re-sending if
        already sent in last 24 hours), then continues on hourly cadence.
        """
        while self.is_running:
            try:
                await self._send_reports()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in delivery webhook scheduler: {e}", exc_info=True)
            finally:
                # Wait before next batch
                await asyncio.sleep(SLEEP_INTERVAL_SECONDS)

    async def _send_reports(self) -> None:
        """Send reports for all active media buys with configured webhooks."""
        logger.info("Starting scheduled delivery report webhook batch")

        try:
            with get_db_session() as session:
                # Find all reportable media buys (cross-tenant scheduler query):
                # the serving set (including legacy aliases "ready"/"scheduled")
                # plus terminal completed, canceled, and rejected buys until a
                # successful final log exists.
                # This durable anti-join keeps required finals selectable across
                # arbitrarily long scheduler downtime.
                media_buys = MediaBuyRepository.get_reportable_for_delivery(
                    session,
                    serving_statuses=sorted(SERVING_PERSISTED_STATUSES),
                    terminal_statuses=sorted(WEBHOOK_TERMINAL_PERSISTED_STATUSES),
                )

                reports_sent = 0
                errors = 0

                for media_buy in media_buys:
                    try:
                        # Check if this media buy has a reporting webhook configured.
                        # NOT redundant with get_reportable_for_delivery's SQL
                        # predicate: that predicate is JSON key-presence
                        # (raw_request['reporting_webhook'] IS NOT NULL), a cheap
                        # SQL-level pre-filter; this is Python truthiness, which
                        # also excludes a present-but-null/false/{} value. Keep both.
                        raw_request = media_buy.raw_request or {}
                        reporting_webhook = raw_request.get("reporting_webhook")

                        if not reporting_webhook:
                            continue

                        # The status-only selection also matches pre-flight and
                        # paused rows the impl cannot report on. Resolve the
                        # same canonical status the impl would and skip them
                        # here, instead of invoking the full delivery impl
                        # every hour only to misread its MEDIA_BUY_NOT_FOUND
                        # advisory as a warning-worthy failure.
                        canonical = resolve_canonical_status(media_buy, datetime.now(UTC).date())
                        if canonical not in WEBHOOK_REPORTABLE_CANONICAL_STATUSES:
                            continue

                        # Send delivery report; only count it when a webhook
                        # actually went out (dedup/frequency skips return False).
                        if await self._send_report_for_media_buy(media_buy, reporting_webhook, session):
                            reports_sent += 1

                    except Exception as e:
                        logger.error(
                            "Error sending report for media buy %s (tenant %s, principal %s): %s",
                            media_buy.media_buy_id,
                            media_buy.tenant_id,
                            media_buy.principal_id,
                            e,
                            exc_info=True,
                        )
                        errors += 1

                logger.info(f"Daily delivery report batch complete: {reports_sent} sent, {errors} errors")

        except Exception as e:
            logger.error(f"Error in daily delivery report batch: {e}", exc_info=True)

    async def trigger_report_for_media_buy_by_id(self, media_buy_id: str, tenant_id: str) -> bool:
        """Manually trigger a delivery report for a single media buy by ID.

        This method manages its own database session to avoid detached instance errors.

        Args:
            media_buy_id: The media buy ID
            tenant_id: The tenant ID

        Returns:
            bool: True when a webhook was actually sent; False when the buy was
            LEGITIMATELY skipped (no reporting webhook configured, dedup, no data).

        Raises:
            Exception: propagated from the send path. A real failure must NOT be
                flattened into False here — the admin route already distinguishes
                three outcomes (sent / skipped / errored) and swallowing the
                exception collapsed the last two into one "Failed to trigger …
                check logs" banner, telling an operator the same thing whether
                nothing needed sending or the adapter actually broke. This mirrors
                _send_report_for_media_buy's contract rather than re-flattening it
                one layer up.
        """
        try:
            with get_db_session() as session:
                repo = MediaBuyRepository(session, tenant_id)
                media_buy = repo.get_by_id(media_buy_id)

                if not media_buy:
                    logger.warning(f"Cannot trigger report: Media buy {media_buy_id} not found")
                    return False

                raw_request = media_buy.raw_request or {}
                reporting_webhook = raw_request.get("reporting_webhook")

                if not reporting_webhook:
                    logger.warning(f"Cannot trigger report: No reporting_webhook configured for {media_buy_id}")
                    return False

                # force bypasses the frequency + 24h "scheduled" dedup so an
                # operator can re-send a fresh periodic report. It does NOT bypass
                # the final gate: a completed buy whose final was already delivered
                # is still skipped, so a manual trigger won't duplicate the final on
                # the read-check path (best-effort; #1606 for true exactly-once).
                return await self._send_report_for_media_buy(media_buy, reporting_webhook, session, force=True)
        except Exception as e:
            # Log here (this frame knows the media_buy_id) then RE-RAISE, so the caller
            # can tell a genuine failure from a legitimate skip.
            logger.error(f"Error manually triggering report for {media_buy_id}: {e}", exc_info=True)
            raise

    def _should_skip_send(
        self, delivery_repo: DeliveryRepository, media_buy: MediaBuy, *, is_final: bool, force: bool
    ) -> bool:
        """BEST-EFFORT read-only de-dup — True if this delivery webhook should NOT be sent.

        NOT a hard exactly-once guarantee. This is a pure read decision; the atomic
        concurrency CLAIM is taken later, just before the POST (see _deliver_report),
        so definitive no-send paths before the POST never hold a claim.
          - final: skip if a SUCCESSFUL "final" was already logged for this buy.
            Applies EVEN under ``force`` — a manual re-trigger must never duplicate a
            delivered final. Keys on a *successful* final, so a retry after a FAILED
            final still goes through, and it fires regardless of the 24h window (so
            the status scheduler flipping the buy to persisted "completed" before this
            hourly batch can't leave the spec-required final unsent).
          - scheduled: 24h rolling dedup, bypassed by ``force`` so an operator can
            re-send a fresh periodic report on demand.
        """
        if is_final:
            if delivery_repo.has_successful_final(media_buy.media_buy_id):
                logger.info("Final delivery webhook already sent for media buy %s – skipping", media_buy.media_buy_id)
                return True
            return False
        if force:
            return False
        one_day_ago = datetime.now(UTC) - timedelta(hours=24)
        existing_log = delivery_repo.get_recent_successful_log(
            media_buy.media_buy_id, task_type=DELIVERY_TASK_TYPE, since=one_day_ago
        )
        if existing_log:
            logger.info(
                "Daily delivery webhook for media buy %s already sent (log id %s) – skipping",
                media_buy.media_buy_id,
                existing_log.id,
            )
            return True
        return False

    def _claim_final_webhook(self, session: Session, media_buy: MediaBuy) -> datetime | None:
        """Atomically claim the buy's ONE final webhook. Returns the claim token
        (the exact ``claimed_at`` written) if THIS worker won, else None.

        Best-effort concurrency guard: a conditional UPDATE that wins only
        when the claim is unset or stale (older than FINAL_WEBHOOK_CLAIM_LEASE, so a
        crashed worker's claim self-heals). Runs on the caller's ``session`` and
        COMMITS it so the claim is immediately visible to a racing worker (whose
        UPDATE then matches 0 rows and loses). The returned token is passed to
        _release_final_webhook_claim on a definitive failure/no-send so the claim doesn't
        block an immediate retry for the whole lease. Does NOT close the
        post-POST window — #1606. That window is not crash-only: a failure of the
        success-path ``_write_delivery_log`` (which raises) is indistinguishable
        here from a failed send, so the claim is released and the final re-sends.
        """
        now = datetime.now(UTC)
        won = MediaBuyRepository(session, media_buy.tenant_id).try_claim_final_webhook(
            media_buy.media_buy_id, now=now, stale_before=now - FINAL_WEBHOOK_CLAIM_LEASE
        )
        session.commit()
        return now if won else None

    def _release_final_webhook_claim(self, session: Session, media_buy: MediaBuy, claimed_at: datetime) -> None:
        """Best-effort release of THIS worker's final claim after a definitive
        failure/no-send, so an immediate retry isn't blocked for the whole lease.

        Token-guarded by ``claimed_at`` (see release_final_webhook_claim) so it can
        never clear a newer owner's claim. Swallows its own errors — the lease is the
        real guarantee, so a failed release just falls back to lease recovery.
        """
        try:
            MediaBuyRepository(session, media_buy.tenant_id).release_final_webhook_claim(
                media_buy.media_buy_id, claimed_at=claimed_at
            )
            session.commit()
        except Exception:  # best-effort; lease recovery is the guarantee
            logger.debug(
                "Failed to release final claim for media buy %s (lease will recover)",
                media_buy.media_buy_id,
                exc_info=True,
            )

    async def _send_report_for_media_buy(
        self, media_buy: MediaBuy, reporting_webhook: dict[str, Any], session: Session, force: bool = False
    ) -> bool:
        """Send a delivery report for a single media buy.

        Args:
            media_buy: MediaBuy database model
            reporting_webhook: Webhook configuration dict
            session: Database session
            force: If True, bypass frequency + the 24h "scheduled" dedup. Does
                NOT bypass the final gate, so a manual re-trigger won't emit a
                duplicate final on the read-check path (best-effort; a crash /
                concurrency window remains — see #1606).

        Returns:
            True when a webhook was actually delivered; False when the buy was
            legitimately skipped (unsupported frequency, dedup, no data, no
            URL). A failed delivery RAISES so the caller counts it as an
            error instead of a send.
        """
        try:
            delivery_repo = DeliveryRepository(session, media_buy.tenant_id)

            # Determine reporting frequency from AdCP config (hourly, daily, monthly)
            raw_freq = str(reporting_webhook.get("reporting_frequency") or "daily").lower()

            # Computed BEFORE the cadence gate below, which must not strand it.
            is_final = (
                resolve_canonical_status(media_buy, datetime.now(UTC).date()) in WEBHOOK_TERMINAL_CANONICAL_STATUSES
            )

            # Same set the create/update capability validator rejects against. If
            # these drift, an unsupported cadence is accepted at booking and then
            # silently never sent — the acknowledged-but-never-fires state
            # validate_reporting_webhook_frequency exists to prevent.
            #
            # `not is_final` is load-bearing, not defensive. This gate reads
            # reporting_frequency, and until that key was corrected it read a key
            # raw_request never contained, so it never fired. Live, it applies to a
            # population that predates the booking-time validator: create used to
            # accept hourly/monthly with only a warning that they "will be ignored
            # until implemented", and no migration normalises those rows. Skipping
            # their PERIODIC sends is the intended behavior. Skipping their FINAL
            # send is not — the anti-join re-selects a buy until a success-final row
            # exists, so returning False here would mean that row is never written
            # and the terminal notification never goes out, for the whole life of
            # the buy. That is the exact outcome the claim/lease machinery below
            # exists to make exactly-once rather than never, and _should_skip_send
            # is already final-aware for the same reason.
            if not force and not is_final and raw_freq not in SUPPORTED_REPORTING_FREQUENCIES:
                logger.warning(
                    "Skipping reporting webhook with frequency '%s' for media buy %s – "
                    "supported delivery-webhook frequencies are: %s",
                    raw_freq,
                    media_buy.media_buy_id,
                    ", ".join(sorted(SUPPORTED_REPORTING_FREQUENCIES)),
                )
                return False

            # Best-effort read-only de-dup (no claim here — the atomic concurrency
            # claim is taken inside _deliver_report, just before the POST).
            if self._should_skip_send(delivery_repo, media_buy, is_final=is_final, force=force):
                return False

            return await self._deliver_report(session, delivery_repo, media_buy, reporting_webhook, is_final=is_final)

        except Exception as e:
            # Re-raise for the caller (batch loop / manual trigger) to own the
            # single ERROR line. Log at DEBUG here to avoid a duplicate full
            # traceback on the common send_notification -> False path.
            logger.debug("Error sending delivery report for media buy %s: %s", media_buy.media_buy_id, e, exc_info=True)
            raise

    async def _deliver_report(
        self,
        session: Session,
        delivery_repo: DeliveryRepository,
        media_buy: MediaBuy,
        reporting_webhook: dict[str, Any],
        *,
        is_final: bool,
    ) -> bool:
        """Build the delivery report and POST it.

        Returns True when a webhook was delivered, False on a definitive no-send
        (no delivery data, no URL, or the final claim was lost to a concurrent
        worker); RAISES on a failed send so the batch counts an error.

        The atomic final CLAIM is taken here, immediately before the POST — so the
        no-send checks above it never hold a claim — and is RELEASED (token-guarded)
        if the send fails or the claim is lost, so an immediate retry isn't blocked
        for the whole lease. A successful POST keeps the claim; the crash-after-POST
        duplicate window is the best-effort residual tracked in #1606.
        """
        # Reporting period for daily frequency: yesterday (full day).
        start_date_obj = datetime.now(UTC).date() - timedelta(days=1)
        end_date_obj = datetime.now(UTC)

        # Create a ResolvedIdentity for the delivery call. Imported lazily ON
        # PURPOSE: tests inject a testing_context by patching
        # src.core.resolved_identity.ResolvedIdentity, which only intercepts a
        # call-time import — hoisting this to module scope breaks that seam
        # (test_scheduler_uses_simulated_path_in_testing_mode).
        from src.core.resolved_identity import ResolvedIdentity

        identity = ResolvedIdentity(
            principal_id=media_buy.principal_id,
            tenant_id=media_buy.tenant_id,
            tenant={"tenant_id": media_buy.tenant_id},
            protocol="rest",
        )

        # The scheduler requests serving plus every terminal state in the
        # reporting-webhook termination contract.
        req = GetMediaBuyDeliveryRequest(
            media_buy_ids=[media_buy.media_buy_id],
            status_filter=[MediaBuyStatus(s) for s in sorted(WEBHOOK_REPORTABLE_CANONICAL_STATUSES)],
            start_date=start_date_obj.strftime("%Y-%m-%d"),
            end_date=end_date_obj.strftime("%Y-%m-%d"),
            context=None,
        )

        delivery_response = _get_media_buy_delivery_impl(req, identity)

        if not _delivery_lookup_is_usable(media_buy, delivery_response):
            return False

        # Sequence number for this webhook: max SUCCESSFULLY DELIVERED
        # sequence + 1 (spec: "Sequential notification number ... starts at
        # 1"). Failed/retrying sends also log the sequence they attempted;
        # counting them — while the dedup above counts only successes —
        # would burn numbers the buyer never received, so a buyer's
        # first-ever webhook could start above 1. A query failure
        # propagates and aborts this send loudly: a quiet fallback to 1
        # would put an already-consumed sequence on the wire.
        sequence_number = (
            delivery_repo.get_max_sequence_number(media_buy.media_buy_id, task_type=DELIVERY_TASK_TYPE) + 1
        )

        # Set webhook-specific metadata directly on the response model.
        # These fields are webhook-only ("only present in webhook deliveries" —
        # get-media-buy-delivery-response.json @ v3.1-04f59d2d5), so the polling
        # impl never sets them. On the polling-response path this is the only
        # place they are attached to the wire — NOT a repo-wide sole emitter:
        # webhook_delivery_service.send_delivery_webhook (GAM reporting, delivery
        # simulator) attaches its own notification_type / sequence_number /
        # next_expected_at from an in-memory counter. That emitter IS graded by the
        # shared next_expected_at oracle, but not by the omission or pairing ones —
        # reconciliation tracked in #1624.
        #
        # The body emitted here is governed by media-buy-delivery-webhook-result.json
        # (@ 3.1.0, the release the schema entered — the v3.1-04f59d2d5 ref above
        # predates it and does not contain that file, and it is not among the
        # vendored fixtures under tests/fixtures/adcp_schemas_pinned/, so it is not
        # graded offline). It lists notification_type in `required` — the constraint
        # the zero-deliveries note below reasons about.
        #
        # notification_type: derived from the reported statuses — "final" when
        # every buy will never produce more data ("one final notification when
        # the campaign completes", optimization-reporting.mdx §Publisher
        # Commitment; extended to canceled/rejected per webhooks.mdx
        # §Termination — see derive_notification_type()'s docstring), "scheduled"
        # otherwise.
        derived = derive_notification_type(enum_value(d.status) for d in delivery_response.media_buy_deliveries or [])
        notification_type = NotificationType(derived) if derived else None
        delivery_response.notification_type = notification_type

        # next_expected_at: only present when notification_type is not "final"
        # (spec, same schema — a non-nullable date-time, so a final webhook
        # must OMIT the field; leaving it None lets the response's
        # exclude-None serialization drop it from the wire). Daily
        # frequency -> start of next day (UTC).
        #
        # Branch on the enum rather than re-comparing the raw string: the value
        # was already narrowed to NotificationType one line up, and a bare
        # literal here would silently stop matching if the vocabulary changed.
        if notification_type is NotificationType.final:
            delivery_response.next_expected_at = None
        elif notification_type is NotificationType.scheduled:
            next_day = datetime.now(UTC).date() + timedelta(days=1)
            delivery_response.next_expected_at = utc_flight_start(next_day)
        # derived is None (zero deliveries) -> leave next_expected_at unset;
        # notification_type is None too, so the pair stays consistent. Unreachable from
        # this scheduler today (a single-ID request yields >=1 row, or an advisory that
        # aborts earlier); if it ever becomes reachable the body would omit
        # notification_type, which the webhook-result schema marks REQUIRED -- add an
        # explicit empty-deliveries no-send guard rather than emitting that body.

        delivery_response.sequence_number = sequence_number
        # TODO: Check for reporting_delayed status. Co-edit site: the shared
        # assert_partial_data_pairing oracle (tests/helpers/delivery_assertions.py)
        # pins partial_data False deliberately, so implementing real partial-data
        # reporting must update that helper and its callers in the same change.
        delivery_response.partial_data = False
        # unavailable_count is "only present in webhook deliveries when partial_data
        # is true" (schema description) — leave None (excluded from the wire) until
        # partial_data reporting is implemented; setting 0 alongside partial_data
        # False put a spec-divergent field on every webhook body.
        delivery_response.unavailable_count = None

        # A failed delivery does not consume its sequence number. Reuse the
        # prior attempt's key when the scheduler retries the same logical
        # notification in a later invocation.
        idempotency_key = delivery_repo.get_idempotency_key_for_sequence(
            media_buy.media_buy_id,
            task_type=DELIVERY_TASK_TYPE,
            notification_type=derived,
            sequence_number=sequence_number,
        )

        # Extract webhook URL and authentication
        webhook_url = reporting_webhook.get("url")
        if not webhook_url:
            logger.warning("No webhook URL configured for media buy %s", media_buy.media_buy_id)
            return False

        # Reporting webhooks are a protocol channel distinct from task-status
        # push_notification_config subscriptions. Build the sender carrier from
        # this media buy's reporting_webhook authentication; a stored subscription
        # for the same URL must not override these credentials.
        auth_config = reporting_webhook.get("authentication", {})
        auth_type = None
        auth_token = None

        if auth_config:
            schemes = auth_config.get("schemes", [])
            auth_type = schemes[0] if schemes else None
            auth_token = auth_config.get("credentials")

        push_config_repo = PushNotificationConfigRepository(session, media_buy.tenant_id)
        push_notification_config = push_config_repo.build_detached(
            media_buy.principal_id,
            webhook_url,
            config_id=f"temp_{media_buy.media_buy_id}",
            authentication_type=auth_type,
            authentication_token=auth_token,
        )

        # One task label spans the AdCP wire envelope and internal delivery-log
        # metadata. AdCP 3.1.1 defines ``media_buy_delivery`` specifically for
        # persistent reporting-webhook events.
        metadata = {
            "task_type": DELIVERY_TASK_TYPE,
            "tenant_id": media_buy.tenant_id,
            "principal_id": media_buy.principal_id,
            "media_buy_id": media_buy.media_buy_id,
        }

        # SDK 6.6.0 accepts the AdCP 3.1.1 media_buy_delivery task type.
        # Serialize via webhook_payload(): the schema scopes aggregated_totals to
        # "API responses (get_media_buy_delivery), not webhook notifications", and
        # this is the exclusion seam (it also drops None fields, preserving the
        # final-omits-next_expected_at contract).
        media_buy_delivery_payload = create_mcp_webhook_payload(
            task_id=media_buy.media_buy_id,
            task_type=DELIVERY_TASK_TYPE,
            result=delivery_response.webhook_payload(requested_metrics=reporting_webhook.get("requested_metrics")),
            status=AdcpTaskStatus.completed,
            token=reporting_webhook.get("token"),
            idempotency_key=idempotency_key,
        )

        # Atomic concurrency claim, taken NOW — immediately before the POST — so the
        # definitive no-send paths above never hold a claim. The loser skips; the
        # winner's claim is released below on a failed send (token-guarded) so an
        # immediate retry isn't blocked for the lease. The crash-after-POST
        # residual is tracked in #1606.
        claim_token = self._claim_final_webhook(session, media_buy) if is_final else None
        if is_final and claim_token is None:
            logger.info(
                "Final delivery webhook for media buy %s is claimed by another worker – skipping",
                media_buy.media_buy_id,
            )
            return False

        # Send webhook notification. ``session`` stays open here — it's reused below
        # on a failed send to release the claim — only the claim's transaction was
        # committed above (for cross-connection visibility, see _claim_final_webhook).
        try:
            delivered = await self.webhook_service.send_notification(
                push_notification_config=push_notification_config, payload=media_buy_delivery_payload, metadata=metadata
            )
            if not delivered:
                # send_notification returns False (never raises) on permanent
                # 4xx / exhausted retries and has already written the failed
                # WebhookDeliveryLog row. Raise so the batch counts an error
                # instead of logging "Sent" for a webhook the buyer never got.
                raise RuntimeError(
                    f"Delivery report webhook send failed for media buy {media_buy.media_buy_id} "
                    "(see webhook service logs for the HTTP failure detail)"
                )
        except Exception:
            # Definitive failure: release our final claim (token-guarded) so an
            # immediate retry isn't blocked for the lease. Lease recovery still
            # covers an actual crash (where this release never runs).
            if claim_token is not None:
                self._release_final_webhook_claim(session, media_buy, claim_token)
            raise

        logger.info("Sent delivery report webhook for media buy %s", media_buy.media_buy_id)
        return True


# Global scheduler instance
_scheduler: DeliveryWebhookScheduler | None = None


def get_delivery_webhook_scheduler() -> DeliveryWebhookScheduler:
    """Get or create global scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = DeliveryWebhookScheduler()
    return _scheduler


async def start_delivery_webhook_scheduler():
    """Start the delivery webhook scheduler (called at application startup)."""
    scheduler = get_delivery_webhook_scheduler()
    await scheduler.start()


async def stop_delivery_webhook_scheduler():
    """Stop the delivery webhook scheduler (called at application shutdown)."""
    scheduler = get_delivery_webhook_scheduler()
    await scheduler.stop()
