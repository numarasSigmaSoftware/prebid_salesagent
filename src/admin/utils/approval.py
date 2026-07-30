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
    ``ApprovalExecutionStatus.WAITING_FOR_CREATIVES`` returned by
    ``prepare_media_buy_approval_execution``, so the wording gets one home rather than one
    per route. The text is adapter-agnostic on purpose: this fires for whichever ad server
    the tenant is configured with, not only GAM.

    ``blocking_count == 0`` is a DIFFERENT operator situation, not a degenerate case of the
    same one. ``_approval_creative_gate`` returns ``(False, ())`` when the buy has no
    creative assignments at all — the gate is unsatisfied precisely BECAUSE nothing is
    assigned — so a single count-interpolated sentence renders as "Waiting for 0
    creative(s) to be approved", which tells the operator to wait for an empty set. The
    two cases need opposite actions: assign creatives, versus wait for the assigned ones
    to clear review.
    """
    if blocking_count == 0:
        return (
            "Media buy approved! No creatives are assigned yet — assign and approve at least one "
            "before it can be created in the ad server."
        )
    return (
        f"Media buy approved! Waiting for {blocking_count} creative(s) to be approved before creating in the ad server."
    )
