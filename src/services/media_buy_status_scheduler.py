"""Media Buy Status Scheduler - Automatically transitions media buy statuses.

This scheduler runs in the background and updates media buy statuses based on
their flight dates:
- pending_start / pending_activation -> active (when start_time has passed AND
  creatives are approved — creative-gated states)
- scheduled / ready / approved (legacy serving aliases) -> active (when
  start_time has passed — purely date-gated, already approved)
- any serving status -> completed (when end_time has passed)

This ensures media buys don't get stuck in transitional states when approved
before their start date, and migrates legacy persisted aliases to the modern
vocabulary so they match what the read tools report.
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from src.core.database.database_session import get_db_session
from src.core.database.models import Creative, CreativeAssignment, MediaBuy, PersistedMediaBuyStatus
from src.core.database.repositories import MediaBuyRepository
from src.core.tools._media_buy_status import (
    LEGACY_SERVING_ALIASES,
    PENDING_PERSISTED_STATUSES,
)
from src.core.tools._media_buy_transitions import resolve_flight_window_status

logger = logging.getLogger(__name__)

# The sweep runs every minute; tests may override the interval through the
# environment without changing the shipped default. Named rather than inlined as
# a string literal, mirroring DEFAULT_SLEEP_INTERVAL_SECONDS in the sibling
# delivery scheduler — the two schedulers spell the same idea, so they spell it
# the same way.
DEFAULT_STATUS_CHECK_INTERVAL_SECONDS = 60
# Configurable via env var for testing
STATUS_CHECK_INTERVAL_SECONDS = int(
    os.getenv("MEDIA_BUY_STATUS_CHECK_INTERVAL") or str(DEFAULT_STATUS_CHECK_INTERVAL_SECONDS)
)


@dataclass
class StatusSweepSummary:
    """Per-run tally of what a status sweep did with each selected buy.

    The sibling of ``DeliveryBatchSummary`` in ``delivery_webhook_scheduler``, and
    for the same reason: the buckets PARTITION the selection, so every selected buy
    increments exactly one and ``accounted_for`` equals ``selected``.

    This scheduler previously counted only transitions and logged nothing at all
    when there were none — the exact "quiet, healthy scheduler" misreading the
    delivery summary was widened to prevent, left unswept one file over. A sweep
    that selected a thousand rows and changed none emitted no line, which is
    indistinguishable from a sweep that selected nothing.

    Returned rather than only logged so the partition invariant can be asserted
    against a real sweep instead of scraped from a log line.
    """

    selected: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: int = 0

    @property
    def accounted_for(self) -> int:
        return self.updated + self.unchanged + self.errors


# PENDING_PERSISTED_STATUSES / LEGACY_SERVING_ALIASES (imported): the pre-serving
# states this scheduler promotes, and the legacy serving aliases it migrates to the
# modern "active" once serving. Both derive from the canonical map and live beside it
# in src/core/tools/_media_buy_status.py so this scheduler can't drift a partial copy.


# Derived from the status vocabulary rather than re-listed here, so a spelling
# added to the map is activatable without a second edit nobody remembers.
#
# This is deliberately WIDER than a literal {pending_start, pending_activation,
# scheduled}: it also carries the legacy serving aliases ("approved", "ready").
# Those rows were the stranded case this PR exists to fix — reported active by
# get_media_buy_delivery, yet never migrated by this sweep and so never sent a
# delivery webhook. They are pre-serving spellings, not seller decisions, so
# including them respects the rule the branch below states: an unattended sweep
# must never resurrect a buy a seller paused, rejected or canceled.
_ACTIVATABLE_STATUSES = frozenset(PENDING_PERSISTED_STATUSES | LEGACY_SERVING_ALIASES)


class MediaBuyStatusScheduler:
    """Scheduler for updating media buy statuses based on flight dates."""

    def __init__(self) -> None:
        self.is_running = False
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the scheduler background task."""
        async with self._lock:
            if self.is_running:
                logger.warning("Media buy status scheduler is already running")
                return

            self.is_running = True
            self._task = asyncio.create_task(self._run_scheduler())
            logger.info("Media buy status scheduler started (checking every %ss)", STATUS_CHECK_INTERVAL_SECONDS)

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
            logger.info("Media buy status scheduler stopped")

    async def _run_scheduler(self) -> None:
        """Main scheduler loop - runs on a fixed cadence."""
        while self.is_running:
            try:
                # Offloaded: _update_statuses is fully synchronous (queries, per-row
                # _compute_new_status, session.commit()) and this loop is started into
                # the MCP server's lifespan loop, so running it inline blocked every
                # other request for the length of a sweep. Safe to hand to a worker
                # thread because the function opens its OWN session inside itself and
                # get_db_session resolves through a thread-local scoped_session — no
                # Session crosses the boundary. Contrast _deliver_report in
                # delivery_webhook_scheduler.py, which cannot be offloaded for exactly
                # the opposite reason.
                await asyncio.to_thread(self._update_statuses)
            except Exception as e:
                logger.error("Error in media buy status scheduler: %s", e, exc_info=True)

            # Cancellation must reach here as a live exception, so this sleep is an
            # ordinary cancellable await in the loop body — NOT a `finally`, and there
            # is deliberately no `except asyncio.CancelledError` above.
            #
            # The old shape caught CancelledError to `break`, which CONSUMED it, and
            # the `finally` then started a FRESH sleep that the cancellation no longer
            # applied to — so `stop()` (which awaits this task) blocked for a whole
            # interval. That was latent while the try body never suspended; offloading
            # the sweep to a thread made `await` a real suspension point and the
            # cancellation started landing inside the try, which made it reachable.
            #
            # CancelledError is a BaseException, so the `except Exception` above cannot
            # swallow it: an ordinary sweep failure is still absorbed and the loop
            # survives it, while a cancel propagates and ends the task at once.
            # `stop()` already wraps `await self._task` in `except CancelledError`.
            await asyncio.sleep(STATUS_CHECK_INTERVAL_SECONDS)

    def _update_statuses(self) -> StatusSweepSummary:
        """Check and update media buy statuses based on flight dates.

        Synchronous by design: every statement in here blocks (repository query,
        per-row ``_compute_new_status``, ``session.commit()``) and there is nothing
        to await. Declaring it ``async`` only disguised that — it ran the whole
        sweep inline on the caller's event loop. ``_run_scheduler`` hands it to
        ``asyncio.to_thread`` instead; the session it opens below is created,
        used and closed entirely within whichever thread runs it.

        Returns the run's :class:`StatusSweepSummary`. Returned rather than only
        logged so the partition invariant (every selected buy lands in exactly one
        bucket) can be asserted against a real sweep.
        """
        now = datetime.now(UTC)
        summary = StatusSweepSummary()

        try:
            with get_db_session() as session:
                # Find media buys that need status updates (cross-tenant scheduler query)
                # 1. pending set -> active if start_time passed (and creatives approved)
                # 2. serving set (incl. legacy aliases) -> active mid-flight, completed
                #    once end_time passes. Derived from the canonical map so legacy
                #    "ready"/"approved" rows are migrated, not stranded.
                media_buys = MediaBuyRepository.get_all_by_statuses(
                    session, _ACTIVATABLE_STATUSES | {PersistedMediaBuyStatus.ACTIVE}
                )
                summary.selected = len(media_buys)

                for media_buy in media_buys:
                    # One unprocessable row must not strand every other buy's transition.
                    # This sweep now covers the legacy serving aliases too, so it reaches
                    # older rows whose flight fields were written under earlier
                    # conventions; letting a single bad row abort the loop would leave
                    # every buy after it stuck in a transitional state indefinitely.
                    #
                    # SQLAlchemyError is deliberately NOT caught: _compute_new_status
                    # queries (creative approval), and a failed statement leaves the
                    # transaction aborted, so continuing would only produce a cascade of
                    # failures on rows that were never actually bad. A session-level
                    # fault aborts the sweep; a row-level one skips the row.
                    try:
                        new_status = self._compute_new_status(media_buy, now, session)
                    except SQLAlchemyError:
                        raise
                    except Exception as e:
                        logger.error(
                            "Skipping status update for media buy %s: %s",
                            media_buy.media_buy_id,
                            e,
                            exc_info=True,
                        )
                        summary.errors += 1
                        continue

                    if new_status and new_status != media_buy.status:
                        old_status = media_buy.status
                        # The sweep is deliberately cross-tenant, but the repository is
                        # tenant-scoped, so build it from this row's own tenant. That
                        # keeps every write inside the isolation the class enforces
                        # rather than widening it with a cross-tenant write method.
                        updated = MediaBuyRepository(session, media_buy.tenant_id).update_status(
                            media_buy.media_buy_id,
                            new_status,
                            # The sweep does not itself commit anything -- commitment
                            # happened earlier, at the synchronous create or at approval,
                            # and confirmed_at is write-once so a stamped row is untouched.
                            # ACTIVE is passed as committing anyway, and the PIN is the
                            # reason: create-media-buy-response.json @ 3.1.1 constrains
                            # confirmed_at in exactly one direction -- a null value forbids
                            # status "active". This sweep is the last writer before a buyer
                            # can observe that combination, so it must not be able to
                            # produce it. Any row reaching ACTIVE unstamped is already a
                            # defect upstream; stamping here keeps the defect from becoming
                            # a schema-invalid document on the wire.
                            seller_committed=new_status == PersistedMediaBuyStatus.ACTIVE,
                        )
                        if updated is None:
                            # Unreachable: media_buy_id is the sole primary key and the
                            # row is already loaded in this transaction, so the
                            # tenant-filtered re-fetch cannot miss. Never fall through
                            # silently — a sweep must not report an update it did not make.
                            logger.error(
                                "Media buy %s vanished from its own tenant %r mid-sweep; status left at %s",
                                media_buy.media_buy_id,
                                media_buy.tenant_id,
                                old_status,
                            )
                            continue
                        summary.updated += 1
                        logger.info(
                            "Updated media buy %s status: %s -> %s", media_buy.media_buy_id, old_status, new_status
                        )
                    else:
                        summary.unchanged += 1

                if summary.updated > 0:
                    session.commit()

                # Logged UNCONDITIONALLY, against the selection. The previous line
                # only fired when something changed, so a sweep that skipped every
                # row it selected emitted nothing at all -- the same "quiet, healthy
                # scheduler" misreading the delivery batch summary was widened to
                # prevent, in the sibling this PR also touches.
                logger.info(
                    "Status sweep: %d updated, %d unchanged, %d errors (of %d selected)",
                    summary.updated,
                    summary.unchanged,
                    summary.errors,
                    summary.selected,
                )

        except Exception as e:
            logger.error("Failed to update media buy statuses: %s", e, exc_info=True)

    def _compute_new_status(self, media_buy: MediaBuy, now: datetime, session) -> PersistedMediaBuyStatus | None:
        """The status this sweep should write, or ``None`` to leave the row alone.

        The flight-window rule itself lives in
        ``src.core.tools._media_buy_transitions.resolve_flight_window_status`` — one
        domain owner shared with the two admin approval paths. What stays here is the
        part that is genuinely the SCHEDULER's: this runs unattended over every buy,
        so it moves only buys that are waiting to start, and never writes a status the
        row already has.
        """
        target = resolve_flight_window_status(
            media_buy,
            now=now,
            creatives_approved=self._are_creatives_approved(media_buy, session),
        )
        if target is None:
            return None  # No flight window — this sweep has no opinion.

        current = media_buy.status
        if target == current:
            return None

        if target == PersistedMediaBuyStatus.COMPLETED:
            return target

        # Activation only, and only out of a pre-serving state. An unattended sweep
        # must not resurrect a buy a seller deliberately paused, rejected or canceled,
        # and it must not push a buy BACK to scheduled once it is serving.
        if target == PersistedMediaBuyStatus.ACTIVE and current in _ACTIVATABLE_STATUSES:
            return target

        return None

    def _are_creatives_approved(self, media_buy: MediaBuy, session) -> bool:
        """Check if all creatives for a media buy are approved.

        Returns:
            True if no creatives assigned OR all creatives are approved.
        """
        # Get creative assignments for this media buy
        stmt = select(CreativeAssignment).filter_by(tenant_id=media_buy.tenant_id, media_buy_id=media_buy.media_buy_id)
        assignments = session.scalars(stmt).all()

        if not assignments:
            # No creatives assigned - can activate (some campaigns run without creatives initially)
            return True

        # Get all creative IDs
        creative_ids = list({a.creative_id for a in assignments})

        # Check creative statuses
        creative_stmt = select(Creative).where(
            Creative.tenant_id == media_buy.tenant_id,
            Creative.creative_id.in_(creative_ids),
        )
        creatives = session.scalars(creative_stmt).all()

        # All creatives must be approved
        for creative in creatives:
            if creative.status != "approved":
                return False

        return True


# Global singleton instance
_scheduler: MediaBuyStatusScheduler | None = None


def get_media_buy_status_scheduler() -> MediaBuyStatusScheduler:
    """Get or create the global media buy status scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = MediaBuyStatusScheduler()
    return _scheduler


async def start_media_buy_status_scheduler() -> None:
    """Start the global media buy status scheduler."""
    scheduler = get_media_buy_status_scheduler()
    await scheduler.start()


async def stop_media_buy_status_scheduler() -> None:
    """Stop the global media buy status scheduler."""
    scheduler = get_media_buy_status_scheduler()
    await scheduler.stop()
