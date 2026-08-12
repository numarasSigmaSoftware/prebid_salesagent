"""Durable A2A tasks/get correlation via the persisted outer task ID.

The A2A boundary persists its outer ``task_*`` id on the create step's
``request_data.external_task_id``. These tests verify the two halves of the durable
path against a real DB:

  * ``WorkflowRepository.get_by_external_task_id`` resolves the buyer's id → step,
    tenant-scoped (a different tenant cannot resolve it) AND principal-scoped (a
    same-tenant sibling buyer cannot resolve it either).
  * ``AdCPRequestHandler.on_get_task`` rebuilds a terminal ``Task`` (with the stored
    ``response_data`` artifact) from that step when the id is NOT in the in-memory map
    — i.e. after the out-of-band approval / a restart.
"""

import asyncio
import json
from threading import Event, Thread, current_thread
from unittest.mock import patch

import pytest
from a2a.types import GetTaskRequest, TaskNotFoundError, TaskState

from src.core.context_manager import ContextManager
from src.core.database.repositories import WorkflowUoW
from tests.factories import PrincipalFactory
from tests.integration.conftest import make_create_media_buy_step

_EXTERNAL_TASK_ID = "task_buyer_abc123"
# A second buyer in the SAME tenant. ``contexts.principal_id`` carries no FK, so the
# sibling needs no Principal row — only a distinct id the scope filter must reject.
_SIBLING_PRINCIPAL_ID = "sibling_principal"


def _make_step(
    context_manager: ContextManager,
    tenant_id: str,
    principal_id: str,
    *,
    status: str = "completed",
    external_task_id: str = _EXTERNAL_TASK_ID,
    response_data: dict | None = None,
) -> str:
    """Create a create-media-buy workflow step carrying an external_task_id (returns step_id)."""
    step = make_create_media_buy_step(
        context_manager,
        tenant_id,
        principal_id,
        status=status,
        external_task_id=external_task_id,
        response_data=response_data if response_data is not None else {"media_buy_id": "mb_1", "revision": 2},
    )
    return step.step_id


def _make_completed_step(context_manager: ContextManager, tenant_id: str, principal_id: str) -> str:
    return _make_step(context_manager, tenant_id, principal_id, status="completed")


