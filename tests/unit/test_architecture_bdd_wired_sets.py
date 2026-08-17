"""The anti-dormancy guard's wired-tag union must be derived, and shrink-only.

`tests/bdd/conftest.py`'s makereport hook refuses to auto-xfail a scenario
whose tag appears in a `*_WIRED` set: such a tag is a promise the scenario
executes, and auto-xfailing a broken binding turns dormant coverage green.

Two ways that guard can be defeated without touching a scenario, both closed
here:

1. **Enumeration drift.** The union used to list four set names by hand, with
   the completeness rule stated only in a docstring ("Add new *_WIRED sets
   here when you add them"). A fifth set would simply not be guarded. The
   union is now derived from module globals; this file grades the derivation.
2. **Deleting the tag.** The guard's own failure message used to suggest
   "remove the tag from the wired set" as a remedy. For UC-002 that turns the
   guard green and restores the dormancy, because the UC-002 catch-all in
   `_harness_env` is `pytest.xfail` rather than a raise. The sets are pinned
   shrink-only below, so removing a tag is a deliberate edit to a baseline
   rather than a one-line escape.
"""

from __future__ import annotations

# Recorded membership. This baseline may GROW freely (wiring more scenarios is
# the goal) and may only shrink through a deliberate edit here, with the reason
# in the commit message.
_WIRED_BASELINE: dict[str, set[str]] = {
    "_UC002_IDEMPOTENCY_WIRED": {
        "T-UC-002-v31-idempotency-missing",
        "T-UC-002-v31-idempotency-pattern-invalid",
        "T-UC-002-v31-idempotency-replay",
    },
    "_UC002_MANUAL_APPROVAL_WIRED": {
        "T-UC-002-alt-manual",
    },
    "_UC010_VERSION_NEGOTIATION_WIRED": {
        "T-UC-010-v31-version-unsupported",
        "T-UC-010-v31-version-unsupported-build-version-advisory",
        "T-UC-010-v31-version-unsupported-cross-major",
        "T-UC-010-v31-version-unsupported-details-bounds",
        "T-UC-010-v31-version-unsupported-major-fallback",
        "T-UC-010-v31-version-unsupported-prerelease",
        "T-UC-010-v31-version-unsupported-sub-min",
    },
    # Hoisted from function-local scope in the round-3 response: as locals they
    # were bound but invisible to the globals-scan derivation, so unbinding a
    # step degraded them to a silent xfail instead of failing.
    "_UC003_MANUAL_APPROVAL_WIRED": {
        "T-UC-003-alt-manual",
        "T-UC-003-approval-adapter",
        "T-UC-003-approval-tenant",
    },
    "_UC003_REQUIRED_IDEMPOTENCY_WIRED": {
        "T-UC-003-local-required-idempotency-v311",
    },
    "_UC010_CAPABILITY_FILTER_WIRED": {
        "T-UC-010-local-capability-filter-empty-v311",
        "T-UC-010-local-capability-filter-invalid-v311",
        "T-UC-010-local-capability-filter-unsupported-v311",
        "T-UC-010-local-capability-filter-v311",
    },
}


def _wired_globals() -> dict[str, set[str]]:
    from tests.bdd import conftest

    return {name: value for name, value in vars(conftest).items() if name.endswith("_WIRED") and isinstance(value, set)}


def test_wired_union_is_derived_not_enumerated():
    """Every `*_WIRED` module global reaches the guard, without being listed."""
    from tests.bdd.conftest import _wired_scenario_tags

    union = _wired_scenario_tags()
    missing = {name: sorted(value - union) for name, value in _wired_globals().items() if not value.issubset(union)}

    assert not missing, (
        "these *_WIRED tags are declared bound but never reach the anti-dormancy guard, "
        f"so a broken binding would auto-xfail silently: {missing}"
    )


def test_wired_sets_never_shrink():
    """Removing a tag must be a deliberate edit here, not a one-line escape.

    The guard's failure message previously offered tag removal as a remedy.
    Growth is free; shrinkage has to be argued for.
    """
    live = _wired_globals()

    vanished_sets = sorted(set(_WIRED_BASELINE) - set(live))
    assert not vanished_sets, f"wired sets removed wholesale: {vanished_sets}"

    lost = {name: sorted(tags - live[name]) for name, tags in _WIRED_BASELINE.items() if tags - live[name]}
    assert not lost, (
        "tags removed from a wired set — this un-guards the scenario and, where the "
        "use-case catch-all is pytest.xfail, silently restores dormancy. If the removal is "
        f"intended, update _WIRED_BASELINE in this file in the same commit: {lost}"
    )
