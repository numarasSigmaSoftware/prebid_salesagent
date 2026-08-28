"""Shared buyer-safe messages for admin approval outcomes."""

APPROVED_MEDIA_BUY_EXECUTION_FAILURE_MESSAGE = (
    "Workflow approved, but media buy creation failed. Review server logs before retrying."
)

APPROVED_MEDIA_BUY_PENDING_RECONCILIATION_MESSAGE = (
    "Media buy was created externally, but activation could not be finalized. "
    "The workflow remains pending for safe reconciliation."
)


def waiting_for_creatives_message(blocking_count: int) -> str:
    """The one operator-facing message for an approved media buy still blocked on creatives.

    Both admin approve routes reach this from the SAME
    ``ApprovalExecutionStatus.WAITING_FOR_CREATIVES``, which is now the writer's
    ``ApprovalOutcome.HELD_PENDING_CREATIVES`` translated once in
    ``execute_and_finalize_media_buy_approval``. The wording therefore gets one home
    rather than one per route. The text is adapter-agnostic on purpose: this fires for
    whichever ad server the tenant is configured with, not only GAM.

    ``blocking_count`` is always at least 1. The gate is
    ``CreativeAssignmentRepository.unapproved_creative_ids``, which returns the ids that
    are NOT approved — a buy with no assignments returns an empty list and is therefore
    not held at all, so this function is never reached with nothing to wait for. The
    earlier zero-count branch existed because the superseded gate reported "no creatives
    assigned" as an unsatisfied gate; that state no longer reaches here.
    """
    return (
        f"Media buy approved! Waiting for {blocking_count} creative(s) to be approved before creating in the ad server."
    )
