"""
Delivery Webhook Scheduler

Sends daily delivery reports via webhooks for media buys that have configured reporting_webhook.
This runs as a background task and sends reports when GAM data is fresh (after 4 AM PT daily).
"""

import asyncio
import logging
import os
from dataclasses import dataclass
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


@dataclass(frozen=True)
class DeliveryBatchSummary:
    """Per-run tally of what a delivery-webhook batch did with each selected buy.

    The buckets PARTITION the selection: every buy the batch pulled increments
    exactly one, so ``accounted_for`` equals ``selected``. That is what lets an
    operator read "everything was skipped" apart from "there was nothing to do"
    — a distinction the summary loses the moment some path exits the loop
    without counting, since the remaining numbers still look plausible.

    Returned rather than only logged so the invariant can be asserted against a
    real batch instead of scraped from a log line.
    """

    selected: int = 0
    sent: int = 0
    suppressed: int = 0
    not_reportable: int = 0
    no_webhook_config: int = 0
    errors: int = 0

    @property
    def accounted_for(self) -> int:
        return self.sent + self.suppressed + self.not_reportable + self.no_webhook_config + self.errors


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

    async def _send_reports(self) -> DeliveryBatchSummary:
        """Send reports for all active media buys with configured webhooks.

        Returns the run's :class:`DeliveryBatchSummary`. The scheduler loop
        ignores it; it exists so the partition invariant (every selected buy
        lands in exactly one bucket) is assertable against a real batch rather
        than read back out of a log line.
        """
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
                suppressed = 0
                not_reportable = 0
                no_webhook_config = 0

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
                            # Counted, not a bare `continue`: this is a skip like
                            # any other, and the summary below claims to account
                            # for every buy the batch touched. Left uncounted, a
                            # run whose whole population has a present-but-empty
                            # reporting_webhook logs all-zeroes — the exact
                            # "quiet, healthy scheduler" misreading the summary
                            # was widened to prevent.
                            no_webhook_config += 1
                            continue

                        # The status-only selection also matches pre-flight and
                        # paused rows the impl cannot report on. Resolve the
                        # same canonical status the impl would and skip them
                        # here, instead of invoking the full delivery impl
                        # every hour only to misread its MEDIA_BUY_NOT_FOUND
                        # advisory as a warning-worthy failure.
                        canonical = resolve_canonical_status(media_buy, datetime.now(UTC).date())
                        if canonical not in WEBHOOK_REPORTABLE_CANONICAL_STATUSES:
                            not_reportable += 1
                            continue

                        # Send delivery report; only count it when a webhook
                        # actually went out (dedup/frequency skips return False).
                        if await self._send_report_for_media_buy(media_buy, reporting_webhook, session):
                            reports_sent += 1
                        else:
                            suppressed += 1

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

                        # This loop shares ONE session across every buy in the batch, so
                        # any failed statement on it poisons the rest of the run: a
                        # failed flush/commit leaves a SQLAlchemy session unusable until
                        # rolled back — Postgres itself refuses every further statement
                        # on an aborted transaction. Without this, one buy's DB error
                        # would silently fail every remaining buy in the batch too (each
                        # logged as its own unrelated-looking error), not just the one
                        # that actually failed. Safe to call unconditionally: rolling
                        # back a session with nothing pending is a no-op.
                        try:
                            session.rollback()
                        except Exception:
                            logger.debug(
                                "session.rollback() itself failed after media buy %s; "
                                "batch continues, but the session may be unusable for "
                                "the rest of this run",
                                media_buy.media_buy_id,
                                exc_info=True,
                            )

                # Suppressions are reported, not just sends and errors. Every skip on
                # this path returns False — dedup, unsupported cadence, nothing to
                # report — so a summary of only sent+errors renders a batch
                # that suppressed every buy identically to one with no work to do:
                # "0 sent, 0 errors". That is the reading under which a population
                # whose webhooks were skipped on every pass looks like a quiet, healthy
                # scheduler. _delivery_lookup_is_usable calls out the same hazard for
                # one branch; this closes it for the rest.
                #
                # The five counters PARTITION the selected buys — every iteration
                # increments exactly one — so the total is auditable against the
                # query and a future skip added without a counter shows up as a
                # shortfall rather than as silence. Returned as well as logged so
                # that is assertable; see the batch-summary partition test.
                summary = DeliveryBatchSummary(
                    selected=len(media_buys),
                    sent=reports_sent,
                    suppressed=suppressed,
                    not_reportable=not_reportable,
                    no_webhook_config=no_webhook_config,
                    errors=errors,
                )
                logger.info(
                    "Daily delivery report batch complete: %d sent, %d suppressed, "
                    "%d not reportable, %d without webhook config, %d errors (of %d selected)",
                    summary.sent,
                    summary.suppressed,
                    summary.not_reportable,
                    summary.no_webhook_config,
                    summary.errors,
                    summary.selected,
                )
                return summary

        except Exception as e:
            logger.error(f"Error in daily delivery report batch: {e}", exc_info=True)

        # The batch aborted before the per-buy loop (or while opening the session):
        # nothing was selected and nothing was attempted, which is what an
        # all-zero summary means. The error itself is logged above.
        return DeliveryBatchSummary()

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
                # the read-check path (best-effort; true exactly-once needs a durable
                # reserve-before-send).
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

        NOT a hard exactly-once guarantee. This is a pure read decision, so two
        workers can both pass it and both POST; closing that needs the send reserved
        durably before the POST, tracked separately.
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
                concurrency window remains, closable only by reserving the send
                durably before the POST).

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
            # the buy. _should_skip_send is already final-aware for the same reason.
            if not force and not is_final and raw_freq not in SUPPORTED_REPORTING_FREQUENCIES:
                logger.warning(
                    "Skipping reporting webhook with frequency '%s' for media buy %s – "
                    "supported delivery-webhook frequencies are: %s",
                    raw_freq,
                    media_buy.media_buy_id,
                    ", ".join(sorted(SUPPORTED_REPORTING_FREQUENCIES)),
                )
                return False

            # Best-effort read-only de-dup.
            if self._should_skip_send(delivery_repo, media_buy, is_final=is_final, force=force):
                return False

            return await self._deliver_report(session, delivery_repo, media_buy, reporting_webhook)

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
    ) -> bool:
        """Build the delivery report and POST it.

        Returns True when a webhook was delivered, False on a definitive no-send
        (no delivery data, no URL); RAISES on a failed send so the batch counts an
        error.

        No concurrency guard runs here: the dedup checks in ``_should_skip_send``
        are read-only, so two workers running their own scheduler against the same
        database (e.g. two replicas) can both pass them and both POST. Making the
        send exactly-once needs the send reserved durably BEFORE the POST (a
        transactional outbox), which is tracked separately.
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

        # Set webhook-specific metadata directly on the response model.
        # These fields are webhook-only ("only present in webhook deliveries" —
        # get-media-buy-delivery-response.json @ v3.1-04f59d2d5), so the polling
        # impl never sets them. On the polling-response path this is the only
        # place they are attached to the wire — NOT a repo-wide sole emitter:
        # webhook_delivery_service.send_delivery_webhook (GAM reporting, delivery
        # simulator) attaches its own notification_type / sequence_number /
        # next_expected_at from an in-memory counter. That emitter IS graded by the
        # shared next_expected_at oracle, but not by the omission or pairing ones —
        # the two emitters are not yet reconciled.
        #
        # The body emitted here is governed by media-buy-delivery-webhook-result.json
        # (@ 3.1.0, the release the schema entered). It lists notification_type in
        # `required` — the constraint the zero-deliveries note below reasons about.
        #
        # That constraint IS graded offline now, contrary to an earlier revision of
        # this comment which said the file "is not among the vendored fixtures ...
        # so it is not graded offline". That was true of the vendored tree; the
        # repointed tests/helpers/pinned_schema.py at the SDK's own adcp/_schemas/,
        # where the file resolves. test_scheduler_webhook_body_is_schema_valid
        # validates a real scheduler body against it, so "required" and the
        # webhook-only omissions are checked rather than only described here.
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

        # Sequence number for this webhook: max SUCCESSFULLY DELIVERED sequence + 1
        # (spec: "Sequential notification number ... starts at 1"). Failed/retrying
        # sends also log the sequence they attempted; counting them — while the dedup
        # above counts only successes — would burn numbers the buyer never received,
        # so a buyer's first-ever webhook could start above 1. A query failure
        # propagates and aborts this send loudly: a quiet fallback to 1 would put an
        # already-consumed sequence on the wire.
        sequence_number = (
            delivery_repo.get_max_sequence_number(media_buy.media_buy_id, task_type=DELIVERY_TASK_TYPE) + 1
        )
        delivery_response.sequence_number = sequence_number

        # A failed delivery does not consume its sequence number. Reuse the
        # prior attempt's key when the scheduler retries the same logical
        # notification in a later invocation.
        idempotency_key = delivery_repo.get_idempotency_key_for_sequence(
            media_buy.media_buy_id,
            task_type=DELIVERY_TASK_TYPE,
            notification_type=derived,
            sequence_number=sequence_number,
        )

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

        delivered = await self.webhook_service.send_notification(
            push_notification_config=push_notification_config, payload=media_buy_delivery_payload, metadata=metadata
        )
        if not delivered:
            # send_notification returns False (never raises) for FOUR distinct
            # outcomes, and collapses them into one bool this caller cannot
            # tell apart:
            #   1. no URL configured        -> no HTTP attempt, NO log row
            #   2. rejected by the SSRF gate -> no HTTP attempt, NO log row
            #   3. permanent 4xx             -> HTTP attempted, log row written
            #   4. retries exhausted         -> HTTP attempted, log row written
            #
            # An earlier revision of this comment asserted 3/4 unconditionally
            # ("has already written the failed WebhookDeliveryLog row") and the
            # message sent the reader to "the HTTP failure detail". On 1 and 2
            # there is no log row and no HTTP failure to find, so an operator
            # hunting a delivery that never lands was pointed at evidence that
            # does not exist.
            #
            # Say only what is known here. Distinguishing them properly means
            # giving send_notification a typed outcome instead of a bool —
            # a change across every caller (A2A, MCP, task webhooks), tracked
            # separately rather than widened into this change.
            raise RuntimeError(
                f"Delivery report webhook send failed for media buy {media_buy.media_buy_id} "
                "(webhook service reported failure; if no HTTP attempt is logged, the URL was "
                "missing or rejected by the SSRF gate — those paths write no WebhookDeliveryLog row)"
            )

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
