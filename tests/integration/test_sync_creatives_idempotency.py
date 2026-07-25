"""sync_creatives idempotency: replay + conflict + fresh-key re-execution (AdCP 3.1.1).

Until #1546 the sync_creatives ``idempotency_key`` was validated but INERT — a
same-key retry re-executed. These tests pin the wired reservation:

- an identical retry (same key + payload) replays the ORIGINAL success verbatim
  (``replayed=True``) — the cached "created" is returned, NOT re-derived to
  "unchanged";
- the same key with a DIFFERENT canonical payload is ``IDEMPOTENCY_CONFLICT``;
- a DIFFERENT key with an identical payload executes fresh (no cross-key replay),
  observably re-deriving the creative to "unchanged".

sync_creatives reserves in its own committed transaction and completes
strictly after successful work. Handler failures release the reservation so
errors are never cached and a corrected retry can execute.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from src.core.exceptions import AdCPIdempotencyConflictError, build_two_layer_error_envelope
from tests.harness import CreativeSyncEnv
from tests.harness.creative_sync import CreativeSyncIdempotencyWireEnv
from tests.harness.transport import Transport
from tests.helpers import assert_envelope_shape
from tests.helpers.creative_test_helpers import creative_payload

DEFAULT_AGENT_URL = "https://creative.test.example.com"

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


def _creative(creative_id: str = "c_idem_1", name: str = "Idempotent Creative") -> dict:
    return creative_payload(
        creative_id=creative_id,
        name=name,
        format_id={"id": "display_300x250", "agent_url": DEFAULT_AGENT_URL},
    )


def _action(a) -> str:
    return a.value if hasattr(a, "value") else str(a)


class TestSyncCreativesIdempotency:
    def test_identical_retry_replays_verbatim(self, integration_db):
        with CreativeSyncEnv(tenant_id="cre_replay", principal_id="agent_cre_replay") as env:
            env.setup_default_data()
            key = f"cre-replay-{uuid.uuid4().hex}"

            first = env.call_impl(creatives=[_creative()], idempotency_key=key)
            assert first.replayed is False
            assert _action(first.creatives[0].action) == "created"

            second = env.call_impl(creatives=[_creative()], idempotency_key=key)

        # The spec's top-level marker is present only on the replay, and the cached
        # "created" is replayed verbatim (NOT re-executed to "unchanged").
        assert second.replayed is True
        assert _action(second.creatives[0].action) == "created"
        assert second.creatives[0].creative_id == first.creatives[0].creative_id

    def test_same_key_different_payload_conflicts(self, integration_db):
        with CreativeSyncEnv(tenant_id="cre_conflict", principal_id="agent_cre_conflict") as env:
            env.setup_default_data()
            key = f"cre-conflict-{uuid.uuid4().hex}"

            first = env.call_impl(creatives=[_creative(name="Original")], idempotency_key=key)
            assert first.replayed is False

            with pytest.raises(AdCPIdempotencyConflictError) as excinfo:
                env.call_impl(creatives=[_creative(name="Changed Name")], idempotency_key=key)

        assert_envelope_shape(
            build_two_layer_error_envelope(excinfo.value),
            "IDEMPOTENCY_CONFLICT",
            recovery="correctable",
        )

    def test_fresh_key_re_executes(self, integration_db):
        """A DIFFERENT key with an identical payload executes fresh (no cross-key replay)."""
        with CreativeSyncEnv(tenant_id="cre_fresh", principal_id="agent_cre_fresh") as env:
            env.setup_default_data()

            first = env.call_impl(creatives=[_creative()], idempotency_key=f"k1-{uuid.uuid4().hex}")
            assert first.replayed is False
            assert _action(first.creatives[0].action) == "created"

            second = env.call_impl(creatives=[_creative()], idempotency_key=f"k2-{uuid.uuid4().hex}")

        # Different key -> real re-execution: no replay marker, and the identical
        # creative is re-derived to "unchanged"/"updated" (NOT the verbatim
        # "created" a cross-key replay would have echoed).
        assert second.replayed is False
        assert _action(second.creatives[0].action) in ("unchanged", "updated")

    def test_failure_after_reservation_releases_for_retry(self, integration_db):
        from src.core.tools.creatives._sync import _sync_creatives_impl

        with CreativeSyncEnv(tenant_id="cre_fail_closed", principal_id="agent_cre_fail_closed") as env:
            env.setup_default_data()
            env._commit_factory_data()
            key = f"cre-fail-{uuid.uuid4().hex}"
            raw = {"creatives": [_creative()], "idempotency_key": key}

            with patch(
                "src.core.tools.creatives._sync._sync_creatives_work",
                side_effect=RuntimeError("failure after reservation"),
            ) as work:
                with pytest.raises(RuntimeError, match="failure after reservation"):
                    _sync_creatives_impl(
                        creatives=[_creative()],
                        idempotency_key=key,
                        identity=env.identity,
                        raw_wire_payload=raw,
                    )
                with pytest.raises(RuntimeError, match="failure after reservation"):
                    _sync_creatives_impl(
                        creatives=[_creative()],
                        idempotency_key=key,
                        identity=env.identity,
                        raw_wire_payload=raw,
                    )
            assert work.call_count == 2

    def test_crash_before_cache_completion_does_not_duplicate_workflow(self, integration_db):
        """A retry resumes deterministic internal upserts after the cache-write crash window."""
        from src.core.database.repositories.uow import WorkflowUoW
        from src.core.tools.creatives import _sync as sync_module

        with CreativeSyncEnv(tenant_id="cre_crash_fence", principal_id="agent_cre_crash_fence") as env:
            env.setup_default_data()
            env._commit_factory_data()
            env.identity.tenant["approval_mode"] = "require-human"
            key = f"cre-crash-{uuid.uuid4().hex}"
            payload = _creative(creative_id="creative-crash-fence")
            raw = {"creatives": [payload], "idempotency_key": key}
            original_complete = sync_module.complete_idempotent
            calls = 0

            def fail_first_completion(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("crash before cache completion")
                return original_complete(*args, **kwargs)

            with patch.object(sync_module, "complete_idempotent", side_effect=fail_first_completion):
                with pytest.raises(RuntimeError, match="crash before cache completion"):
                    sync_module._sync_creatives_impl(
                        creatives=[payload],
                        idempotency_key=key,
                        identity=env.identity,
                        raw_wire_payload=raw,
                    )
                retry = sync_module._sync_creatives_impl(
                    creatives=[payload],
                    idempotency_key=key,
                    identity=env.identity,
                    raw_wire_payload=raw,
                )

            assert retry.replayed is False
            with WorkflowUoW(env._tenant_id) as uow:
                assert uow.workflows is not None
                assert (
                    uow.workflows.count_by_tenant(
                        object_type="creative",
                        object_id="creative-crash-fence",
                    )
                    == 1
                )


@pytest.mark.parametrize("transport", [Transport.MCP, Transport.A2A, Transport.REST])
def test_sync_creatives_wire_retry_replays_without_second_write(integration_db, transport: Transport) -> None:
    """Every exposed boundary uses the durable cache and echoes the retry context."""
    tenant_id = f"cre_wire_{transport.value}"
    principal_id = f"agent_cre_wire_{transport.value}"
    key = f"cre-wire-{transport.value}-{uuid.uuid4().hex}"
    creative = _creative(creative_id=f"creative-wire-{transport.value}")

    with CreativeSyncIdempotencyWireEnv(tenant_id=tenant_id, principal_id=principal_id) as env:
        env.setup_default_data()
        first = env.call_via(
            transport,
            creatives=[creative],
            idempotency_key=key,
            context={"correlation_id": "original"},
        )
        second = env.call_via(
            transport,
            creatives=[creative],
            idempotency_key=key,
            context={"correlation_id": "retry"},
        )

    assert first.is_success, first.error
    assert second.is_success, second.error
    assert first.payload.replayed is False
    assert second.payload.replayed is True
    assert _action(first.payload.creatives[0].action) == "created"
    assert _action(second.payload.creatives[0].action) == "created"
    assert second.payload.context.model_dump(exclude_none=True) == {"correlation_id": "retry"}


@pytest.mark.parametrize("transport", [Transport.MCP, Transport.A2A, Transport.REST])
def test_sync_creatives_wire_conflict_has_exact_envelope(integration_db, transport: Transport) -> None:
    tenant_id = f"cre_conflict_wire_{transport.value}"
    principal_id = f"agent_cre_conflict_wire_{transport.value}"
    key = f"cre-conflict-wire-{transport.value}-{uuid.uuid4().hex}"

    with CreativeSyncIdempotencyWireEnv(tenant_id=tenant_id, principal_id=principal_id) as env:
        env.setup_default_data()
        first = env.call_via(
            transport,
            creatives=[_creative(name="Original")],
            idempotency_key=key,
        )
        second = env.call_via(
            transport,
            creatives=[_creative(name="Changed")],
            idempotency_key=key,
        )

    assert first.is_success, first.error
    assert second.is_error
    assert_envelope_shape(second.wire_error_envelope, "IDEMPOTENCY_CONFLICT", recovery="correctable")
