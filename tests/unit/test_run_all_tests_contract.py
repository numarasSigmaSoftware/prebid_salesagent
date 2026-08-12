"""Contract test for run_all_tests.sh argument handling.

Guards PR #1420 review finding #2: the in-network runner replaced the
historical MODE contract (``ci`` default / ``quick`` / targeted) with a raw tox
suite-list, so ``./run_all_tests.sh ci`` became ``tox -e ci`` ->
"provided environments not found: ci", breaking Makefile quality-full/test-full
and the documented commands.

Asserts the resolved contract via the ``RUN_ALL_TESTS_RESOLVE_ONLY`` seam so no
Docker stack is needed.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNNER = _REPO_ROOT / "run_all_tests.sh"
_HOST_RUNNER = _REPO_ROOT / "run_all_tests_host.sh"
_TOX_INI = _REPO_ROOT / "tox.ini"
_ALL_SUITES = "unit,integration,bdd,admin,e2e,ui"


def _resolve(*args: str) -> str:
    proc = subprocess.run(
        ["bash", str(_RUNNER), *args],
        cwd=_REPO_ROOT,
        env={**os.environ, "RUN_ALL_TESTS_RESOLVE_ONLY": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"resolve-only exited {proc.returncode}: {proc.stderr}"
    return proc.stdout.strip()


@pytest.mark.parametrize(
    "args, expected",
    [
        # The resolve line now also reports the scheduler interval, which is scoped
        # to the suites that grade it (see TestSchedulerIntervalIsScopedToTheE2eSuite).
        # Kept as exact full-line matches: this is a contract test, and a substring
        # match here would stop noticing a change to the part it does not name.
        ([], f"RESOLVED suites={_ALL_SUITES} scheduler_interval=5"),  # bare == full in-network run
        (["ci"], f"RESOLVED suites={_ALL_SUITES} scheduler_interval=5"),  # ci is the explicit alias (was broken)
        (["quick"], "RESOLVED delegate-host: quick"),  # no-Docker fast path -> host runner
        (["unit,integration"], "RESOLVED suites=unit,integration scheduler_interval=<off>"),  # explicit tox env list
        (
            ["ci", "tests/integration/test_x.py", "-k", "foo"],  # targeted form -> host runner
            "RESOLVED delegate-host: ci tests/integration/test_x.py -k foo",
        ),
    ],
)
def test_run_all_tests_arg_contract(args, expected):
    assert _resolve(*args) == expected


def _tox_env_list() -> set[str]:
    """Canonical parallel suite set from tox.ini (coverage runs separately)."""
    m = re.search(r"^env_list\s*=\s*(.+)$", _TOX_INI.read_text(), re.MULTILINE)
    assert m, "tox.ini has no env_list"
    return {s.strip() for s in m.group(1).split(",") if s.strip()}


def _runner_all_suites() -> set[str]:
    m = re.search(r'ALL_SUITES="([^"]+)"', _RUNNER.read_text())
    assert m, "run_all_tests.sh has no ALL_SUITES"
    return {s.strip() for s in m.group(1).split(",") if s.strip()}


def _host_runner_collect_suites() -> set[str]:
    m = re.search(r"for name in ([a-z][a-z0-9 ]+?); do", _HOST_RUNNER.read_text())
    assert m, "run_all_tests_host.sh collect_reports loop not found"
    return set(m.group(1).split())


def test_six_suite_list_is_single_sourced():
    """All four materializations of the suite list must match tox.ini env_list.

    PR #1420 review nit: the six-suite list is duplicated in run_all_tests.sh,
    run_all_tests_host.sh, tox.ini, and this test's _ALL_SUITES, with nothing
    tying them — a 7th tox env added without updating the runners would silently
    not run in-network. Compare as sets (parallel-run order vs report-collection
    order legitimately differ); tox.ini env_list is the single source.
    """
    canonical = _tox_env_list()
    assert _runner_all_suites() == canonical, "run_all_tests.sh ALL_SUITES drifted from tox env_list"
    assert _host_runner_collect_suites() == canonical, "run_all_tests_host.sh collect_reports drifted from tox env_list"
    assert {s.strip() for s in _ALL_SUITES.split(",")} == canonical, "_ALL_SUITES constant drifted from tox env_list"


class TestSchedulerIntervalIsScopedToTheE2eSuite:
    """The delivery-webhook scheduler runs only for the suite that grades it.

    ``DELIVERY_WEBHOOK_INTERVAL`` was exported unconditionally, so every
    in-network invocation started a scheduler ticking every 5s on the server —
    including the ``bdd_e2e`` leg, which does not grade the background tick (its
    delivery scenarios drive ``send_delivery_webhook`` in-process).

    That produced a real deadlock, not a slowdown. The scheduler's selection query
    takes ``media_buys`` then ``webhook_delivery_log`` (the correlated anti-join);
    the per-scenario fixture reset issues ``TRUNCATE ... CASCADE``, needing
    AccessExclusiveLock in the opposite order. Postgres killed one side, failing
    roughly half of in-network runs on a different scenario each time. It read as
    unexplained flakiness for several rounds — two wrong causes were published and
    withdrawn — until the server-log dump captured ``DeadlockDetected``.

    Parametrized over the invocations that actually occur, because the bug was a
    single unconditional export: asserting only the ``e2e`` case would pass for
    the broken version too.

    Scope of this gate, stated because an earlier revision of it over-claimed: it
    keeps the scheduler OUT of legs that do not grade it. It does NOT make
    ``./run_all_tests.sh ci`` safe -- that invocation legitimately needs the
    scheduler for its ``e2e`` suite while also running bdd_e2e, so the interval is
    5 and both run. Lock ordering is what makes that safe; see
    tests/integration/test_e2e_reset_lock_ordering.py.
    """

    @pytest.mark.parametrize(
        "args,expected,why",
        [
            pytest.param(["e2e"], "5", "test_daily_delivery_webhook waits for a real tick", id="e2e-alone"),
            # These two are NOT protected by the scoping, and must not be read as if
            # they were. `ci` runs the `e2e` suite -- which REQUIRES a ticking
            # scheduler -- alongside bdd_e2e in one compose stack (the E2E_WORKERS
            # fast path rewrites `bdd` into `bdd_inprocess,bdd_e2e` AFTER this
            # decision, and after the resolve-only seam, so neither this value nor
            # this test can see it). Interval 5 is correct here; what keeps the two
            # safe together is the lock ordering in _reset_e2e_db, graded by
            # tests/integration/test_e2e_reset_lock_ordering.py.
            pytest.param(["ci"], "5", "the full suite includes e2e", id="ci-full"),
            pytest.param([], "5", "no argument means the full suite", id="default"),
            pytest.param(["bdd_e2e"], "<off>", "the CI in-network job — the leg that deadlocked", id="bdd_e2e"),
            pytest.param(["bdd_inprocess,bdd_e2e"], "<off>", "the E2E_WORKERS fast-path split", id="bdd-split"),
            pytest.param(["unit,integration"], "<off>", "no server-side scheduler needed", id="no-server-suites"),
            pytest.param(["bdd"], "<off>", "plain bdd does not grade the tick either", id="bdd-plain"),
        ],
    )
    def test_interval_is_set_only_for_suites_that_need_it(self, args, expected, why):
        out = _resolve(*args)
        assert f"scheduler_interval={expected}" in out, f"{args or ['<none>']}: {why} — resolved to {out!r}"

    def test_bdd_e2e_is_not_matched_as_the_e2e_token(self):
        """The substring trap this gate has to avoid.

        ``bdd_e2e`` contains ``e2e``. A plain substring test would enable the
        scheduler for precisely the leg the scoping exists to protect, leaving the
        deadlock in place while looking fixed.
        """
        assert "scheduler_interval=<off>" in _resolve("bdd_e2e")
        assert "scheduler_interval=5" in _resolve("e2e")

    def test_an_explicit_operator_value_still_wins(self):
        """Scoping must not stop an operator forcing the scheduler on."""
        proc = subprocess.run(
            ["bash", str(_RUNNER), "bdd_e2e"],
            cwd=_REPO_ROOT,
            env={**os.environ, "RUN_ALL_TESTS_RESOLVE_ONLY": "1", "DELIVERY_WEBHOOK_INTERVAL": "9"},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert "scheduler_interval=9" in proc.stdout, proc.stdout
