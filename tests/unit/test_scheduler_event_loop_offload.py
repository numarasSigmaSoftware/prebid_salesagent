"""Runtime invariants of the two background scheduler loops.

Two things are pinned here, both by driving the REAL ``_run_scheduler`` rather
than by inspecting source:

1. The status sweep executes off the server's event-loop thread.
2. Cancelling either scheduler ends its task promptly, instead of sleeping out
   a full cadence interval first.

Both were previously enforced by nothing.

---- 1. The offload ----

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

---- 2. Prompt cancellation ----

Both loops used to catch ``asyncio.CancelledError`` to ``break``, which CONSUMED
the cancellation, and then start a FRESH ``await asyncio.sleep(INTERVAL)`` in a
``finally`` that the original cancellation no longer applied to. Since both
``stop()`` methods await the task, shutdown blocked for up to one interval — 60s
for the status scheduler, 3600s for the delivery scheduler.

For the delivery scheduler that was always reachable (``_send_reports`` awaits,
so its try body always suspended). For the status scheduler it was latent: an
``async def`` with zero awaits never suspends, so ``cancel()`` only set the
pending flag and the ``CancelledError`` surfaced at the ``finally``'s sleep,
outside the ``try``'s guard, and propagated at once. Offloading the sweep to a
thread made that ``await`` a genuine suspension point, which moved the
cancellation inside the ``try`` and made the swallow reachable.

Both spies replace the work body, so no database is touched and this whole
module runs offline in ``make quality``.
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
        # A single cancel, awaited plainly. This deliberately has no retry loop:
        # the loop that used to be here masked the cancellation-swallow bug, so if
        # that bug returns this hangs until the pytest timeout rather than quietly
        # staying fast. The prompt-exit contract itself is graded below.
        scheduler.is_running = False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert swept_on["ident"] != event_loop_thread_ident, (
        "the status sweep executed on the event-loop thread "
        f"(ident {event_loop_thread_ident}) — _run_scheduler is calling "
        "_update_statuses inline again, so every sweep blocks the server for its "
        "whole duration. It must go through asyncio.to_thread."
    )


# ---------------------------------------------------------------------------
# Invariant 2: cancelling a scheduler ends its task promptly.
#
# Both tests patch the cadence interval down and assert cancel-to-exit lands
# well inside ONE interval. The threshold is a fraction of the patched interval
# rather than an absolute wall-clock budget: the failure being graded is
# "slept out a whole interval", which is a factor-of-N miss, not a few
# milliseconds. That keeps the assertion immune to a slow machine.
# ---------------------------------------------------------------------------

# Short enough that a regression costs the suite seconds, not minutes; long
# enough that ordinary scheduling jitter cannot reach the threshold below.
_PATCHED_INTERVAL_SECONDS = 5.0
# Exceeding this means the cancel did not end the task — it waited on a sleep.
_PROMPT_EXIT_SECONDS = _PATCHED_INTERVAL_SECONDS / 5


async def _cancel_and_time_exit(task: asyncio.Task, loop: asyncio.AbstractEventLoop) -> float:
    """Cancel *task* once and return how long it took to finish, in seconds.

    One cancel, one await — the shape ``stop()`` itself uses. No retry loop: a
    scheduler that swallows its cancellation must show up as a long elapsed
    time here, not be papered over by a second cancel.
    """
    started = loop.time()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return loop.time() - started


def _assert_prompt_exit(elapsed: float, scheduler_name: str) -> None:
    assert elapsed < _PROMPT_EXIT_SECONDS, (
        f"{scheduler_name} took {elapsed:.2f}s to exit after cancel, with the cadence "
        f"interval patched to {_PATCHED_INTERVAL_SECONDS}s. It is swallowing its own "
        f"CancelledError and then sleeping out an interval — stop() awaits this task, "
        f"so shutdown blocks for that long. Let CancelledError propagate and keep the "
        f"sleep a cancellable await in the loop body."
    )


@pytest.mark.asyncio
async def test_status_scheduler_exits_promptly_when_cancelled_mid_sweep(monkeypatch):
    """Cancelling during an in-flight sweep must end the task, not start a sleep.

    The cancel is deliberately delivered while the sweep is executing on the
    worker thread, so the CancelledError lands INSIDE the try — the arm the old
    ``except asyncio.CancelledError: break`` consumed. Before the fix this
    returned one full interval; after it, effectively zero.
    """
    monkeypatch.setattr(
        "src.services.media_buy_status_scheduler.STATUS_CHECK_INTERVAL_SECONDS",
        _PATCHED_INTERVAL_SECONDS,
    )
    loop = asyncio.get_running_loop()
    sweeping = asyncio.Event()
    release = threading.Event()

    def _spy_sweep() -> StatusSweepSummary:
        # Announce that the sweep is in flight, then hold the worker thread until
        # the test has issued the cancel, so the cancellation cannot arrive early
        # and land on the sleep instead. Bounded so a failure cannot wedge the run.
        loop.call_soon_threadsafe(sweeping.set)
        release.wait(timeout=_SWEEP_TIMEOUT_SECONDS)
        return StatusSweepSummary()

    scheduler = MediaBuyStatusScheduler()
    scheduler._update_statuses = _spy_sweep  # type: ignore[method-assign]
    scheduler.is_running = True

    task = asyncio.create_task(scheduler._run_scheduler())
    try:
        await asyncio.wait_for(sweeping.wait(), timeout=_SWEEP_TIMEOUT_SECONDS)
        scheduler.is_running = False
        elapsed = await _cancel_and_time_exit(task, loop)
    finally:
        release.set()

    _assert_prompt_exit(elapsed, "MediaBuyStatusScheduler")


@pytest.mark.asyncio
async def test_delivery_scheduler_exits_promptly_when_cancelled_mid_batch(monkeypatch):
    """The identical contract on the delivery scheduler's hourly loop.

    Its try body has always suspended (``_send_reports`` awaits), so the swallow
    was reachable here before the status scheduler's offload existed — an
    unpatched interval means shutdown could block for a full hour.
    """
    from src.services.delivery_webhook_scheduler import DeliveryBatchSummary, DeliveryWebhookScheduler

    monkeypatch.setattr(
        "src.services.delivery_webhook_scheduler.SLEEP_INTERVAL_SECONDS",
        _PATCHED_INTERVAL_SECONDS,
    )
    loop = asyncio.get_running_loop()
    batching = asyncio.Event()
    hold = asyncio.Event()  # never set — the cancel is what interrupts this await

    async def _spy_send_reports() -> DeliveryBatchSummary:
        batching.set()
        await hold.wait()
        return DeliveryBatchSummary()

    scheduler = DeliveryWebhookScheduler()
    scheduler._send_reports = _spy_send_reports  # type: ignore[method-assign]
    scheduler.is_running = True

    task = asyncio.create_task(scheduler._run_scheduler())
    await asyncio.wait_for(batching.wait(), timeout=_SWEEP_TIMEOUT_SECONDS)
    scheduler.is_running = False
    elapsed = await _cancel_and_time_exit(task, loop)

    _assert_prompt_exit(elapsed, "DeliveryWebhookScheduler")
