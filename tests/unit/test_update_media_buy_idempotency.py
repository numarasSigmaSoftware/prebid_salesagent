"""Durable idempotency orchestration for the update_media_buy mutation."""

from unittest.mock import ANY, MagicMock, patch

import pytest

from src.core.resolved_identity import ResolvedIdentity
from src.core.schemas import Principal, UpdateMediaBuyResult, UpdateMediaBuySuccess
from src.core.tools.media_buy_update import _update_media_buy_impl
from src.services.idempotency_replay import ReservationResult
from tests.factories import PrincipalFactory


def _identity() -> ResolvedIdentity:
    return PrincipalFactory.make_identity(
        principal_id="principal-1",
        tenant_id="tenant-1",
        tenant={"tenant_id": "tenant-1"},
    ).model_copy(update={"account_id": "account-1"})


def _request() -> MagicMock:
    req = MagicMock()
    req.context = None
    req.idempotency_key = "update-idem-key-0001"
    return req


def _principal() -> Principal:
    return Principal(principal_id="principal-1", name="Test Principal", platform_mappings={})


def _result(*, media_buy_id: str = "mb-1", replayed: bool = False) -> UpdateMediaBuyResult:
    return UpdateMediaBuyResult(
        response=UpdateMediaBuySuccess(media_buy_id=media_buy_id, affected_packages=[]),
        status="completed",
        replayed=replayed,
    )


def test_wire_retry_replays_without_executing_update() -> None:
    replay = _result(replayed=True)
    identity = _identity()
    request = _request()
    principal = _principal()
    with (
        patch("src.core.tools.media_buy_update.resolve_principal_or_raise", return_value=principal),
        patch(
            "src.core.tools.media_buy_update.reserve_idempotent",
            return_value=ReservationResult(replay=replay),
        ) as reserve,
        patch("src.core.tools.media_buy_update._update_media_buy_work") as work,
    ):
        result = _update_media_buy_impl(
            req=request,
            identity=identity,
            raw_wire_payload={"idempotency_key": "update-idem-key-0001", "media_buy_id": "mb-1", "paused": True},
        )

    assert result is replay
    reserve.assert_called_once_with(
        ANY,
        "tenant-1",
        principal_id="principal-1",
        account_id="account-1",
        tool_name="update_media_buy",
        idempotency_key="update-idem-key-0001",
        request_hash=ANY,
        lease=ANY,
        decode=ANY,
        enforce_ceiling=True,
    )
    work.assert_not_called()


def test_fresh_wire_update_completes_the_owned_reservation() -> None:
    fresh = _result()
    identity = _identity()
    request = _request()
    principal = _principal()
    uow = MagicMock()
    uow.__enter__.return_value = uow
    with (
        patch("src.core.tools.media_buy_update.resolve_principal_or_raise", return_value=principal),
        patch(
            "src.core.tools.media_buy_update.reserve_idempotent",
            return_value=ReservationResult(attempt_id="attempt-1"),
        ),
        patch("src.core.tools.media_buy_update._update_media_buy_work", return_value=fresh) as work,
        patch("src.core.tools.media_buy_update.MediaBuyUoW", return_value=uow),
        patch("src.core.tools.media_buy_update.complete_idempotent") as complete,
    ):
        result = _update_media_buy_impl(
            req=request,
            identity=identity,
            raw_wire_payload={"idempotency_key": "update-idem-key-0001", "media_buy_id": "mb-1", "paused": True},
        )

    assert result is fresh
    work.assert_called_once_with(req=request, identity=identity, principal=principal, context_id=None)
    complete.assert_called_once_with(
        uow,
        attempt_id="attempt-1",
        response_model=fresh.response,
        protocol_status="completed",
    )


def test_failed_wire_update_stays_in_flight_and_never_completes() -> None:
    failure = RuntimeError("adapter failed")
    with (
        patch("src.core.tools.media_buy_update.resolve_principal_or_raise", return_value=_principal()),
        patch(
            "src.core.tools.media_buy_update.reserve_idempotent",
            return_value=ReservationResult(attempt_id="attempt-1"),
        ),
        patch("src.core.tools.media_buy_update._update_media_buy_work", side_effect=failure),
        patch("src.core.tools.media_buy_update.complete_idempotent") as complete,
        pytest.raises(RuntimeError, match="adapter failed"),
    ):
        _update_media_buy_impl(
            req=_request(),
            identity=_identity(),
            raw_wire_payload={"idempotency_key": "update-idem-key-0001", "media_buy_id": "mb-1", "paused": True},
        )

    complete.assert_not_called()
