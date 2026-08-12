"""Cross-event-loop admission guarantees for shared blocking executors."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import patch

import pytest

from src.core.bounded_executor import AsyncThreadPoolBulkhead


def test_async_bulkhead_capacity_is_global_across_sequential_event_loops() -> None:
    """Repeated ``asyncio.run`` callers cannot each enqueue one timed-out job."""
    bulkhead = AsyncThreadPoolBulkhead(max_workers=1, thread_name_prefix="bulkhead-cross-loop-test")
    release_worker = threading.Event()
    worker_started = threading.Event()
    worker_finished = threading.Event()

    def _blocked_work() -> None:
        worker_started.set()
        release_worker.wait(timeout=1)
        worker_finished.set()

    async def _time_out_one_call() -> None:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(bulkhead.run(_blocked_work), timeout=0.02)

    original_submit = bulkhead.submit
    try:
        with patch.object(bulkhead, "submit", wraps=original_submit) as submit:
            asyncio.run(_time_out_one_call())
            assert worker_started.wait(timeout=0.2)

            # Each call creates and closes a new event loop, matching synchronous
            # Flask/admin request paths. A loop-local limiter would submit seven
            # more futures into the one-worker executor's queue here.
            for _ in range(7):
                asyncio.run(_time_out_one_call())

            assert submit.call_count == 1

        release_worker.set()
        assert worker_finished.wait(timeout=0.2)

        async def _recovered_call() -> str:
            return await asyncio.wait_for(bulkhead.run(lambda: "recovered"), timeout=0.2)

        assert asyncio.run(_recovered_call()) == "recovered"
    finally:
        release_worker.set()
        bulkhead._executor.shutdown(wait=True)


class TestAdmissionGateOverReleaseIsObservable:
    """The over-release guard must be reachable AND leave capacity bounded.

    It used to `raise ValueError` from inside a Future done-callback, where
    CPython swallows the exception into the concurrent.futures logger — a
    fail-loud guard that could never reach a caller, and read like one. It now
    logs at ERROR and clamps. Swapping a raise for a clamp with nothing driving
    it just moves the untested code, so this drives it.
    """

    @pytest.mark.asyncio
    async def test_over_release_clamps_instead_of_inflating_capacity(self, caplog):
        import logging

        from src.core.bounded_executor import _AsyncAdmissionGate

        gate = _AsyncAdmissionGate(capacity=2)

        with caplog.at_level(logging.ERROR, logger="src.core.bounded_executor"):
            gate.release()  # one more release than was ever acquired

        assert gate._available <= 2, (
            f"capacity inflated to {gate._available} — an over-release widens the bulkhead it bounds"
        )
        assert any("released too many times" in r.getMessage() for r in caplog.records), (
            "the over-release was neither raised nor logged, so it is invisible"
        )
