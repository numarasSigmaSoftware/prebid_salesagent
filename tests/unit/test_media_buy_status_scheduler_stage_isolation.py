"""Stage isolation for MediaBuyStatusScheduler._update_statuses.

``_update_statuses`` runs its DB status-transition pass plus three independent
drain stages (creative-unblock recovery, workflow-notification publication,
native-task-notification publication) through a shared ``_run_drain_stage``
helper. Each stage's own try/except means a raise in one stage must not skip
the stages after it. Prior to this test, that property was claimed (by the
helper's docstring and its extraction from three hand-rolled try/except
blocks) but never actually graded — a regression that made stage N cancel
stage N+1 would have gone undetected.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.services.creative_unblock_recovery import CreativeUnblockRecoveryResult
from src.services.media_buy_status_scheduler import MediaBuyStatusScheduler


@pytest.fixture
def _no_db_status_transitions():
    """Short-circuit the DB status-transition pass so only the three drain
    stages under test are exercised.
    """
    with (
        patch("src.services.media_buy_status_scheduler.get_db_session") as mock_get_db_session,
        patch("src.services.media_buy_status_scheduler.MediaBuyRepository") as mock_repo,
    ):
        mock_session = MagicMock()
        mock_get_db_session.return_value.__enter__.return_value = mock_session
        mock_repo.get_all_by_statuses.return_value = []
        yield


@pytest.mark.asyncio
async def test_first_stage_raising_does_not_skip_the_later_stages(_no_db_status_transitions):
    """A raise recovering stale creative-unblock workflows must not prevent
    the workflow- and task-notification publication stages from running.
    """
    scheduler = MediaBuyStatusScheduler()

    with (
        patch(
            "src.services.creative_unblock_recovery.recover_stale_creative_unblock_workflows",
            side_effect=RuntimeError("boom"),
        ),
        patch(
            "src.core.context_manager.publish_pending_workflow_notifications",
            return_value=0,
        ) as mock_publish_workflow,
        patch(
            "src.services.a2a_task_lifecycle.publish_pending_task_notifications",
            return_value=0,
        ) as mock_publish_task,
    ):
        await scheduler._update_statuses()

    mock_publish_workflow.assert_called_once_with()
    mock_publish_task.assert_called_once_with()


@pytest.mark.asyncio
async def test_middle_stage_raising_does_not_skip_the_final_stage(_no_db_status_transitions):
    """A raise publishing workflow notifications must not prevent the final
    native-task-notification publication stage from running.
    """
    scheduler = MediaBuyStatusScheduler()

    with (
        patch(
            "src.services.creative_unblock_recovery.recover_stale_creative_unblock_workflows",
            return_value=CreativeUnblockRecoveryResult(recovered=0, deferred=0),
        ) as mock_recover,
        patch(
            "src.core.context_manager.publish_pending_workflow_notifications",
            side_effect=RuntimeError("boom"),
        ),
        patch(
            "src.services.a2a_task_lifecycle.publish_pending_task_notifications",
            return_value=0,
        ) as mock_publish_task,
    ):
        await scheduler._update_statuses()

    mock_recover.assert_called_once_with()
    mock_publish_task.assert_called_once_with()
