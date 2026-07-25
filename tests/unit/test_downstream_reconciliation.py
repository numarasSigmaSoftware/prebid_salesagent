"""Consequential adapter mutations are claimed and reconciled before retry."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from threading import Barrier, BoundedSemaphore, Lock
from unittest.mock import MagicMock, patch

import pytest

from src.adapters.base import DownstreamMutation, ReconciliationOutcome, ReconciliationResult
from src.core.exceptions import (
    AdCPCapabilityNotSupportedError,
    AdCPIdempotencyConflictError,
    AdCPIdempotencyExpiredError,
    AdCPServiceUnavailableError,
)
from src.core.resolved_identity import ResolvedIdentity
from src.core.schemas import UpdateMediaBuySuccess
from src.services.downstream_reconciliation import (
    execute_reconciled_media_buy_create,
    execute_reconciled_media_buy_update,
)
from tests.factories import PrincipalFactory


@pytest.fixture(autouse=True)
def _local_operation_fence(monkeypatch):
    monkeypatch.setattr(
        "src.services.downstream_reconciliation._downstream_operation_fence",
        lambda _scope: nullcontext(MagicMock()),
    )


def _identity() -> ResolvedIdentity:
    return PrincipalFactory.make_identity(
        tenant_id="tenant-1",
        principal_id="principal-1",
        tenant={"tenant_id": "tenant-1"},
    ).model_copy(update={"account_id": "account-1"})


def _call(
    adapter,
    work,
    *,
    implementation_date=None,
    request_hash: str | None = None,
    is_applied=lambda _result: True,
):
    from src.core.idempotency_canonical import canonical_payload_hash

    effective_request_hash = request_hash or canonical_payload_hash(
        {
            "media_buy_id": "mb-1",
            "action": "pause_package",
            "package_id": "pkg-1",
            "budget": None,
        }
    )
    return execute_reconciled_media_buy_update(
        adapter=adapter,
        identity=_identity(),
        idempotency_key="update-key-0001",
        operation_key="pkg-1:pause_package",
        media_buy_id="mb-1",
        action="pause_package",
        package_id="pkg-1",
        budget=None,
        implementation_date=implementation_date,
        request_hash=effective_request_hash,
        response_decoder=UpdateMediaBuySuccess.model_validate,
        work=work,
        is_applied=is_applied,
    )


def test_downstream_request_id_separates_account_scopes() -> None:
    from src.services.downstream_reconciliation import _request_id

    common = ("tenant-1", "principal-1")
    first = _request_id(*common, "account-a", "same-key-0000001", "gam", "create_media_buy")
    second = _request_id(*common, "account-b", "same-key-0000001", "gam", "create_media_buy")

    assert first != second


def test_expired_claim_is_a_fail_closed_tombstone() -> None:
    from datetime import UTC, datetime, timedelta

    from src.core.idempotency_canonical import canonical_payload_hash

    repo = MagicMock()
    repo.get.return_value = MagicMock(
        claim_id="claim-expired",
        status="applied",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        request_hash=canonical_payload_hash(
            {
                "media_buy_id": "mb-1",
                "action": "pause_package",
                "package_id": "pkg-1",
                "budget": None,
            }
        ),
        result_metadata={"response": {"media_buy_id": "mb-1", "affected_packages": []}},
    )
    uow = MagicMock(downstream_mutation_claims=repo)
    uow.__enter__.return_value = uow
    adapter = MagicMock(adapter_name="mock", supports_media_buy_update_reconciliation=True)
    work = MagicMock()

    with (
        patch("src.services.downstream_reconciliation.DownstreamMutationClaimRepository", return_value=repo),
        pytest.raises(AdCPIdempotencyExpiredError, match="reconciliation window has expired"),
    ):
        _call(adapter, work)

    work.assert_not_called()


def test_surviving_claim_rejects_same_key_with_changed_full_request() -> None:
    """Fields outside the provider action descriptor remain conflict-significant."""
    repo = MagicMock()
    repo.get.return_value = MagicMock(
        claim_id="claim-ambiguous",
        status="unknown",
        expires_at=None,
        request_hash="a" * 64,
        result_metadata=None,
    )
    adapter = MagicMock(adapter_name="mock", supports_media_buy_update_reconciliation=True)
    work = MagicMock()

    with (
        patch("src.services.downstream_reconciliation.DownstreamMutationClaimRepository", return_value=repo),
        pytest.raises(AdCPIdempotencyConflictError),
    ):
        _call(adapter, work, request_hash="b" * 64)

    adapter.reconcile_media_buy_update.assert_not_called()
    work.assert_not_called()


def test_fresh_claim_is_marked_invoked_then_applied() -> None:
    repo = MagicMock()
    repo.get.return_value = None
    repo.create_from_values.return_value = MagicMock(
        claim_id="claim-1",
        status="planned",
        request_hash="ignored",
        result_metadata=None,
    )
    # Match the canonical hash generated by the service.
    from src.core.idempotency_canonical import canonical_payload_hash

    repo.create_from_values.return_value.request_hash = canonical_payload_hash(
        {
            "media_buy_id": "mb-1",
            "action": "pause_package",
            "package_id": "pkg-1",
            "budget": None,
        }
    )
    uow = MagicMock(downstream_mutation_claims=repo)
    uow.__enter__.return_value = uow
    adapter = MagicMock()
    result = UpdateMediaBuySuccess(media_buy_id="mb-1", affected_packages=[])
    work = MagicMock(return_value=result)

    with patch("src.services.downstream_reconciliation.DownstreamMutationClaimRepository", return_value=repo):
        assert _call(adapter, work) is result

    assert repo.create_from_values.call_args.args[0][3] == "MagicMock"
    assert [call.kwargs["status"] for call in repo.transition.call_args_list] == ["invoked", "applied"]


def test_definitive_adapter_error_is_recorded_not_applied_for_retry() -> None:
    from src.core.idempotency_canonical import canonical_payload_hash

    repo = MagicMock()
    repo.get.return_value = None
    repo.create_from_values.return_value = MagicMock(
        claim_id="claim-1",
        status="planned",
        request_hash=canonical_payload_hash(
            {
                "media_buy_id": "mb-1",
                "action": "pause_package",
                "package_id": "pkg-1",
                "budget": None,
            }
        ),
        result_metadata=None,
    )
    adapter = MagicMock(adapter_name="mock")
    result = UpdateMediaBuySuccess(media_buy_id="mb-1", affected_packages=[])

    with patch("src.services.downstream_reconciliation.DownstreamMutationClaimRepository", return_value=repo):
        assert _call(adapter, MagicMock(return_value=result), is_applied=lambda _result: False) is result

    assert [call.kwargs["status"] for call in repo.transition.call_args_list] == ["invoked", "not_applied"]


def test_definitive_not_applied_prior_outcome_resumes_once() -> None:
    from src.core.idempotency_canonical import canonical_payload_hash

    repo = MagicMock()
    repo.get.return_value = MagicMock(
        claim_id="claim-1",
        status="invoked",
        request_hash=canonical_payload_hash(
            {
                "media_buy_id": "mb-1",
                "action": "pause_package",
                "package_id": "pkg-1",
                "budget": None,
            }
        ),
        result_metadata=None,
    )
    uow = MagicMock(downstream_mutation_claims=repo)
    uow.__enter__.return_value = uow
    adapter = MagicMock(adapter_name="mock")
    adapter.reconcile_media_buy_update.return_value = ReconciliationResult(ReconciliationOutcome.NOT_APPLIED)
    expected = UpdateMediaBuySuccess(media_buy_id="mb-1", affected_packages=[])
    work = MagicMock(return_value=expected)

    with patch("src.services.downstream_reconciliation.DownstreamMutationClaimRepository", return_value=repo):
        assert _call(adapter, work) is expected

    work.assert_called_once_with()
    from src.services.downstream_reconciliation import _request_id

    adapter.reconcile_media_buy_update.assert_called_once_with(
        DownstreamMutation(
            downstream_request_id=_request_id(
                "tenant-1",
                "principal-1",
                "account-1",
                "update-key-0001",
                "mock",
                "pkg-1:pause_package",
            ),
            media_buy_id="mb-1",
            action="pause_package",
            package_id="pkg-1",
            budget=None,
            implementation_date=None,
        )
    )
    assert [call.kwargs["status"] for call in repo.transition.call_args_list] == [
        "not_applied",
        "invoked",
        "applied",
    ]


def test_existing_planned_claim_is_known_not_invoked_and_executes_once() -> None:
    from src.core.idempotency_canonical import canonical_payload_hash

    repo = MagicMock()
    repo.get.return_value = MagicMock(
        claim_id="claim-1",
        status="planned",
        request_hash=canonical_payload_hash(
            {
                "media_buy_id": "mb-1",
                "action": "pause_package",
                "package_id": "pkg-1",
                "budget": None,
            }
        ),
        result_metadata=None,
    )
    uow = MagicMock(downstream_mutation_claims=repo)
    uow.__enter__.return_value = uow
    adapter = MagicMock(adapter_name="mock", supports_media_buy_update_reconciliation=True)
    expected = UpdateMediaBuySuccess(media_buy_id="mb-1", affected_packages=[])
    work = MagicMock(return_value=expected)

    with patch("src.services.downstream_reconciliation.DownstreamMutationClaimRepository", return_value=repo):
        assert _call(adapter, work) is expected

    work.assert_called_once_with()


def test_retry_reuses_first_claim_implementation_date_across_midnight() -> None:
    from datetime import UTC, datetime

    from src.core.idempotency_canonical import canonical_payload_hash

    first_date = datetime(2026, 7, 25, tzinfo=UTC)
    retry_date = datetime(2026, 7, 26, tzinfo=UTC)
    expected = UpdateMediaBuySuccess(media_buy_id="mb-1", affected_packages=[])
    repo = MagicMock()
    repo.get.return_value = MagicMock(
        claim_id="claim-midnight",
        status="unknown",
        request_hash=canonical_payload_hash(
            {
                "media_buy_id": "mb-1",
                "action": "pause_package",
                "package_id": "pkg-1",
                "budget": None,
            }
        ),
        result_metadata={"implementation_date": first_date.isoformat()},
    )
    uow = MagicMock(downstream_mutation_claims=repo)
    uow.__enter__.return_value = uow
    adapter = MagicMock(adapter_name="mock", supports_media_buy_update_reconciliation=True)
    adapter.reconcile_media_buy_update.return_value = ReconciliationResult(
        ReconciliationOutcome.APPLIED,
        expected,
    )

    with patch("src.services.downstream_reconciliation.DownstreamMutationClaimRepository", return_value=repo):
        assert _call(adapter, MagicMock(), implementation_date=retry_date) is expected

    assert adapter.reconcile_media_buy_update.call_args.args[0].implementation_date == first_date


@pytest.mark.parametrize(
    ("outcome", "expected_statuses", "expected_work_calls"),
    [
        (ReconciliationOutcome.APPLIED, ["applied"], 0),
        (ReconciliationOutcome.NOT_APPLIED, ["not_applied", "invoked", "applied"], 1),
    ],
)
def test_ambiguous_claim_obeys_provider_reconciliation(
    outcome: ReconciliationOutcome,
    expected_statuses: list[str],
    expected_work_calls: int,
) -> None:
    from src.core.idempotency_canonical import canonical_payload_hash

    expected = UpdateMediaBuySuccess(media_buy_id="mb-1", affected_packages=[])
    repo = MagicMock()
    repo.get.return_value = MagicMock(
        claim_id="claim-1",
        status="unknown",
        request_hash=canonical_payload_hash(
            {
                "media_buy_id": "mb-1",
                "action": "pause_package",
                "package_id": "pkg-1",
                "budget": None,
            }
        ),
        result_metadata=None,
    )
    uow = MagicMock(downstream_mutation_claims=repo)
    uow.__enter__.return_value = uow
    adapter = MagicMock(adapter_name="mock", supports_media_buy_update_reconciliation=True)
    adapter.reconcile_media_buy_update.return_value = ReconciliationResult(
        outcome,
        expected if outcome is ReconciliationOutcome.APPLIED else None,
    )
    work = MagicMock(return_value=expected)

    with patch("src.services.downstream_reconciliation.DownstreamMutationClaimRepository", return_value=repo):
        assert _call(adapter, work) == expected

    assert work.call_count == expected_work_calls
    assert [call.kwargs["status"] for call in repo.transition.call_args_list] == expected_statuses


def test_unsupported_adapter_is_rejected_before_claim_or_provider_invocation() -> None:
    adapter = MagicMock(adapter_name="xandr", supports_media_buy_update_reconciliation=False)
    work = MagicMock()

    with pytest.raises(AdCPCapabilityNotSupportedError, match="cannot safely reconcile"):
        _call(adapter, work)

    work.assert_not_called()


def test_create_claim_crash_is_never_blindly_reinvoked() -> None:
    request_hash = "f" * 64
    repo = MagicMock()
    fresh = MagicMock(
        claim_id="claim-create",
        status="planned",
        request_hash=request_hash,
        result_metadata=None,
    )
    prior = MagicMock(
        claim_id="claim-create",
        status="unknown",
        request_hash=request_hash,
        result_metadata=None,
    )
    repo.get.side_effect = [None, prior]
    repo.create_from_values.return_value = fresh
    uow = MagicMock(downstream_mutation_claims=repo)
    uow.__enter__.return_value = uow
    adapter = MagicMock(
        adapter_name="mock",
        supports_media_buy_create_reconciliation=True,
    )
    work = MagicMock(side_effect=RuntimeError("provider accepted; response lost"))

    with patch("src.services.downstream_reconciliation.DownstreamMutationClaimRepository", return_value=repo):
        with pytest.raises(RuntimeError, match="response lost"):
            execute_reconciled_media_buy_create(
                adapter=adapter,
                identity=_identity(),
                idempotency_key="create-key-0001",
                request_hash=request_hash,
                response_decoder=UpdateMediaBuySuccess.model_validate,
                work=work,
            )
        with pytest.raises(AdCPServiceUnavailableError, match="outcome is unknown"):
            execute_reconciled_media_buy_create(
                adapter=adapter,
                identity=_identity(),
                idempotency_key="create-key-0001",
                request_hash=request_hash,
                response_decoder=UpdateMediaBuySuccess.model_validate,
                work=work,
            )

    work.assert_called_once_with()
    assert [call.kwargs["status"] for call in repo.transition.call_args_list] == ["invoked", "unknown"]


def test_applied_create_replay_restores_provider_and_local_continuation_metadata() -> None:
    request_hash = "e" * 64
    repo = MagicMock()
    fresh = MagicMock(
        claim_id="claim-create",
        status="planned",
        request_hash=request_hash,
        result_metadata=None,
    )
    repo.get.return_value = None
    repo.create_from_values.return_value = fresh
    response = UpdateMediaBuySuccess(media_buy_id="mb-1", affected_packages=[])
    object.__setattr__(response, "_platform_line_item_ids", {"pkg-original": "provider-42"})
    work = MagicMock(return_value=response)
    adapter = MagicMock(adapter_name="mock", supports_media_buy_create_reconciliation=True)
    pricing = {"pkg-original": {"pricing_model": "cpm", "bid_price": 12.5}}

    with patch("src.services.downstream_reconciliation.DownstreamMutationClaimRepository", return_value=repo):
        first = execute_reconciled_media_buy_create(
            adapter=adapter,
            identity=_identity(),
            idempotency_key="create-key-0002",
            request_hash=request_hash,
            response_decoder=UpdateMediaBuySuccess.model_validate,
            work=work,
            continuation_metadata={"package_pricing_info": pricing},
        )
        stored = repo.transition.call_args_list[-1].kwargs["result_metadata"]
        repo.get.return_value = MagicMock(
            claim_id="claim-create",
            status="applied",
            request_hash=request_hash,
            result_metadata=stored,
        )
        replayed = execute_reconciled_media_buy_create(
            adapter=adapter,
            identity=_identity(),
            idempotency_key="create-key-0002",
            request_hash=request_hash,
            response_decoder=UpdateMediaBuySuccess.model_validate,
            work=work,
            continuation_metadata={"package_pricing_info": {"pkg-new": {}}},
        )

    assert first == replayed
    assert work.call_count == 1
    assert replayed._platform_line_item_ids == {"pkg-original": "provider-42"}
    assert replayed._package_pricing_info == pricing


def test_ambiguous_create_reconciliation_persists_local_continuation_metadata() -> None:
    request_hash = "f" * 64
    pricing = {"pkg-original": {"pricing_model": "cpm", "bid_price": 12.5}}
    repo = MagicMock()
    repo.get.return_value = MagicMock(
        claim_id="claim-create",
        status="unknown",
        request_hash=request_hash,
        result_metadata=None,
    )
    response = UpdateMediaBuySuccess(media_buy_id="mb-1", affected_packages=[])
    object.__setattr__(response, "_platform_line_item_ids", {"pkg-original": "provider-42"})
    adapter = MagicMock(adapter_name="mock", supports_media_buy_create_reconciliation=True)
    adapter.reconcile_media_buy_create.return_value = ReconciliationResult(
        ReconciliationOutcome.APPLIED,
        response,
    )
    work = MagicMock()

    with patch("src.services.downstream_reconciliation.DownstreamMutationClaimRepository", return_value=repo):
        result = execute_reconciled_media_buy_create(
            adapter=adapter,
            identity=_identity(),
            idempotency_key="create-key-0003",
            request_hash=request_hash,
            response_decoder=UpdateMediaBuySuccess.model_validate,
            work=work,
            continuation_metadata={"package_pricing_info": pricing},
        )

    work.assert_not_called()
    assert result.media_buy_id == "mb-1"
    assert result._platform_line_item_ids == {"pkg-original": "provider-42"}
    assert result._package_pricing_info == pricing
    assert repo.transition.call_args.kwargs["result_metadata"] == {
        "response": response.model_dump(mode="json"),
        "continuation": {"package_pricing_info": pricing},
        "platform_line_item_ids": {"pkg-original": "provider-42"},
    }


def test_concurrent_reconciliation_progresses_when_domain_pool_is_saturated() -> None:
    """The independent fence pool prevents domain-pool hold-and-wait deadlock."""
    domain_pool = BoundedSemaphore(3)
    coordination_pool = BoundedSemaphore(1)
    barrier = Barrier(3)
    state_lock = Lock()
    domain_checked_out = 0
    peak_domain_checked_out = 0

    @contextmanager
    def domain_connection():
        nonlocal domain_checked_out, peak_domain_checked_out
        assert domain_pool.acquire(timeout=1), "domain connection pool exhausted"
        with state_lock:
            domain_checked_out += 1
            peak_domain_checked_out = max(peak_domain_checked_out, domain_checked_out)
        try:
            yield MagicMock()
        finally:
            with state_lock:
                domain_checked_out -= 1
            domain_pool.release()

    def repository_factory(_session, _tenant_id):
        repo = MagicMock()
        repo.get.return_value = None
        repo.create_from_values.return_value = MagicMock(
            claim_id="claim",
            status="planned",
            request_hash=canonical_payload_hash(
                {
                    "media_buy_id": "mb-1",
                    "action": "pause_package",
                    "package_id": "pkg-1",
                    "budget": None,
                }
            ),
            result_metadata=None,
        )
        return repo

    @contextmanager
    def pooled_fence(_scope):
        assert coordination_pool.acquire(timeout=1), "coordination connection pool exhausted"
        try:
            yield MagicMock()
        finally:
            coordination_pool.release()

    def run_one() -> UpdateMediaBuySuccess:
        with domain_connection():  # caller-owned domain transaction
            barrier.wait(timeout=1)
            adapter = MagicMock(adapter_name="mock", supports_media_buy_update_reconciliation=True)
            expected = UpdateMediaBuySuccess(media_buy_id="mb-1", affected_packages=[])
            return _call(adapter, MagicMock(return_value=expected))

    from src.core.idempotency_canonical import canonical_payload_hash

    with (
        patch(
            "src.services.downstream_reconciliation._downstream_operation_fence",
            side_effect=pooled_fence,
        ),
        patch(
            "src.services.downstream_reconciliation.DownstreamMutationClaimRepository",
            side_effect=repository_factory,
        ),
        ThreadPoolExecutor(max_workers=3) as executor,
    ):
        results = list(executor.map(lambda _: run_one(), range(3)))

    assert len(results) == 3
    assert peak_domain_checked_out == 3
