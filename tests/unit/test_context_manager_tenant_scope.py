"""Persistent contexts are always resolved within tenant and principal scope."""

from unittest.mock import MagicMock, patch

from src.core.context_manager import ContextManager


def test_get_context_forwards_tenant_and_principal_scope() -> None:
    manager = ContextManager()
    session = MagicMock()
    context = MagicMock()
    manager._session = session
    manager._owns_session = False

    with patch("src.core.context_manager.WorkflowRepository") as repository_type:
        repository_type.return_value.get_context.return_value = context
        result = manager.get_context(
            "ctx-shared",
            tenant_id="tenant-a",
            principal_id="principal-a",
        )

    assert result is context
    repository_type.assert_called_once_with(session, "tenant-a")
    repository_type.return_value.get_context.assert_called_once_with(
        "ctx-shared",
        principal_id="principal-a",
    )
    session.expunge.assert_called_once_with(context)
    assert manager._session is None


def test_get_or_create_does_not_fall_back_when_scoped_context_is_missing() -> None:
    manager = ContextManager()
    with (
        patch.object(manager, "get_context", return_value=None) as get_context,
        patch.object(manager, "create_context") as create_context,
    ):
        result = manager.get_or_create_context(
            "tenant-b",
            "principal-b",
            context_id="ctx-foreign",
            is_async=True,
        )

    assert result is None
    get_context.assert_called_once_with(
        "ctx-foreign",
        tenant_id="tenant-b",
        principal_id="principal-b",
    )
    create_context.assert_not_called()