@pytest.mark.requires_db
class TestA2ATaskCorrelation:
    @pytest.fixture
    def context_manager(self):
        return ContextManager()

    def test_get_by_external_task_id_resolves_within_tenant(
        self, integration_db, sample_tenant, sample_principal, context_manager
    ):
        """The stored outer task id resolves to its step within the owning tenant."""
        tenant_id = sample_tenant["tenant_id"]
        step_id = _make_completed_step(context_manager, tenant_id, sample_principal["principal_id"])

        with WorkflowUoW(tenant_id) as uow:
            assert uow.workflows is not None
            step = uow.workflows.get_by_external_task_id(
                _EXTERNAL_TASK_ID, principal_id=sample_principal["principal_id"]
            )
            assert step is not None
            assert step.step_id == step_id
            assert step.status == "completed"

    def test_replayed_a2a_task_alias_resolves_to_original_step(
        self, integration_db, sample_tenant, sample_principal, context_manager
    ):
        """A second outer task from an idempotent submitted retry remains pollable.

        The replay deliberately reuses the original submitted response and must
        not create a second workflow step.  Its freshly allocated A2A task ID is
        therefore persisted as an alias of that original step.
        """
        tenant_id = sample_tenant["tenant_id"]
        principal_id = sample_principal["principal_id"]
        step_id = _make_completed_step(context_manager, tenant_id, principal_id)
        replay_task_id = "task_replayed_outer_456"

        with WorkflowUoW(tenant_id) as uow:
            assert uow.workflows is not None
            assert uow.workflows.register_external_task_alias(step_id, replay_task_id, principal_id=principal_id)

        with WorkflowUoW(tenant_id) as uow:
            assert uow.workflows is not None
            replayed_step = uow.workflows.get_by_external_task_id(replay_task_id, principal_id=principal_id)
            assert replayed_step is not None
            assert replayed_step.step_id == step_id

    def test_concurrent_replayed_task_aliases_are_not_lost(
        self, integration_db, sample_tenant, sample_principal, context_manager
    ):
        """Independent replay transactions preserve every buyer-visible task ID."""
        tenant_id = sample_tenant["tenant_id"]
        principal_id = sample_principal["principal_id"]
        step_id = _make_completed_step(context_manager, tenant_id, principal_id)
        first_task_id = "task_replayed_outer_a"
        second_task_id = "task_replayed_outer_b"
        second_lock_query_started = Event()
        second_finished = Event()

        from sqlalchemy import event

        from src.core.database.database_session import get_engine

        def observe_second_lock_query(conn, cursor, statement, parameters, context, executemany):
            if (
                current_thread().name == "a2a-alias-retry"
                and "workflow_steps" in statement
                and "FOR UPDATE" in statement
            ):
                second_lock_query_started.set()

        engine = get_engine()
        event.listen(engine, "before_cursor_execute", observe_second_lock_query)
        worker: Thread | None = None

        def register_second_alias() -> None:
            with WorkflowUoW(tenant_id) as uow:
                assert uow.workflows is not None
                assert uow.workflows.register_external_task_alias(step_id, second_task_id, principal_id=principal_id)
            second_finished.set()

        try:
            with WorkflowUoW(tenant_id) as first_uow:
                assert first_uow.workflows is not None
                assert first_uow.workflows.register_external_task_alias(
                    step_id, first_task_id, principal_id=principal_id
                )
                worker = Thread(target=register_second_alias, name="a2a-alias-retry")
                worker.start()
                assert second_lock_query_started.wait(timeout=1), "second transaction must reach the row-lock query"
                assert not second_finished.wait(timeout=0.1), (
                    "second alias must wait for the first transaction's row lock"
                )
        finally:
            event.remove(engine, "before_cursor_execute", observe_second_lock_query)
            if worker is not None:
                worker.join(timeout=3)

        assert worker is not None
        assert not worker.is_alive()
        assert second_finished.is_set()
        with WorkflowUoW(tenant_id) as uow:
            assert uow.workflows is not None
            assert uow.workflows.get_by_external_task_id(first_task_id, principal_id=principal_id) is not None
            assert uow.workflows.get_by_external_task_id(second_task_id, principal_id=principal_id) is not None

    def test_get_by_external_task_id_is_tenant_scoped(
        self, integration_db, sample_tenant, sample_principal, context_manager
    ):
        """A different tenant cannot resolve another tenant's task id."""
        tenant_id = sample_tenant["tenant_id"]
        _make_completed_step(context_manager, tenant_id, sample_principal["principal_id"])

        with WorkflowUoW("other_tenant") as uow:
            assert uow.workflows is not None
            assert (
                uow.workflows.get_by_external_task_id(_EXTERNAL_TASK_ID, principal_id=sample_principal["principal_id"])
                is None
            )

    def test_get_by_external_task_id_is_principal_scoped(
        self, integration_db, sample_tenant, sample_principal, context_manager
    ):
        """A SAME-TENANT sibling buyer cannot resolve another principal's task id.

        The red oracle for the principal scope: dropping the
        ``DBContext.principal_id`` filter in ``get_by_external_task_id`` makes the
        sibling's lookup resolve the owner's step, and this assertion fails. The
        owner's own lookup is asserted alongside it so the filter cannot be
        "passed" by rejecting everyone.
        """
        tenant_id = sample_tenant["tenant_id"]
        step_id = _make_completed_step(context_manager, tenant_id, sample_principal["principal_id"])

        with WorkflowUoW(tenant_id) as uow:
            assert uow.workflows is not None
            assert uow.workflows.get_by_external_task_id(_EXTERNAL_TASK_ID, principal_id=_SIBLING_PRINCIPAL_ID) is None
            owner_step = uow.workflows.get_by_external_task_id(
                _EXTERNAL_TASK_ID, principal_id=sample_principal["principal_id"]
            )
            assert owner_step is not None
            assert owner_step.step_id == step_id

    def test_on_get_task_does_not_serve_a_sibling_principals_outcome(
        self, integration_db, sample_tenant, sample_principal, context_manager
    ):
        """The same scope, end-to-end through the durable poll.

        A sibling buyer in the owner's tenant polls the owner's task id with no
        in-memory task: the durable rebuild must find nothing, so ``on_get_task``
        raises the same not-found response as an unknown task rather than serving
        the owner's terminal result artifact.
        """
        from src.a2a_server.adcp_a2a_server import AdCPRequestHandler

        tenant_id = sample_tenant["tenant_id"]
        _make_completed_step(context_manager, tenant_id, sample_principal["principal_id"])

        handler = AdCPRequestHandler.__new__(AdCPRequestHandler)
        handler.tasks = {}  # in-memory miss → the poll must go through the scoped durable read
        sibling = PrincipalFactory.make_identity(
            principal_id=_SIBLING_PRINCIPAL_ID, tenant_id=tenant_id, protocol="a2a"
        )

        with (
            patch.object(handler, "_get_auth_token", return_value="tok"),
            patch.object(handler, "_resolve_a2a_identity", return_value=sibling),
        ):
            with pytest.raises(TaskNotFoundError):
                asyncio.run(handler.on_get_task(GetTaskRequest(id=_EXTERNAL_TASK_ID), context=None))

    def test_on_get_task_rebuilds_terminal_task_from_step(
        self, integration_db, sample_tenant, sample_principal, context_manager
    ):
        """tasks/get of an id NOT in memory rebuilds a COMPLETED Task with the stored artifact."""
        from src.a2a_server.adcp_a2a_server import AdCPRequestHandler

        tenant_id = sample_tenant["tenant_id"]
        _make_completed_step(context_manager, tenant_id, sample_principal["principal_id"])

        handler = AdCPRequestHandler.__new__(AdCPRequestHandler)
        handler.tasks = {}  # in-memory miss → forces the durable DB fallback
        identity = PrincipalFactory.make_identity(
            principal_id=sample_principal["principal_id"], tenant_id=tenant_id, protocol="a2a"
        )

        with (
            patch.object(handler, "_get_auth_token", return_value="tok"),
            patch.object(handler, "_resolve_a2a_identity", return_value=identity),
        ):
            task = asyncio.run(handler.on_get_task(GetTaskRequest(id=_EXTERNAL_TASK_ID), context=None))

        assert task is not None
        assert task.id == _EXTERNAL_TASK_ID
        assert task.status.state == TaskState.TASK_STATE_COMPLETED
        assert len(task.artifacts) == 1
        assert task.artifacts[0].name == "media_buy_result"

    def test_on_get_task_terminal_in_memory_task_short_circuits_for_the_owner(
        self, integration_db, sample_tenant, sample_principal, context_manager
    ):
        """An OWNED terminal in-memory task is authoritative — served without a DB lookup.

        The poller still authenticates (fixing the bypass below); the short-circuit
        is that an owned, already-terminal hit skips the durable read, not that it
        skips authorization.
        """
        from a2a.types import Task, TaskStatus

        from src.a2a_server.adcp_a2a_server import AdCPRequestHandler

        tenant_id = sample_tenant["tenant_id"]
        principal_id = sample_principal["principal_id"]
        identity = PrincipalFactory.make_identity(principal_id=principal_id, tenant_id=tenant_id, protocol="a2a")

        handler = AdCPRequestHandler.__new__(AdCPRequestHandler)
        done = Task(id="task_done", status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED))
        handler.tasks = {"task_done": done}
        handler._task_owners = {"task_done": (tenant_id, principal_id)}

        with (
            patch.object(handler, "_get_auth_token", return_value="tok") as mock_auth,
            patch.object(handler, "_resolve_a2a_identity", return_value=identity),
            patch("src.a2a_server.adcp_a2a_server.resolve_durable_task_outcome") as mock_durable,
        ):
            task = asyncio.run(handler.on_get_task(GetTaskRequest(id="task_done"), context=None))

        assert task is done
        # The poller IS authenticated even for a fast-path hit — the same
        # ``context`` on_get_task received (None, from this call) is what it
        # forwards to _get_auth_token.
        mock_auth.assert_called_once_with(None)
        mock_durable.assert_not_called()  # ...but an OWNED terminal hit still skips the DB round-trip

    def test_on_get_task_terminal_in_memory_task_denies_a_non_owner(
        self, integration_db, sample_tenant, sample_principal, context_manager
    ):
        """A terminal in-memory task is NOT served to a caller who doesn't own it.

        The red oracle for the auth bypass: before authorization was added, ANY
        caller who supplied this task_id got the owner's terminal artifacts back,
        with zero authentication. A sibling buyer authenticating as themselves
        must get ``TaskNotFoundError`` instead — identical to an unknown task_id, so this
        cannot be used as an existence oracle either.
        """
        from a2a.types import Task, TaskStatus

        from src.a2a_server.adcp_a2a_server import AdCPRequestHandler

        tenant_id = sample_tenant["tenant_id"]
        owner_id = sample_principal["principal_id"]
        sibling = PrincipalFactory.make_identity(
            principal_id=_SIBLING_PRINCIPAL_ID, tenant_id=tenant_id, protocol="a2a"
        )

        handler = AdCPRequestHandler.__new__(AdCPRequestHandler)
        done = Task(id="task_owned", status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED))
        handler.tasks = {"task_owned": done}
        handler._task_owners = {"task_owned": (tenant_id, owner_id)}

        with (
            patch.object(handler, "_get_auth_token", return_value="tok"),
            patch.object(handler, "_resolve_a2a_identity", return_value=sibling),
        ):
            with pytest.raises(TaskNotFoundError):
                asyncio.run(handler.on_get_task(GetTaskRequest(id="task_owned"), context=None))

    def test_on_get_task_denies_an_unauthenticated_poller(
        self, integration_db, sample_tenant, sample_principal, context_manager
    ):
        """A poller who cannot authenticate at all gets ``TaskNotFoundError``, even for a terminal
        in-memory hit — no anonymous existence or content oracle survives the fix."""
        from a2a.types import Task, TaskStatus

        from src.a2a_server.adcp_a2a_server import AdCPRequestHandler

        tenant_id = sample_tenant["tenant_id"]
        owner_id = sample_principal["principal_id"]

        handler = AdCPRequestHandler.__new__(AdCPRequestHandler)
        done = Task(id="task_owned", status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED))
        handler.tasks = {"task_owned": done}
        handler._task_owners = {"task_owned": (tenant_id, owner_id)}

        with patch.object(handler, "_get_auth_token", return_value=None):
            with pytest.raises(TaskNotFoundError):
                asyncio.run(handler.on_get_task(GetTaskRequest(id="task_owned"), context=None))

    def test_on_cancel_task_requires_the_same_owner_as_tasks_get(self, integration_db, sample_tenant, sample_principal):
        """A task id is a correlation handle, not authority to cancel another buyer's task."""
        from a2a.types import CancelTaskRequest, Task, TaskStatus

        from src.a2a_server.adcp_a2a_server import AdCPRequestHandler

        tenant_id = sample_tenant["tenant_id"]
        owner_id = sample_principal["principal_id"]
        owner = PrincipalFactory.make_identity(principal_id=owner_id, tenant_id=tenant_id, protocol="a2a")
        sibling = PrincipalFactory.make_identity(
            principal_id=_SIBLING_PRINCIPAL_ID, tenant_id=tenant_id, protocol="a2a"
        )
        handler = AdCPRequestHandler.__new__(AdCPRequestHandler)
        submitted = Task(id="task_cancellable", status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED))
        handler.tasks = {submitted.id: submitted}
        handler._task_owners = {submitted.id: (tenant_id, owner_id)}

        with (
            patch.object(handler, "_get_auth_token", return_value="tok"),
            patch.object(handler, "_resolve_a2a_identity", return_value=sibling),
            pytest.raises(TaskNotFoundError),
        ):
            asyncio.run(handler.on_cancel_task(CancelTaskRequest(id=submitted.id), context=None))

        with (
            patch.object(handler, "_get_auth_token", return_value="tok"),
            patch.object(handler, "_resolve_a2a_identity", return_value=owner),
        ):
            cancelled = asyncio.run(handler.on_cancel_task(CancelTaskRequest(id=submitted.id), context=None))

        assert cancelled.status.state == TaskState.TASK_STATE_CANCELED

    def test_on_cancel_task_rejects_a_persisted_task_without_mutating_its_memory_copy(
        self, integration_db, sample_tenant, sample_principal, context_manager
    ):
        """A persisted task cannot acquire a false in-memory cancellation state."""
        from a2a.types import CancelTaskRequest, Task, TaskStatus

        from src.a2a_server.adcp_a2a_server import AdCPRequestHandler

        tenant_id = sample_tenant["tenant_id"]
        principal_id = sample_principal["principal_id"]
        task_id = "task_persisted_cannot_cancel"
        _make_step(
            context_manager,
            tenant_id,
            principal_id,
            status="requires_approval",
            external_task_id=task_id,
        )
        handler = AdCPRequestHandler.__new__(AdCPRequestHandler)
        submitted = Task(id=task_id, status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED))
        handler.tasks = {task_id: submitted}
        handler._task_owners = {task_id: (tenant_id, principal_id)}
        owner = PrincipalFactory.make_identity(principal_id=principal_id, tenant_id=tenant_id, protocol="a2a")

        with (
            patch.object(handler, "_get_auth_token", return_value="tok"),
            patch.object(handler, "_resolve_a2a_identity", return_value=owner),
            pytest.raises(TaskNotFoundError),
        ):
            asyncio.run(handler.on_cancel_task(CancelTaskRequest(id=task_id), context=None))

        assert submitted.status.state == TaskState.TASK_STATE_SUBMITTED
        with WorkflowUoW(tenant_id) as uow:
            assert uow.workflows is not None
            persisted = uow.workflows.get_by_external_task_id(task_id, principal_id=principal_id)
            assert persisted is not None
            assert persisted.status == "requires_approval"

    def _poll_with_stale_in_memory(self, handler, tenant_id, principal_id, external_task_id):
        """Seed a SUBMITTED in-memory task OWNED by (tenant_id, principal_id), then poll on_get_task."""
        from a2a.types import Task, TaskStatus

        handler.tasks = {
            external_task_id: Task(id=external_task_id, status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED))
        }
        handler._task_owners = {external_task_id: (tenant_id, principal_id)}
        identity = PrincipalFactory.make_identity(principal_id=principal_id, tenant_id=tenant_id, protocol="a2a")
        with (
            patch.object(handler, "_get_auth_token", return_value="tok"),
            patch.object(handler, "_resolve_a2a_identity", return_value=identity),
        ):
            return asyncio.run(handler.on_get_task(GetTaskRequest(id=external_task_id), context=None))

    @pytest.mark.parametrize(
        "step_status, expected_state",
        [
            ("completed", TaskState.TASK_STATE_COMPLETED),
            ("rejected", TaskState.TASK_STATE_REJECTED),
            ("failed", TaskState.TASK_STATE_FAILED),
        ],
    )
    def test_stale_submitted_in_memory_yields_to_persisted_terminal(
        self, integration_db, sample_tenant, sample_principal, context_manager, step_status, expected_state
    ):
        """A stale in-memory SUBMITTED task must not mask a persisted terminal decision."""
        from src.a2a_server.adcp_a2a_server import AdCPRequestHandler

        tenant_id = sample_tenant["tenant_id"]
        _make_step(context_manager, tenant_id, sample_principal["principal_id"], status=step_status)

        handler = AdCPRequestHandler.__new__(AdCPRequestHandler)
        task = self._poll_with_stale_in_memory(handler, tenant_id, sample_principal["principal_id"], _EXTERNAL_TASK_ID)

        assert task is not None
        assert task.status.state == expected_state

    def test_stale_submitted_in_memory_kept_when_step_still_non_terminal(
        self, integration_db, sample_tenant, sample_principal, context_manager
    ):
        """When neither the in-memory task NOR the persisted step is terminal, the in-flight
        in-memory SUBMITTED task is returned (still working)."""
        from src.a2a_server.adcp_a2a_server import AdCPRequestHandler

        tenant_id = sample_tenant["tenant_id"]
        _make_step(context_manager, tenant_id, sample_principal["principal_id"], status="in_progress")

        handler = AdCPRequestHandler.__new__(AdCPRequestHandler)
        task = self._poll_with_stale_in_memory(handler, tenant_id, sample_principal["principal_id"], _EXTERNAL_TASK_ID)

        assert task is not None
        assert task.status.state == TaskState.TASK_STATE_SUBMITTED

    def test_approval_adapter_failure_stores_buyer_facing_error_artifact(
        self, integration_db, sample_tenant, sample_principal, context_manager
    ):
        """An adapter failure during async approval must store a two-layer
        error envelope on the step, so a durable tasks/get poll returns a FAILED task
        WITH the failure details — not a bare FAILED task with no artifact.

        SECURITY: the envelope served to the BUYER via durable
        on_get_task must carry a STABLE generic message, never the raw adapter
        ``str(e)`` — that text can embed internal state (here a fake secret). The
        raw text is retained ONLY in the internal ``error_message`` column. This
        test pins the wire code/recovery and asserts the raw text is absent from
        the buyer-facing artifact; removing the scrub reddens it."""
        from datetime import UTC, datetime

        from src.a2a_server.adcp_a2a_server import AdCPRequestHandler
        from src.core.database.repositories import MediaBuyUoW
        from src.services.media_buy_completion import FinalizeOutcome, finalize_media_buy_approval
        from tests.helpers import assert_envelope_shape
        from tests.integration.conftest import make_media_buy
        from tests.utils.a2a_helpers import extract_data_from_artifact

        # Raw adapter error carrying internal state that MUST NOT reach the buyer.
        raw_adapter_error = "GAM order creation failed: package_config secret_token=INTERNAL_LEAK_abc123 corrupted"
        stable_wire_message = "Adapter execution failed while creating the media buy"

        tenant_id = sample_tenant["tenant_id"]
        principal_id = sample_principal["principal_id"]
        external_task_id = "task_fail_xyz"

        with MediaBuyUoW(tenant_id) as uow:
            uow.media_buys.create(make_media_buy(tenant_id, principal_id, "mb_fail", status="pending_approval"))

        step = make_create_media_buy_step(
            context_manager,
            tenant_id,
            principal_id,
            media_buy_id="mb_fail",
            status="in_progress",
            external_task_id=external_task_id,
        )
        step_data = {
            "step_id": step.step_id,
            "context_id": step.context_id,
            "tool_name": "create_media_buy",
            "request_data": {"protocol": "a2a", "external_task_id": external_task_id},
        }

        with MediaBuyUoW(tenant_id) as uow:
            outcome, err = finalize_media_buy_approval(
                uow.session,
                tenant_id,
                media_buy_id="mb_fail",
                step_id=step.step_id,
                step_data=step_data,
                compute_target=lambda mb: "active",
                run_adapter=lambda: (False, raw_adapter_error),
                expected_status="pending_approval",
                approved_by="admin@test",
                approved_at=datetime.now(UTC),
            )

        assert outcome is FinalizeOutcome.ADAPTER_FAILED
        # The internal outcome/error channel preserves the raw adapter text.
        assert err == raw_adapter_error

        # The step is failed AND carries a buyer-facing two-layer error envelope.
        with WorkflowUoW(tenant_id) as uow:
            assert uow.workflows is not None
            failed = uow.workflows.get_by_step_id(step.step_id)
            assert failed is not None
            assert failed.status == "failed"
            # Raw text is retained ONLY in the internal error_message column.
            assert failed.error_message == raw_adapter_error
            # The buyer-facing envelope carries the stable message and NOT the raw text.
            assert failed.response_data is not None
            assert failed.response_data["errors"][0]["message"] == stable_wire_message
            assert "INTERNAL_LEAK_abc123" not in json.dumps(failed.response_data)
            # Correlation survives via structured details, not the raw message.
            assert failed.response_data["errors"][0]["details"]["media_buy_id"] == "mb_fail"

        # A durable poll returns FAILED *with* the error artifact (named as an error).
        handler = AdCPRequestHandler.__new__(AdCPRequestHandler)
        handler.tasks = {}
        identity = PrincipalFactory.make_identity(principal_id=principal_id, tenant_id=tenant_id, protocol="a2a")
        with (
            patch.object(handler, "_get_auth_token", return_value="tok"),
            patch.object(handler, "_resolve_a2a_identity", return_value=identity),
        ):
            task = asyncio.run(handler.on_get_task(GetTaskRequest(id=external_task_id), context=None))

        assert task is not None
        assert task.status.state == TaskState.TASK_STATE_FAILED
        assert len(task.artifacts) == 1
        assert task.artifacts[0].name == "media_buy_error"

        # Pin the ACTUAL wire (artifact DataPart): AdCPAdapterError -> SERVICE_UNAVAILABLE /
        # transient, stable message, and the raw internal text absent from the buyer artifact.
        wire_envelope = extract_data_from_artifact(task.artifacts[0])
        assert_envelope_shape(
            wire_envelope,
            "SERVICE_UNAVAILABLE",
            recovery="transient",
            message_substr=stable_wire_message,
        )
        assert "INTERNAL_LEAK_abc123" not in json.dumps(wire_envelope)
