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
from src.core.database.models import Creative, CreativeAssignment, MediaBuy
from src.core.database.repositories import MediaBuyRepository
from src.core.tools._media_buy_status import (
    LEGACY_SERVING_ALIASES,
    PENDING_PERSISTED_STATUSES,
    SERVING_PERSISTED_STATUSES,
)
from src.core.utils import utc_flight_end, utc_flight_start

logger = logging.getLogger(__name__)

# Configurable via env var - default 60 seconds
STATUS_CHECK_INTERVAL_SECONDS = int(os.getenv("MEDIA_BUY_STATUS_CHECK_INTERVAL") or "60")


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

    ``no_flight_window`` is currently UNREACHABLE and is expected to stay 0: see
    ``_NoFlightWindow``. It is a named bucket rather than a silent path so that if
    the schema ever loosens, the rows land somewhere countable instead of
    reopening the hole.

    Returned rather than only logged so the partition invariant can be asserted
    against a real sweep instead of scraped from a log line.
    """

    selected: int = 0
    updated: int = 0
    unchanged: int = 0
    no_flight_window: int = 0
    errors: int = 0

    @property
    def accounted_for(self) -> int:
        return self.updated + self.unchanged + self.no_flight_window + self.errors


class _NoFlightWindow:
    """Sentinel for "this row has no resolvable flight window".

    ``_compute_new_status`` returned a bare ``None`` for that AND for "no change
    needed", so the caller could not tell them apart. They are different facts:
    "no change" is the healthy steady state, while a row with no resolvable start
    or end can NEVER transition and would be re-selected on every sweep forever.

    UNREACHABLE TODAY, and deliberately kept anyway. ``MediaBuy.start_date`` and
    ``MediaBuy.end_date`` are both ``nullable=False`` (models.py:911-912), and
    ``_compute_new_status`` falls back to them whenever ``start_time``/``end_time``
    are unset, so neither branch can fire against the current schema — the two
    ``return None``s this replaces were not silently dropping rows, contrary to how
    a reading of the loop alone suggests. No test seeds this bucket, because no
    test CAN: the NOT NULL constraint rejects the row. Kept because the cost is a
    named bucket, and the alternative is that a future nullable-ing of either
    column reopens the hole silently.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "NO_FLIGHT_WINDOW"


NO_FLIGHT_WINDOW = _NoFlightWindow()

# PENDING_PERSISTED_STATUSES / LEGACY_SERVING_ALIASES (imported): the pre-serving
# states this scheduler promotes, and the legacy serving aliases it migrates to the
# modern "active" once serving. Both derive from the canonical map and live beside it
# in src/core/tools/_media_buy_status.py so this scheduler can't drift a partial copy.


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
            logger.info(f"Media buy status scheduler started (checking every {STATUS_CHECK_INTERVAL_SECONDS}s)")

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
                await self._update_statuses()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in media buy status scheduler: {e}", exc_info=True)
            finally:
                # Wait before next check
                await asyncio.sleep(STATUS_CHECK_INTERVAL_SECONDS)

    async def _update_statuses(self) -> StatusSweepSummary:
        """Check and update media buy statuses based on flight dates.

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
                    session, sorted(PENDING_PERSISTED_STATUSES | SERVING_PERSISTED_STATUSES)
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
                            f"Skipping status update for media buy {media_buy.media_buy_id}: {e}",
                            exc_info=True,
                        )
                        summary.errors += 1
                        continue

                    if new_status is NO_FLIGHT_WINDOW:
                        # Counted, not a bare `continue`: this row can never
                        # transition, so it is re-selected on every sweep forever.
                        # Silently skipping it made an unbounded population of stuck
                        # rows indistinguishable from an idle scheduler.
                        summary.no_flight_window += 1
                        continue

                    if new_status and new_status != media_buy.status:
                        old_status = media_buy.status
                        media_buy.status = str(new_status)
                        summary.updated += 1
                        logger.info(f"Updated media buy {media_buy.media_buy_id} status: {old_status} -> {new_status}")
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
                    "Status sweep: %d updated, %d unchanged, %d without a flight window, %d errors (of %d selected)",
                    summary.updated,
                    summary.unchanged,
                    summary.no_flight_window,
                    summary.errors,
                    summary.selected,
                )

        except Exception as e:
            logger.error(f"Failed to update media buy statuses: {e}", exc_info=True)

        return summary

    def _compute_new_status(self, media_buy: MediaBuy, now: datetime, session) -> "str | None | _NoFlightWindow":
        """Compute the new status for a media buy based on flight dates.

        Returns:
            New status string if a change is needed; ``None`` if the row is
            already correct; ``NO_FLIGHT_WINDOW`` if no start or end could be
            resolved, which is a STUCK row rather than a settled one and is
            counted separately by the caller.
        """
        # Get start and end times (prefer start_time/end_time over start_date/end_date)
        start_time: datetime | None = None
        if media_buy.start_time:
            raw_start: datetime = media_buy.start_time
            if raw_start.tzinfo is None:
                start_time = raw_start.replace(tzinfo=UTC)
            else:
                start_time = raw_start
        elif media_buy.start_date:
            start_time = utc_flight_start(media_buy.start_date)  # type: ignore[arg-type]

        if start_time is None:
            return NO_FLIGHT_WINDOW  # No start time defined -> cannot ever transition

        end_time: datetime | None = None
        if media_buy.end_time:
            raw_end: datetime = media_buy.end_time
            if raw_end.tzinfo is None:
                end_time = raw_end.replace(tzinfo=UTC)
            else:
                end_time = raw_end
        elif media_buy.end_date:
            end_time = utc_flight_end(media_buy.end_date)  # type: ignore[arg-type]

        if end_time is None:
            return NO_FLIGHT_WINDOW  # No end time defined -> cannot ever transition

        current_status = media_buy.status

        # Check if campaign has ended
        if now > end_time:
            if current_status != "completed":
                return "completed"
            return None

        # Check if campaign should be active
        if now >= start_time:
            if current_status in PENDING_PERSISTED_STATUSES:
                # Creative-gated: verify creatives are approved before activating
                if self._are_creatives_approved(media_buy, session):
                    return "active"
                # Creatives not approved yet - stay pending
                return None
            if current_status in LEGACY_SERVING_ALIASES:
                # scheduled/ready/approved -> active: purely date-gated legacy
                # serving aliases (already approved), no creative check needed
                return "active"

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
