"""Exhaustive presentation guard for finalization outcomes."""

from typing import cast

import pytest

from src.services.media_buy_completion import FinalizeOutcome, classify_finalize_outcome


@pytest.mark.parametrize("outcome", list(FinalizeOutcome))
def test_classifier_accepts_each_defined_finalize_outcome(outcome: FinalizeOutcome) -> None:
    """Every defined outcome has an explicit branch before routes render it."""
    assert classify_finalize_outcome(outcome) is outcome


def test_classifier_rejects_an_unknown_finalize_outcome() -> None:
    """An enum extension cannot silently render as an admin success response."""
    with pytest.raises(ValueError, match="Unhandled FinalizeOutcome"):
        classify_finalize_outcome(cast(FinalizeOutcome, object()))
