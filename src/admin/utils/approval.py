"""Shared buyer-safe messages for admin approval outcomes."""

APPROVED_MEDIA_BUY_EXECUTION_FAILURE_MESSAGE = (
    "Workflow approved, but media buy creation failed. Review server logs before retrying."
)


def waiting_for_creatives_message(blocking_count: int) -> str:
    """The one operator-facing message for an approved media buy still blocked on creatives.

    Both admin approve routes reach this from the SAME
    ``ApprovalExecutionStatus.WAITING_FOR_CREATIVES`` returned by
    ``prepare_media_buy_approval_execution``, so the wording gets one home rather than one
    per route. The text is adapter-agnostic on purpose: this fires for whichever ad server
    the tenant is configured with, not only GAM.
    """
    return (
        f"Media buy approved! Waiting for {blocking_count} creative(s) to be approved before creating in the ad server."
    )
