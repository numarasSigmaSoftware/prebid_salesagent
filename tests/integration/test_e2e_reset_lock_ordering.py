"""The e2e reset and the delivery scheduler must not deadlock each other.

``_reset_e2e_db`` (tests/bdd/conftest.py) TRUNCATEs every table before each
e2e_rest scenario, needing AccessExclusiveLock. The delivery-webhook scheduler
runs concurrently on the live server and its selection query reads ``media_buys``
then ``webhook_delivery_log`` (the correlated anti-join over successful ``final``
sends). The TRUNCATE list came from ``pg_tables`` in catalog order, so the two
sides could take the same locks in opposite orders and Postgres killed one:

    A waits for AccessShareLock     on webhook_delivery_log; blocked by B
    B waits for AccessExclusiveLock on media_buys;           blocked by A

That failed roughly half of in-network CI runs on a different scenario each time,
and read as unexplained flakiness for several review rounds — two wrong causes
were published and withdrawn — until a server-log dump captured DeadlockDetected.

Scoping ``DELIVERY_WEBHOOK_INTERVAL`` keeps the scheduler out of legs that do not
grade it, but it cannot make ``./run_all_tests.sh ci`` safe: that runs the ``e2e``
suite, which REQUIRES a ticking scheduler, alongside bdd_e2e in one stack. Lock
ordering is what makes them coexist, so it is what this grades.

Two real connections racing on a ``threading.Barrier``, not a mocked scenario:
lock ordering is a property of concurrent transactions and cannot be observed
sequentially. The inverted case is asserted too — a test that only shows the
fixed order passing cannot distinguish "no deadlock" from "no contention".
"""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import create_engine, text

from src.core.database.database_session import get_engine

_A, _B = "media_buys", "webhook_delivery_log"
# Bounded so an unexpected hang fails the test instead of stalling the suite.
_TIMEOUT_MS = 4000


def _db_url() -> str:
    """The live test database URL, via the engine rather than a session.

    ``get_db_session()`` is barred from test bodies by
    test_architecture_repository_pattern -- rightly, since that guard exists to stop
    tests hand-rolling data setup. This needs no session at all, only the URL to
    open two INDEPENDENT connections with: the whole point is that a single session
    cannot observe a lock conflict with itself.
    """
    return str(get_engine().url.render_as_string(hide_password=False))


def _race(db_url: str, *, reset_order: list[str]) -> list[str]:
    """Run the scheduler's read against a reset holding locks in ``reset_order``.

    Returns the error class names seen. A deadlock surfaces as OperationalError
    (psycopg2.errors.DeadlockDetected) on whichever side Postgres chooses to kill.
    """
    barrier = threading.Barrier(2, timeout=15)
    errors: list[str] = []

    def scheduler() -> None:
        engine = create_engine(db_url)
        try:
            with engine.begin() as conn:
                conn.execute(text(f"SET LOCAL lock_timeout = '{_TIMEOUT_MS}ms'"))
                # Hold media_buys BEFORE releasing the reset, so the reset always
                # starts its own locking against a held first table. Releasing the
                # barrier earlier makes the reset block before it can reach the
                # barrier at all, which manufactures a stall in both orderings and
                # tells you nothing about either.
                conn.execute(text(f'SELECT 1 FROM "{_A}" LIMIT 1 FOR SHARE'))
                barrier.wait()
                time.sleep(0.3)
                conn.execute(text(f'SELECT count(*) FROM "{_B}"'))
        except Exception as exc:  # noqa: BLE001 - the class name IS the assertion
            errors.append(type(exc).__name__)
        finally:
            engine.dispose()

    def reset() -> None:
        engine = create_engine(db_url)
        try:
            barrier.wait()
            with engine.begin() as conn:
                conn.execute(text(f"SET LOCAL lock_timeout = '{_TIMEOUT_MS}ms'"))
                for table in reset_order:
                    conn.execute(text(f'LOCK TABLE "{table}" IN ACCESS EXCLUSIVE MODE'))
        except Exception as exc:  # noqa: BLE001
            errors.append(type(exc).__name__)
        finally:
            engine.dispose()

    threads = [threading.Thread(target=scheduler), threading.Thread(target=reset)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "a thread never finished — the lock_timeout did not fire"
    return errors


@pytest.mark.requires_db
class TestE2eResetDoesNotDeadlockTheScheduler:
    def test_inverted_order_deadlocks(self, integration_db):
        """The control. Without this the 'fixed' case proves only absence of contention."""
        url = _db_url()

        errors = _race(url, reset_order=[_B, _A])
        assert errors, (
            "taking the locks in the opposite order to the scheduler produced no error at all — "
            "the race is no longer being set up, so the fixed case below proves nothing"
        )

    def test_scheduler_order_does_not_deadlock(self, integration_db):
        """``media_buys`` before ``webhook_delivery_log`` — the order _reset_e2e_db uses.

        Whichever side wins media_buys runs to completion and the other waits: that
        is contention, which is fine, rather than a cycle, which is fatal.
        """
        url = _db_url()

        errors = _race(url, reset_order=[_A, _B])
        assert not errors, (
            f"acquiring in the scheduler's own order still errored ({errors}) — the reset in "
            "tests/bdd/conftest.py::_reset_e2e_db orders its LOCK TABLE to prevent exactly this"
        )

    def test_the_reset_locks_the_contended_tables_first(self):
        """The behavioural tests above race a REPLICA of the reset's ordering, so this
        pins that the reset itself still uses it — the two halves together are what
        make removing the LOCK statement fail.
        """
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "bdd" / "conftest.py").read_text()
        assert 'ordered_first = [t for t in ("media_buys", "webhook_delivery_log") if t in tables]' in source, (
            "_reset_e2e_db no longer locks media_buys before webhook_delivery_log; the deadlock "
            "the tests above describe is reachable again"
        )
        assert "LOCK TABLE" in source, "_reset_e2e_db no longer takes explicit locks before TRUNCATE"
