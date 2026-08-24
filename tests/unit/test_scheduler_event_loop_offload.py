"""The status sweep must execute off the server's event-loop thread.

``MediaBuyStatusScheduler._update_statuses`` is fully synchronous — repository
query, per-row ``_compute_new_status``, ``session.commit()`` — and the scheduler
loop is started into the MCP server's lifespan loop (``src/core/main.py``). It
was previously declared ``async def`` with zero awaits, which meant every sweep
ran inline on that loop and blocked every other request for its duration.
``_run_scheduler`` now hands it to ``asyncio.to_thread``.

Nothing enforced that. A source-level check ("the file contains ``to_thread``")
would not: ``_update_statuses`` was ALREADY ``async def`` with zero awaits and
looked correct to any shape check, which is precisely how the original defect
survived. So this test pins the RUNTIME fact instead — it drives the real
``_run_scheduler`` and records which thread the sweep body actually executed on.

What this proves: the sweep leaves the event-loop thread.
What it does NOT prove: anything about loop latency, throughput, or how long a
sweep takes. It is a threading-boundary oracle, not a performance guarantee —
do not read a green result here as evidence that the scheduler is fast, or that
the loop stayed responsive under load.

The sweep body is replaced with a spy, so no database is touched and this runs
offline in ``make quality``.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading

import pytest

from src.services.media_buy_status_scheduler import MediaBuyStatusScheduler, StatusSweepSummary

# Generous: the assertion is on WHICH thread ran, never on how fast. The test
# waits on an Event the spy sets, never on a bare sleep, so a slow machine
# costs nothing and cannot flip the outcome.
_SWEEP_TIMEOUT_SECONDS = 10

# Teardown only: bounded so a cancel that never lands fails loudly on the outer
# pytest timeout instead of spinning here forever.
_CANCEL_ATTEMPTS = 20
_CANCEL_WAIT_SECONDS = 0.5


@pytest.mark.asyncio
async def test_status_sweep_body_runs_on_a_worker_thread_not_the_event_loop():
    """Driving the real ``_run_scheduler`` must execute the sweep off-loop.

    Reverting the call site to a direct ``self._update_statuses()`` makes the
    spy record the event-loop thread's own ident and reddens the assertion
    below — that mutation is what makes this an oracle rather than decoration.
    """
    loop = asyncio.get_running_loop()
    event_loop_thread_ident = threading.get_ident()

    swept_on: dict[str, int] = {}
    swept = asyncio.Event()

    def _spy_sweep() -> StatusSweepSummary:
        # Record the executing thread, then wake the test. ``call_soon_threadsafe``
        # is the only sanctioned way to touch an asyncio.Event from another
        # thread; it is also correct when called FROM the loop thread, so the
        # reverted-call-site mutation still wakes the test and fails on the
        # assertion rather than hanging on the timeout.
        swept_on["ident"] = threading.get_ident()
        loop.call_soon_threadsafe(swept.set)
        return StatusSweepSummary()

    scheduler = MediaBuyStatusScheduler()
    # Replace the sweep body, not the offload: _run_scheduler's real call site is
    # what is under test, so it must run unmodified.
    scheduler._update_statuses = _spy_sweep  # type: ignore[method-assign]
    scheduler.is_running = True

    task = asyncio.create_task(scheduler._run_scheduler())
    try:
        await asyncio.wait_for(swept.wait(), timeout=_SWEEP_TIMEOUT_SECONDS)
    finally:
        scheduler.is_running = False
        # Cancel in a bounded loop rather than once. _run_scheduler CATCHES
        # CancelledError to break its loop, and its `finally` then starts a FRESH
        # `await asyncio.sleep(STATUS_CHECK_INTERVAL_SECONDS)` that the original
        # cancellation no longer applies to — so a single cancel delivered while
        # the sweep is in flight leaves the task sleeping out a whole 60s interval
        # before it exits. (That is a real shutdown wart in production, reported
        # separately; it is not this test's to fix, but this teardown must not
        # inherit it.) The second cancel lands on that sleep and ends the task.
        for _ in range(_CANCEL_ATTEMPTS):
            if task.done():
                break
            task.cancel()
            await asyncio.wait({task}, timeout=_CANCEL_WAIT_SECONDS)
        with contextlib.suppress(asyncio.CancelledError):
            if task.done():
                task.exception()

    assert swept_on["ident"] != event_loop_thread_ident, (
        "the status sweep executed on the event-loop thread "
        f"(ident {event_loop_thread_ident}) — _run_scheduler is calling "
        "_update_statuses inline again, so every sweep blocks the server for its "
        "whole duration. It must go through asyncio.to_thread."
    )
