"""sync_accounts durable replay/conflict behavior (AdCP 3.1.1)."""

from __future__ import annotations

import asyncio
import threading
import uuid
from typing import Any

import pytest

from src.core.exceptions import AdCPIdempotencyConflictError, build_two_layer_error_envelope
from src.core.schemas.account import SyncAccountsRequest
from tests.harness.account_sync import AccountSyncEnv
from tests.harness.transport import Transport
from tests.helpers import assert_envelope_shape

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


def _accounts(domain: str = "replay.example.com") -> list[dict]:
    return [{"brand": {"domain": domain}, "operator": "example.com", "billing": "operator"}]


def _call(env: AccountSyncEnv, req: SyncAccountsRequest):
    return asyncio.run(env.call_impl_async(req=req))


def _action(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _call_with_identity(req: SyncAccountsRequest, identity: Any):
    from src.core.tools.accounts import _sync_accounts_impl

    return asyncio.run(_sync_accounts_impl(req=req, identity=identity))


def test_identical_retry_replays_original_created_response(integration_db) -> None:
    with AccountSyncEnv(tenant_id="acct_replay", principal_id="agent_acct_replay") as env:
        env.setup_default_data()
        key = f"acct-replay-{uuid.uuid4().hex}"
        req = SyncAccountsRequest(accounts=_accounts(), idempotency_key=key)

        first = _call(env, req)
        second = _call(env, req)

    assert first.replayed is False
    assert second.replayed is True
    assert _action(first.accounts[0].action) == "created"
    assert _action(second.accounts[0].action) == "created"
    assert second.accounts[0].account_id == first.accounts[0].account_id


def test_same_key_changed_payload_conflicts(integration_db) -> None:
    with AccountSyncEnv(tenant_id="acct_conflict", principal_id="agent_acct_conflict") as env:
        env.setup_default_data()
        key = f"acct-conflict-{uuid.uuid4().hex}"
        _call(env, SyncAccountsRequest(accounts=_accounts("a.example.com"), idempotency_key=key))

        with pytest.raises(AdCPIdempotencyConflictError) as exc_info:
            _call(env, SyncAccountsRequest(accounts=_accounts("b.example.com"), idempotency_key=key))

    assert_envelope_shape(
        build_two_layer_error_envelope(exc_info.value),
        "IDEMPOTENCY_CONFLICT",
        recovery="correctable",
    )


def test_fresh_key_executes_again(integration_db) -> None:
    with AccountSyncEnv(tenant_id="acct_fresh", principal_id="agent_acct_fresh") as env:
        env.setup_default_data()
        first = _call(
            env,
            SyncAccountsRequest(accounts=_accounts(), idempotency_key=f"acct-first-{uuid.uuid4().hex}"),
        )
        second = _call(
            env,
            SyncAccountsRequest(accounts=_accounts(), idempotency_key=f"acct-second-{uuid.uuid4().hex}"),
        )

    assert first.replayed is False
    assert second.replayed is False
    assert _action(first.accounts[0].action) == "created"
    assert _action(second.accounts[0].action) == "unchanged"


def test_concurrent_same_key_executes_exactly_once(integration_db) -> None:
    """A committed reservation rejects a live racer before account creation."""
    from src.core.exceptions import AdCPIdempotencyInFlightError
    from src.core.tools import accounts as accounts_module

    with AccountSyncEnv(tenant_id="acct_concurrent", principal_id="agent_acct_concurrent") as env:
        tenant, principal = env.setup_default_data()
        env._commit_factory_data()
        identity = env.identity
        req = SyncAccountsRequest(
            accounts=_accounts("concurrent.example.com"),
            idempotency_key=f"acct-concurrent-{uuid.uuid4().hex}",
        )

        reached_work = threading.Event()
        release_work = threading.Event()
        real_generate = accounts_module._generate_account_id

        def gated_generate() -> str:
            reached_work.set()
            assert release_work.wait(timeout=30)
            return real_generate()

        winner: dict[str, Any] = {}

        def run_winner() -> None:
            try:
                winner["result"] = _call_with_identity(req, identity)
            except Exception as exc:  # noqa: BLE001 - asserted by the parent thread
                winner["error"] = exc

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(accounts_module, "_generate_account_id", gated_generate)
            thread = threading.Thread(target=run_winner)
            thread.start()
            try:
                assert reached_work.wait(timeout=30)
                with pytest.raises(AdCPIdempotencyInFlightError):
                    _call_with_identity(req, identity)
            finally:
                release_work.set()
                thread.join(timeout=30)

        assert not thread.is_alive()
        assert "error" not in winner
        from src.core.database.repositories.uow import AccountUoW

        with AccountUoW(tenant.tenant_id) as uow:
            assert uow.accounts is not None
            assert len(uow.accounts.list_by_principal(principal.principal_id)) == 1


@pytest.mark.parametrize("transport", [Transport.MCP, Transport.A2A, Transport.REST])
def test_sync_accounts_wire_retry_replays_without_second_upsert(integration_db, transport: Transport) -> None:
    tenant_id = f"acct_wire_{transport.value}"
    principal_id = f"agent_acct_wire_{transport.value}"
    key = f"acct-wire-{transport.value}-{uuid.uuid4().hex}"

    with AccountSyncEnv(tenant_id=tenant_id, principal_id=principal_id) as env:
        env.setup_default_data()
        first = env.call_via(
            transport,
            accounts=_accounts(),
            idempotency_key=key,
            context={"correlation_id": "original"},
        )
        second = env.call_via(
            transport,
            accounts=_accounts(),
            idempotency_key=key,
            context={"correlation_id": "retry"},
        )

    assert first.is_success, first.error
    assert second.is_success, second.error
    assert first.payload.replayed is False
    assert second.payload.replayed is True
    assert _action(first.payload.accounts[0].action) == "created"
    assert _action(second.payload.accounts[0].action) == "created"
    assert second.payload.context.model_dump(exclude_none=True) == {"correlation_id": "retry"}


@pytest.mark.parametrize("transport", [Transport.MCP, Transport.A2A, Transport.REST])
def test_sync_accounts_wire_conflict_has_exact_envelope(integration_db, transport: Transport) -> None:
    tenant_id = f"acct_conflict_wire_{transport.value}"
    principal_id = f"agent_acct_conflict_wire_{transport.value}"
    key = f"acct-conflict-wire-{transport.value}-{uuid.uuid4().hex}"

    with AccountSyncEnv(tenant_id=tenant_id, principal_id=principal_id) as env:
        env.setup_default_data()
        first = env.call_via(
            transport,
            accounts=_accounts("original.example.com"),
            idempotency_key=key,
        )
        second = env.call_via(
            transport,
            accounts=_accounts("changed.example.com"),
            idempotency_key=key,
        )

    assert first.is_success, first.error
    assert second.is_error
    assert_envelope_shape(second.wire_error_envelope, "IDEMPOTENCY_CONFLICT", recovery="correctable")
