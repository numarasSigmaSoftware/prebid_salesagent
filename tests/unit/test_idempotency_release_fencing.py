"""A stale request must never clean up its lease successor's planned claims."""

from unittest.mock import MagicMock

from src.services.idempotency_replay import release_idempotent


def test_stale_owner_does_not_run_claim_cleanup_after_lease_takeover() -> None:
    uow = MagicMock()
    uow.__enter__.return_value = uow
    uow.idempotency_attempts.release.return_value = False
    cleanup = MagicMock()

    release_idempotent(
        MagicMock(return_value=uow),
        "tenant-1",
        attempt_id="stale-attempt",
        on_release=cleanup,
    )

    cleanup.assert_not_called()


def test_owned_release_and_claim_cleanup_share_the_same_uow() -> None:
    uow = MagicMock()
    uow.__enter__.return_value = uow
    uow.idempotency_attempts.release.return_value = True
    cleanup = MagicMock()

    release_idempotent(
        MagicMock(return_value=uow),
        "tenant-1",
        attempt_id="owned-attempt",
        on_release=cleanup,
    )

    cleanup.assert_called_once_with(uow=uow)
