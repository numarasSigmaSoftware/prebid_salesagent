"""Nested repository reads must not close or remove an owning UoW session."""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.core.database.database_session import (
    DatabaseManager,
    _discard_after_commit_callbacks,
    _publish_after_root_transaction_end,
    _run_after_commit_callbacks,
    defer_until_after_commit,
    get_coordination_engine,
    get_db_session,
    get_independent_db_session,
    reset_engine,
)
from src.core.database.repositories.uow import MediaBuyUoW, ProductUoW


@pytest.fixture(autouse=True)
def use_database_session_implementations_in_uow():
    """This module exercises the real nesting logic, not the unit DB guard."""
    with (
        patch("src.core.database.repositories.uow.get_db_session", get_db_session),
        patch(
            "src.core.database.repositories.uow.get_independent_db_session",
            get_independent_db_session,
        ),
    ):
        yield


def test_nested_context_reuses_session_and_only_outer_owner_closes_it() -> None:
    session = MagicMock()
    scoped = MagicMock(return_value=session)

    with patch("src.core.database.database_session.get_scoped_session", return_value=scoped):
        with get_db_session() as outer:
            with get_db_session() as inner:
                assert inner is outer
            session.close.assert_not_called()
            scoped.remove.assert_not_called()

    session.close.assert_called_once_with()
    scoped.remove.assert_called_once_with()


def test_uow_inside_raw_session_still_owns_and_commits_transaction() -> None:
    """A fixture/session scope is not itself an atomic UoW boundary."""
    session = MagicMock()
    scoped = MagicMock(return_value=session)

    with patch("src.core.database.database_session.get_scoped_session", return_value=scoped):
        with get_db_session():
            with MediaBuyUoW("tenant"):
                pass

    session.commit.assert_called_once_with()


def test_nested_uows_commit_only_at_outer_uow_boundary() -> None:
    session = MagicMock()
    scoped = MagicMock(return_value=session)

    with patch("src.core.database.database_session.get_scoped_session", return_value=scoped):
        with MediaBuyUoW("tenant"):
            with ProductUoW("tenant"):
                pass
            session.commit.assert_not_called()

    session.commit.assert_called_once_with()


def test_database_manager_joins_outer_uow_transaction() -> None:
    session = MagicMock()
    scoped = MagicMock(return_value=session)

    with patch("src.core.database.database_session.get_scoped_session", return_value=scoped):
        with MediaBuyUoW("tenant"):
            manager = DatabaseManager()
            manager.session.add(object())
            manager.commit()
            session.flush.assert_called_once_with()
            session.commit.assert_not_called()

    session.commit.assert_called_once_with()


def test_nested_commit_does_not_publish_root_callback() -> None:
    session = MagicMock()
    callback = MagicMock()
    session.info = {}
    defer_until_after_commit(session, callback)
    session.in_nested_transaction.return_value = True

    _run_after_commit_callbacks(session)

    callback.assert_not_called()
    session.in_nested_transaction.return_value = False
    _run_after_commit_callbacks(session)
    _publish_after_root_transaction_end(session, SimpleNamespace(parent=None))
    callback.assert_called_once_with()


def test_nested_rollback_preserves_prior_callback_for_root_commit() -> None:
    session = MagicMock()
    callback = MagicMock()
    session.info = {}
    defer_until_after_commit(session, callback)
    session.in_nested_transaction.return_value = True

    _discard_after_commit_callbacks(session)

    session.in_nested_transaction.return_value = False
    _run_after_commit_callbacks(session)
    _publish_after_root_transaction_end(session, SimpleNamespace(parent=None))
    callback.assert_called_once_with()


def test_root_rollback_discards_deferred_callback() -> None:
    session = MagicMock()
    callback = MagicMock()
    session.info = {}
    defer_until_after_commit(session, callback)
    session.in_nested_transaction.return_value = False

    _discard_after_commit_callbacks(session)
    _run_after_commit_callbacks(session)
    _publish_after_root_transaction_end(session, SimpleNamespace(parent=None))

    callback.assert_not_called()


def test_callback_runs_only_after_root_transaction_releases_connection() -> None:
    session = MagicMock()
    callback = MagicMock()
    transaction = SimpleNamespace(parent=None)
    session.info = {}
    session.in_nested_transaction.return_value = False
    defer_until_after_commit(session, callback)

    _run_after_commit_callbacks(session)
    callback.assert_not_called()
    _publish_after_root_transaction_end(session, transaction)

    callback.assert_called_once_with()


def test_coordination_engine_rejects_pgbouncer_fallback() -> None:
    reset_engine()
    with (
        patch.dict(os.environ, {"USE_PGBOUNCER": "true"}, clear=False),
        patch.dict(os.environ, {}, clear=False),
        patch(
            "src.core.database.database_session.DatabaseConfig.get_connection_string",
            return_value="postgresql://user:secret@pooler:6543/app",
        ),
    ):
        os.environ.pop("COORDINATION_DATABASE_URL", None)
        with pytest.raises(RuntimeError, match="must point directly to PostgreSQL"):
            get_coordination_engine()
    reset_engine()


def test_coordination_engine_uses_explicit_direct_url() -> None:
    reset_engine()
    engine = MagicMock()
    with (
        patch.dict(
            os.environ,
            {
                "USE_PGBOUNCER": "true",
                "COORDINATION_DATABASE_URL": "postgresql://user:secret@postgres:5432/app",
            },
            clear=False,
        ),
        patch(
            "src.core.database.database_session.DatabaseConfig.get_connection_string",
            return_value="postgresql://user:secret@pooler:6543/app",
        ),
        patch("src.core.database.database_session.create_engine", return_value=engine) as create_engine_mock,
        patch("src.core.database.database_session._install_statement_timeout"),
    ):
        assert get_coordination_engine() is engine

    assert create_engine_mock.call_args.args == ("postgresql://user:secret@postgres:5432/app",)
    reset_engine()
