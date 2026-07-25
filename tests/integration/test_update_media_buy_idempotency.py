"""update_media_buy replay and canonical-payload conflict on a real database."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from src.core.database.repositories import MediaBuyUoW
from src.core.exceptions import (
    AdCPIdempotencyConflictError,
    AdCPIdempotencyExpiredError,
    AdCPServiceUnavailableError,
)
from src.core.idempotency_canonical import canonical_payload_hash
from src.core.resolved_identity import ResolvedIdentity
from src.core.schemas import Principal, UpdateMediaBuyRequest, UpdateMediaBuyResult, UpdateMediaBuySuccess
from src.core.tools.media_buy_update import _update_media_buy_impl
from tests.harness._base import BareIntegrationEnv

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


def _setup(env: BareIntegrationEnv) -> ResolvedIdentity:
    from tests.factories import PrincipalFactory, TenantFactory

    tenant = TenantFactory(tenant_id="update_idem_tenant")
    PrincipalFactory(tenant=tenant, principal_id="update_idem_principal")
    env._commit_factory_data()
    return PrincipalFactory.make_identity(
        principal_id="update_idem_principal",
        tenant_id="update_idem_tenant",
        tenant={"tenant_id": "update_idem_tenant"},
    ).model_copy(update={"account_id": "update_idem_account"})


def _request() -> UpdateMediaBuyRequest:
    return UpdateMediaBuyRequest(
        media_buy_id="mb-idem-1",
        paused=True,
        idempotency_key="update-real-idem-0001",
    )


def _principal() -> Principal:
    return Principal(
        principal_id="update_idem_principal",
        name="Test Advertiser update_idem_principal",
        platform_mappings={"mock": {"advertiser_id": "test_adv"}},
    )


def _success() -> UpdateMediaBuyResult:
    return UpdateMediaBuyResult(
        response=UpdateMediaBuySuccess(media_buy_id="mb-idem-1", affected_packages=[]),
        status="completed",
    )


def _adapter() -> MagicMock:
    return MagicMock(adapter_name="mock", supports_media_buy_update_reconciliation=True)


def _assert_single_work_call(
    work: MagicMock,
    *,
    request: UpdateMediaBuyRequest,
    identity: ResolvedIdentity,
    adapter: MagicMock,
    raw_wire_payload: dict[str, object],
) -> None:
    call_uow = work.call_args.kwargs["uow_override"]
    assert isinstance(call_uow, MediaBuyUoW)
    work.assert_called_once_with(
        req=request,
        identity=identity,
        principal=_principal(),
        adapter_override=adapter,
        context_id=None,
        guard_downstream=True,
        downstream_request_hash=canonical_payload_hash(raw_wire_payload),
        uow_override=call_uow,
    )


def test_identical_wire_retry_replays_without_reexecuting_work(integration_db) -> None:
    with BareIntegrationEnv() as env:
        identity = _setup(env)
        first_request = _request()
        adapter = _adapter()
        raw_wire_payload = {
            "idempotency_key": "update-real-idem-0001",
            "media_buy_id": "mb-idem-1",
            "paused": True,
        }
        with (
            patch("src.core.tools.media_buy_update.get_adapter", return_value=adapter),
            patch("src.core.tools.media_buy_update._update_media_buy_work", return_value=_success()) as work,
        ):
            first = _update_media_buy_impl(
                req=first_request,
                identity=identity,
                raw_wire_payload=raw_wire_payload,
            )
            replay = _update_media_buy_impl(
                req=_request(),
                identity=identity,
                raw_wire_payload=raw_wire_payload,
            )

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.response.media_buy_id == first.response.media_buy_id
    _assert_single_work_call(
        work,
        request=first_request,
        identity=identity,
        adapter=adapter,
        raw_wire_payload=raw_wire_payload,
    )


def test_changed_wire_payload_conflicts_before_work(integration_db) -> None:
    with BareIntegrationEnv() as env:
        identity = _setup(env)
        first_request = _request()
        adapter = _adapter()
        first_wire_payload = {
            "idempotency_key": "update-real-idem-0001",
            "media_buy_id": "mb-idem-1",
            "paused": True,
        }
        with (
            patch("src.core.tools.media_buy_update.get_adapter", return_value=adapter),
            patch("src.core.tools.media_buy_update._update_media_buy_work", return_value=_success()) as work,
        ):
            _update_media_buy_impl(
                req=first_request,
                identity=identity,
                raw_wire_payload=first_wire_payload,
            )
            with pytest.raises(AdCPIdempotencyConflictError):
                _update_media_buy_impl(
                    req=_request(),
                    identity=identity,
                    raw_wire_payload={
                        "idempotency_key": "update-real-idem-0001",
                        "media_buy_id": "mb-idem-1",
                        "paused": False,
                    },
                )

    _assert_single_work_call(
        work,
        request=first_request,
        identity=identity,
        adapter=adapter,
        raw_wire_payload=first_wire_payload,
    )


def test_unusable_completed_replay_fails_closed(integration_db) -> None:
    class MalformedStoredResponse(BaseModel):
        unexpected: str

    raw = {
        "idempotency_key": "update-real-idem-0001",
        "media_buy_id": "mb-idem-1",
        "paused": True,
    }
    with BareIntegrationEnv() as env:
        identity = _setup(env)
        with MediaBuyUoW("update_idem_tenant") as uow:
            assert uow.idempotency_attempts is not None
            uow.idempotency_attempts.record_success(
                principal_id="update_idem_principal",
                account_id="update_idem_account",
                tool_name="update_media_buy",
                idempotency_key="update-real-idem-0001",
                response_model=MalformedStoredResponse(unexpected="shape"),
                protocol_status="completed",
                payload_hash=canonical_payload_hash(raw),
            )

        with (
            patch("src.core.tools.media_buy_update._update_media_buy_work") as work,
            pytest.raises(AdCPServiceUnavailableError, match="could not be replayed safely"),
        ):
            _update_media_buy_impl(
                req=_request(),
                identity=identity,
                raw_wire_payload=raw,
            )

    work.assert_not_called()


def test_expired_completed_replay_requires_a_fresh_key(integration_db) -> None:
    raw = {
        "idempotency_key": "update-real-idem-0001",
        "media_buy_id": "mb-idem-1",
        "paused": True,
    }
    with BareIntegrationEnv() as env:
        identity = _setup(env)
        with MediaBuyUoW("update_idem_tenant") as uow:
            assert uow.idempotency_attempts is not None
            uow.idempotency_attempts.record_success(
                principal_id="update_idem_principal",
                account_id="update_idem_account",
                tool_name="update_media_buy",
                idempotency_key="update-real-idem-0001",
                response_model=_success().response,
                protocol_status="completed",
                payload_hash=canonical_payload_hash(raw),
                ttl=timedelta(seconds=1),
                now=datetime(2020, 1, 1, tzinfo=UTC),
            )

        with (
            patch("src.core.tools.media_buy_update._update_media_buy_work") as work,
            pytest.raises(AdCPIdempotencyExpiredError),
        ):
            _update_media_buy_impl(
                req=_request(),
                identity=identity,
                raw_wire_payload=raw,
            )

    work.assert_not_called()


def test_failure_after_reservation_releases_for_retry(integration_db) -> None:
    raw = {
        "idempotency_key": "update-real-idem-0001",
        "media_buy_id": "mb-idem-1",
        "paused": True,
    }
    with BareIntegrationEnv() as env:
        identity = _setup(env)
        with patch(
            "src.core.tools.media_buy_update._update_media_buy_work",
            side_effect=RuntimeError("adapter failed after reservation"),
        ) as work:
            with pytest.raises(RuntimeError, match="adapter failed"):
                _update_media_buy_impl(req=_request(), identity=identity, raw_wire_payload=raw)
            with pytest.raises(RuntimeError, match="adapter failed"):
                _update_media_buy_impl(req=_request(), identity=identity, raw_wire_payload=raw)
        assert work.call_count == 2
