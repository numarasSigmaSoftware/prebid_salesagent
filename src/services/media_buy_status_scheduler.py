"""Media Buy Status Scheduler - Automatically transitions media buy statuses.

This scheduler runs in the background and updates media buy statuses based on
their flight dates:
- pending_activation -> active (when start_time has passed and creatives approved)
- scheduled -> active (when start_time has passed)
- active -> completed (when end_time has passed)

This ensures media buys don't get stuck in transitional states when approved
before their start date.
"""

import asyncio
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypeVar

from sqlalchemy import select

from src.core.database.database_session import get_db_session
from src.core.database.models import Creative, CreativeAssignment, MediaBuy
from src.core.database.repositories import MediaBuyRepository
from src.core.utils import utc_flight_end, utc_flight_start

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Configurable via env var - default 60 seconds
STATUS_CHECK_INTERVAL_SECONDS = int(os.getenv("MEDIA_BUY_STATUS_CHECK_INTERVAL") or "60")


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

    async def _update_statuses(self) -> None:
        """Check and update media buy statuses based on flight dates."""
        now = datetime.now(UTC)
        updated_count = 0

        try:
            with get_db_session() as session:
                # Find media buys that need status updates (cross-tenant scheduler query)
                # 1. pending_start (or legacy pending_activation/scheduled) -> active if start_time passed
                # 2. active -> should become completed if end_time passed
                media_buys = MediaBuyRepository.get_all_by_statuses(
                    session, ["pending_start", "pending_activation", "scheduled", "active"]
                )

                for media_buy in media_buys:
                    new_status = self._compute_new_status(media_buy, now, session)

                    if new_status and new_status != media_buy.status:
                        old_status = media_buy.status
                        media_buy.status = new_status
                        updated_count += 1
                        logger.info(f"Updated media buy {media_buy.media_buy_id} status: {old_status} -> {new_status}")

                if updated_count > 0:
                    session.commit()
                    logger.info(f"Updated {updated_count} media buy status(es)")

        except Exception as e:
            logger.error(f"Failed to update media buy statuses: {e}", exc_info=True)

        from src.services.creative_unblock_recovery import recover_stale_creative_unblock_workflows

        await self._run_drain_stage(
            "recover stale creative-unblock workflows",
            recover_stale_creative_unblock_workflows,
            self._report_creative_unblock_recovery,
        )

        from src.core.context_manager import publish_pending_workflow_notifications

        await self._run_drain_stage(
            "publish pending workflow notifications",
            publish_pending_workflow_notifications,
            self._report_published_workflow_notifications,
        )

        from src.services.a2a_task_lifecycle import publish_pending_task_notifications

        await self._run_drain_stage(
            "publish pending native task notifications",
            publish_pending_task_notifications,
            self._report_published_task_notifications,
        )

    async def _run_drain_stage(
        self,
        label: str,
        drain: Callable[[], T],
        report: Callable[[T], None] | None = None,
    ) -> None:
        """Run one drain stage in a worker thread, isolating its failure from siblings.

        A raise from *drain* is logged and swallowed here so that the caller's
        remaining stages still run — mirrors the try/except-per-stage shape each
        of the three call sites in ``_update_statuses`` used to hand-roll.
        """
        try:
            result = await asyncio.to_thread(drain)
        except Exception as e:
            logger.error(f"Failed to {label}: {e}", exc_info=True)
            return
        if report is not None:
            report(result)

    def _report_creative_unblock_recovery(self, unblock_result) -> None:
        if unblock_result.recovered:
            logger.info("Recovered %d stale creative-unblock workflow(s)", unblock_result.recovered)
        if unblock_result.deferred:
            logger.warning(
                "Deferred %d stale creative-unblock workflow(s) pending an ambiguous provider outcome",
                unblock_result.deferred,
            )

    def _report_published_workflow_notifications(self, published_count: int) -> None:
        if published_count:
            logger.info("Published %d pending workflow notification(s)", published_count)

    def _report_published_task_notifications(self, published_task_count: int) -> None:
        if published_task_count:
            logger.info("Published %d pending native task notification(s)", published_task_count)

    def _compute_new_status(self, media_buy: MediaBuy, now: datetime, session) -> str | None:
        """Compute the new status for a media buy based on flight dates.

        Returns:
            New status string if change needed, None otherwise.
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
            return None  # No start time defined

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
            return None  # No end time defined

        current_status = media_buy.status

        # Check if campaign has ended
        if now > end_time:
            if current_status != "completed":
                return "completed"
            return None

        # Check if campaign should be active
        if now >= start_time:
            if current_status in ["pending_start", "pending_activation", "scheduled"]:
                # Before activating, verify creatives are approved (for pending_start/pending_activation)
                if current_status in ["pending_start", "pending_activation"]:
                    if self._are_creatives_approved(media_buy, session):
                        return "active"
                    # Creatives not approved yet - stay pending
                    return None
                else:
                    # scheduled -> active (no creative check needed, already validated)
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
