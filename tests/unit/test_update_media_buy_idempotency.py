"""Durable idempotency orchestration for the update_media_buy mutation."""

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from src.core.database.repositories.idempotency_attempt import DEFAULT_IN_FLIGHT_LEASE
from src.core.idempotency_canonical import canonical_payload_hash
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
    req.media_buy_id = "mb-1"
    req.paused = True
    req.packages = []
    req.today = None
    return req


def _principal() -> Principal:
    return Principal(principal_id="principal-1", name="Test Principal", platform_mappings={})


def _result(*, media_buy_id: str = "mb-1", replayed: bool = False) -> UpdateMediaBuyResult:
    return UpdateMediaBuyResult(
        response=UpdateMediaBuySuccess(media_buy_id=media_buy_id, affected_packages=[]),
        status="completed",
        replayed=replayed,
    )


@dataclass
class _OrchestrationHarness:
    principal: Principal
    reserve: MagicMock
    work: MagicMock
    uow: MagicMock
    complete: MagicMock
    adapter: MagicMock
    uow_factory: MagicMock


@pytest.fixture
def orchestration(mocker) -> _OrchestrationHarness:
    principal = _principal()
    mocker.patch("src.core.tools.media_buy_update.resolve_principal_or_raise", return_value=principal)
    reserve = mocker.patch("src.core.tools.media_buy_update.reserve_idempotent")
    work = mocker.patch("src.core.tools.media_buy_update._update_media_buy_work")
    uow = MagicMock()
    uow.__enter__.return_value = uow
    uow_factory = mocker.patch("src.core.tools.media_buy_update.MediaBuyUoW", return_value=uow)
    complete = mocker.patch("src.core.tools.media_buy_update.complete_idempotent")
    adapter = MagicMock(supports_media_buy_update_reconciliation=True)
    mocker.patch("src.core.tools.media_buy_update.get_adapter", return_value=adapter)
    return _OrchestrationHarness(principal, reserve, work, uow, complete, adapter, uow_factory)


def test_wire_retry_replays_without_executing_update(orchestration: _OrchestrationHarness) -> None:
    replay = _result(replayed=True)
    identity = _identity()
    request = _request()
    request.context = {"correlation_id": "ctx-1"}
    orchestration.reserve.return_value = ReservationResult(replay=replay)
    result = _update_media_buy_impl(
        req=request,
        identity=identity,
        raw_wire_payload={"idempotency_key": "update-idem-key-0001", "media_buy_id": "mb-1", "paused": True},
    )

    assert result is replay
    args = orchestration.reserve.call_args
    assert args.args == (orchestration.uow_factory, "tenant-1")
    assert args.kwargs["principal_id"] == "principal-1"
    assert args.kwargs["account_id"] == "account-1"
    assert args.kwargs["tool_name"] == "update_media_buy"
    assert args.kwargs["idempotency_key"] == "update-idem-key-0001"
    assert args.kwargs["request_hash"] == canonical_payload_hash(
        {"idempotency_key": "update-idem-key-0001", "media_buy_id": "mb-1", "paused": True}
    )
    assert args.kwargs["lease"] == DEFAULT_IN_FLIGHT_LEASE
    decoded = args.kwargs["decode"](
        {
            "status": "completed",
            "response": UpdateMediaBuySuccess(
                media_buy_id="mb-1",
                affected_packages=[],
                context={"correlation_id": "original"},
            ).model_dump(mode="json"),
        }
    )
    assert decoded.response.context == {"correlation_id": "ctx-1"}
    assert args.kwargs["enforce_ceiling"] is True
    assert callable(args.kwargs["on_reserved"])
    orchestration.work.assert_not_called()


def test_fresh_wire_update_completes_the_owned_reservation(orchestration: _OrchestrationHarness) -> None:
    fresh = _result()
    identity = _identity()
    request = _request()
    orchestration.reserve.return_value = ReservationResult(attempt_id="attempt-1")
    orchestration.work.return_value = fresh
    result = _update_media_buy_impl(
        req=request,
        identity=identity,
        raw_wire_payload={"idempotency_key": "update-idem-key-0001", "media_buy_id": "mb-1", "paused": True},
    )

    assert result is fresh
    orchestration.work.assert_called_once_with(
        req=request,
        identity=identity,
        principal=orchestration.principal,
        adapter_override=orchestration.adapter,
        context_id=None,
        guard_downstream=True,
        downstream_request_hash=canonical_payload_hash(
            {"idempotency_key": "update-idem-key-0001", "media_buy_id": "mb-1", "paused": True}
        ),
        uow_override=orchestration.uow,
    )
    orchestration.complete.assert_called_once_with(
        orchestration.uow,
        attempt_id="attempt-1",
        response_model=fresh.response,
        protocol_status="completed",
    )


def test_failed_wire_update_releases_reservation_and_never_completes(
    orchestration: _OrchestrationHarness,
) -> None:
    failure = RuntimeError("adapter failed")
    orchestration.reserve.return_value = ReservationResult(attempt_id="attempt-1")
    orchestration.work.side_effect = failure
    with pytest.raises(RuntimeError, match="adapter failed"):
        _update_media_buy_impl(
            req=_request(),
            identity=_identity(),
            raw_wire_payload={"idempotency_key": "update-idem-key-0001", "media_buy_id": "mb-1", "paused": True},
        )

    orchestration.complete.assert_not_called()
    orchestration.uow.idempotency_attempts.release.assert_called_once_with("attempt-1")
