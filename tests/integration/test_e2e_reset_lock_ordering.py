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

import os
import threading
import time

import pytest
from sqlalchemy import create_engine, text

_A, _B = "media_buys", "webhook_delivery_log"
# Bounded so an unexpected hang fails the test instead of stalling the suite.
_TIMEOUT_MS = 4000


DEADLOCK = "40P01"  # psycopg2.errors.DeadlockDetected
LOCK_TIMEOUT = "55P03"  # psycopg2.errors.LockNotAvailable


def _pgcode(exc: BaseException) -> str:
    """The SQLSTATE, not the exception class.

    Both a deadlock and a lock_timeout surface as ``OperationalError``, so asserting
    on the class name cannot tell "Postgres detected a cycle" from "we simply waited
    too long" -- the no-deadlock/no-contention conflation this module's docstring
    claims to have eliminated, reintroduced one layer down in its own control.
    """
    return getattr(getattr(exc, "orig", None), "pgcode", None) or type(exc).__name__


def _db_url() -> str:
    """The URL of THIS test's database, from the env var ``integration_db`` sets.

    Not ``get_engine()``: that returns a process-cached engine bound to whatever
    DATABASE_URL was current at first use, which is the bare ``adcp_test`` database
    with no schema — while ``integration_db`` creates a UNIQUE database per test and
    publishes it here. The first version of this module read the cached engine and
    still passed, because an earlier test in the same file had populated the bare
    database by the time these ran. Alone, the same tests raised 42P01
    (UndefinedTable) and the inverted control's 40P01 assertion failed.

    That is the order-dependence this suite has elsewhere (#1931), reproduced inside
    a test written to grade concurrency: it was green for a reason that had nothing
    to do with lock ordering.

    Not ``get_db_session()`` either — barred from test bodies by
    test_architecture_repository_pattern, and no session is wanted here: a single
    session cannot deadlock against itself, so this needs a URL to open two
    INDEPENDENT connections with.
    """
    url = os.environ.get("DATABASE_URL")
    assert url and url.startswith("postgresql://"), (
        f"DATABASE_URL is {url!r}; integration_db sets it to this test's own database, and "
        "racing against any other one grades nothing"
    )
    return url


def _race(db_url: str, *, reset_order: list[str], reader: tuple[str, str] = (_A, _B)) -> list[str]:
    """Race a scheduler-shaped read against a reset holding locks in ``reset_order``.

    ``reader`` is the (first, second) table pair the simulated scheduler transaction
    touches, defaulting to the delivery scheduler's. It is a parameter because the
    two background loops have DIFFERENT shapes and only one of them can show what the
    explicit LOCK buys — hardcoding the delivery pair made a status-scheduler test
    silently race the wrong transaction.

    Returns the error class names seen. A deadlock surfaces as OperationalError
    (psycopg2.errors.DeadlockDetected) on whichever side Postgres chooses to kill.
    """
    # Fail loudly if a table is missing rather than reporting 42P01 as if it were a
    # lock outcome: an UndefinedTable makes both orderings "error", which reads as a
    # deadlock to a control that only checks for errors.
    with create_engine(db_url).begin() as probe:
        present = {r[0] for r in probe.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))}
    missing = sorted({*reset_order, *reader} - present)
    assert not missing, f"race tables absent from {db_url.rsplit('/', 1)[-1]}: {missing}"

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
                conn.execute(text(f'SELECT 1 FROM "{reader[0]}" LIMIT 1 FOR SHARE'))
                barrier.wait()
                time.sleep(0.3)
                conn.execute(text(f'SELECT count(*) FROM "{reader[1]}"'))
        except Exception as exc:  # noqa: BLE001 - the SQLSTATE is the assertion
            errors.append(f"reader:{_pgcode(exc)}")
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
            errors.append(f"reset:{_pgcode(exc)}")
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
        assert any(e.endswith(DEADLOCK) for e in errors), (
            f"inverting the order produced {errors or 'no error'}, not a {DEADLOCK} deadlock. A "
            f"{LOCK_TIMEOUT} lock_timeout here would mean the race degenerated into plain waiting, "
            "which would make the fixed case below prove nothing"
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

    def test_the_status_scheduler_shape_is_what_the_hoist_protects(self, integration_db):
        """media_buys -> creatives: the shape the pairwise ordering does NOT cover.

        The delivery scheduler's pair (media_buys, webhook_delivery_log) is already
        ordered by ``sorted()`` alone, so that pair cannot show what the explicit LOCK
        buys. The status scheduler is where it shows: it starts at media_buys and then
        reads creative tables, and "creatives" sorts BEFORE "media_buys". Under a
        sorted-only reset the two orders invert.

        Measured over 10 races: 10 deadlocks (40P01) sorted-only, 0 with the hoist.
        Both directions asserted, because the passing half alone cannot distinguish
        "the hoist works" from "these two never contend".
        """
        url = _db_url()
        inverted = _race(url, reset_order=["creatives", _A], reader=(_A, "creatives"))
        assert any(e.endswith(DEADLOCK) for e in inverted), (
            f"a sorted-only reset (creatives before media_buys) against the status scheduler's "
            f"shape produced {inverted or 'no error'}, not {DEADLOCK} — if these no longer contend, "
            "the hoist below is being credited for something it is not doing"
        )

        hoisted = _race(url, reset_order=[_A, "creatives"], reader=(_A, "creatives"))
        assert not hoisted, (
            f"locking media_buys first still errored ({hoisted}) — this ordering is the reason "
            "_reset_e2e_db hoists media_buys ahead of the sorted list"
        )

    def test_every_background_loop_still_starts_at_media_buys(self):
        """The invariant the hoist depends on, and its accepted cost.

        Hoisting media_buys ahead of the ~20 tables that sort before it inverts
        against anything touching one of those and THEN media_buys — measured with an
        audit_logs -> media_buys reader: 10 deadlocks with the hoist, 0 without. That
        is safe ONLY because every server-side transaction begins at media_buys.

        So the invariant is a property of the background loops, and this pins the set
        of them. A third loop is not necessarily wrong — but it must start at
        media_buys, or the reset's ordering has to be reconsidered, and that decision
        should not be made silently by adding a start call.
        """
        from pathlib import Path

        main_py = (Path(__file__).resolve().parents[2] / "src" / "core" / "main.py").read_text()
        started = sorted(
            line.split("await ")[1].split("(")[0].strip()
            for line in main_py.splitlines()
            if "await start_" in line and "scheduler" in line
        )
        assert started == ["start_delivery_webhook_scheduler", "start_media_buy_status_scheduler"], (
            f"the set of background scheduler loops changed to {started}. Each must begin its "
            "transaction at media_buys — _reset_e2e_db hoists that table first on exactly that "
            "assumption, and a loop starting elsewhere reintroduces the deadlock through the "
            "inversion class the hoist creates (see tests/bdd/conftest.py::_reset_e2e_db)."
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
