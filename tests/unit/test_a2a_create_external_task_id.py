"""The A2A create_media_buy skill forwards the outer task ID to core.

on_message_send mints the outer ``task_*`` id and threads it down so
``_create_media_buy_impl`` can persist it on the workflow step. This pins the A2A-specific
hop — ``_handle_create_media_buy_skill(a2a_task_id=...)`` must forward it to core as
``external_task_id`` — so a refactor that drops it turns this test red.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.schemas import CreateMediaBuyResult, CreateMediaBuySubmitted
from tests.factories import PrincipalFactory

_MOCK_IDENTITY = PrincipalFactory.make_identity(principal_id="principal_123", tenant_id="tenant_123", protocol="a2a")

_VALID_PARAMS = {
    "brand": {"domain": "example.com"},
    "packages": [{"package_id": "pkg_1", "products": ["prod_1"], "budget": {"total": 1000, "currency": "USD"}}],
    "start_time": "2026-01-01T00:00:00Z",
    "end_time": "2026-02-01T00:00:00Z",
}


def _external_task_id_forwarded_to_core(a2a_task_id: str | None) -> object:
    """Invoke the real create skill handler and return the ``external_task_id`` handed to core."""
    from src.a2a_server.adcp_a2a_server import AdCPRequestHandler

    handler = AdCPRequestHandler.__new__(AdCPRequestHandler)
    captured: dict[str, object] = {}

    async def _fake_core(**kwargs: object) -> MagicMock:
        captured["external_task_id"] = kwargs.get("external_task_id")
        result = MagicMock()
        result.model_dump.return_value = {}
        return result

    # Bypass request-body validation — the unit under test is the external_task_id
    # forwarding, and core (which consumes the params) is mocked.
    with (
        patch.object(handler, "_make_tool_context", return_value=MagicMock()),
        patch("src.core.schemas.CreateMediaBuyRequest.model_validate", return_value=MagicMock(po_number=None)),
        patch("src.a2a_server.adcp_a2a_server.core_create_media_buy_tool", new=AsyncMock(side_effect=_fake_core)),
    ):
        asyncio.run(
            handler._handle_create_media_buy_skill(
                parameters={**_VALID_PARAMS},
                identity=_MOCK_IDENTITY,
                a2a_task_id=a2a_task_id,
            )
        )
    return captured["external_task_id"]


class TestA2ACreateExternalTaskId:
    def test_outer_task_id_forwarded_as_external_task_id(self):
        """The A2A skill hands its outer task id to core as external_task_id."""
        assert _external_task_id_forwarded_to_core("task_outer_xyz") == "task_outer_xyz"

    def test_absent_task_id_forwards_none(self):
        """A direct handler call without an outer task id forwards None (MCP/REST parity)."""
        assert _external_task_id_forwarded_to_core(None) is None

    def test_submitted_replay_registers_outer_task_as_durable_alias(self):
        """A retry's new A2A task ID remains a durable handle for the original step."""
        from src.core.tools.media_buy_create import _register_replayed_a2a_task_alias

        response = CreateMediaBuyResult(response=CreateMediaBuySubmitted(task_id="step_original"), status="submitted")
        uow = MagicMock()
        uow.__enter__.return_value = uow
        uow.workflows.register_external_task_alias.return_value = True

        with patch("src.core.database.repositories.WorkflowUoW", return_value=uow):
            _register_replayed_a2a_task_alias("tenant_123", "principal_123", response, "task_retry")

        uow.workflows.register_external_task_alias.assert_called_once_with(
            "step_original", "task_retry", principal_id="principal_123"
        )

    def test_idempotency_race_replay_registers_outer_task_as_durable_alias(self):
        """The uniqueness-race replay path applies the same A2A alias rule as a cache hit."""
        from sqlalchemy.exc import IntegrityError

        from src.core.tools.media_buy_create import _resolve_idempotency_race_or_raise

        response = CreateMediaBuyResult(response=CreateMediaBuySubmitted(task_id="step_original"), status="submitted")
        uow = MagicMock()
        uow.__enter__.return_value = uow
        uow.workflows.register_external_task_alias.return_value = True
        error = IntegrityError("insert", {}, Exception("idempotency_key"))

        with (
            patch("src.core.tools.media_buy_create._is_idempotency_backstop_violation", return_value=True),
            patch("src.core.tools.media_buy_create._replay_after_race", return_value=response),
            patch("src.core.database.repositories.WorkflowUoW", return_value=uow),
        ):
            result = _resolve_idempotency_race_or_raise(
                error,
                "tenant_123",
                idempotency_key="key_123",
                principal_id="principal_123",
                account_id=None,
                request_hash="hash_123",
                external_task_id="task_retry",
            )

        assert result is response
        uow.workflows.register_external_task_alias.assert_called_once_with(
            "step_original", "task_retry", principal_id="principal_123"
        )
