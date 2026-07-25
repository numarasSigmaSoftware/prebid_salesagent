"""Crash-safe creative-agent build reconciliation."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from src.core.exceptions import AdCPServiceUnavailableError
from src.services.downstream_reconciliation import execute_reconciled_creative_build


@contextmanager
def _fence():
    session = MagicMock()
    yield session


def _execute(repo: MagicMock, work: MagicMock) -> dict:
    with (
        patch(
            "src.services.downstream_reconciliation._downstream_operation_fence",
            return_value=_fence(),
        ),
        patch(
            "src.services.downstream_reconciliation.DownstreamMutationClaimRepository",
            return_value=repo,
        ),
    ):
        return execute_reconciled_creative_build(
            tenant_id="tenant-1",
            principal_id="principal-1",
            account_id="account-1",
            idempotency_key="creative-build-key-1",
            agent_url="https://creative-agent.test",
            creative_id="creative-1",
            request_payload={"format_id": "display", "message": "Build it"},
            work=work,
        )


def test_successful_build_is_durably_replayed_without_second_invocation() -> None:
    repo = MagicMock()
    applied = MagicMock(
        claim_id="claim-1",
        status="applied",
        request_hash="unused",
        result_metadata={"response": {"status": "draft", "context_id": "ctx-1"}},
    )
    repo.get.side_effect = [None, applied]
    repo.create_from_values.side_effect = lambda values, **_kwargs: MagicMock(
        claim_id="claim-1",
        status="planned",
        request_hash=values[-1],
        result_metadata=None,
    )
    work = MagicMock(return_value={"status": "draft", "context_id": "ctx-1"})

    # The helper computes the canonical hash; use the value captured on create
    # for the synthetic replay row.
    first = _execute(repo, work)
    applied.request_hash = repo.create_from_values.call_args.args[0][-1]
    second = _execute(repo, work)

    assert first == second == {"status": "draft", "context_id": "ctx-1"}
    work.assert_called_once_with()


def test_ambiguous_build_fails_closed_without_blind_retry() -> None:
    repo = MagicMock()
    unknown = MagicMock(
        claim_id="claim-1",
        status="unknown",
        request_hash="unused",
        result_metadata=None,
    )
    repo.get.side_effect = [None, unknown]
    repo.create_from_values.side_effect = lambda values, **_kwargs: MagicMock(
        claim_id="claim-1",
        status="planned",
        request_hash=values[-1],
        result_metadata=None,
    )
    work = MagicMock(side_effect=RuntimeError("response lost"))

    with pytest.raises(RuntimeError, match="response lost"):
        _execute(repo, work)
    unknown.request_hash = repo.create_from_values.call_args.args[0][-1]
    with pytest.raises(AdCPServiceUnavailableError, match="outcome is unknown"):
        _execute(repo, work)

    work.assert_called_once_with()
