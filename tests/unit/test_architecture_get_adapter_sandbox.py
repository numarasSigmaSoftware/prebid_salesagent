"""Structural guard: every get_adapter() call decides sandbox mode explicitly.

AdCP 3.1.1 ``dist/docs/3.1.1/media-buy/advanced-topics/sandbox.mdx`` §Seller
implementation requires that a request referencing a sandbox account:

    - MUST NOT make real ad platform API calls (no real orders, line items, etc.)
    - MUST NOT charge real money or create real billing records

Adapters are selected per-tenant, so ``get_adapter()`` is the single chokepoint where a
sandbox request can be diverted to the mock adapter. ``sandbox=`` defaults to ``False``
so that omitting it is *safe by type* but *wrong by default* — a new call site that
forgets it would dispatch a sandbox buy to the tenant's real ad server, which is the
exact defect this guard exists to prevent recurring.

This is a semantic-SSOT guard: no linter or type checker can see that a missing keyword
argument means "books a real campaign". Allowlists here may only shrink.
"""

from __future__ import annotations

import ast

from tests.unit._architecture_helpers import REPO_ROOT, iter_call_expressions, safe_parse

SCAN_ROOTS = (REPO_ROOT / "src",)

# Call sites that legitimately cannot decide sandbox mode, with the reason.
# Empty by design — add an entry only with a written justification, never to silence.
KNOWN_EXEMPT: dict[str, str] = {}


def _get_adapter_calls() -> list[tuple[str, int, bool]]:
    """Return (relative_path, lineno, passes_sandbox) for every get_adapter() call."""
    found: list[tuple[str, int, bool]] = []
    for root in SCAN_ROOTS:
        for path in root.rglob("*.py"):
            tree = safe_parse(path)
            if tree is None:
                continue
            for call in iter_call_expressions(tree, "get_adapter"):
                passes = any(kw.arg == "sandbox" for kw in call.keywords)
                found.append((str(path.relative_to(REPO_ROOT)), call.lineno, passes))
    return found


def test_every_get_adapter_call_decides_sandbox_explicitly() -> None:
    """A call site that omits sandbox= silently dispatches sandbox buys to a real adapter."""
    calls = _get_adapter_calls()
    assert calls, "found no get_adapter() calls — the scan roots are wrong"

    missing = [
        f"{path}:{lineno}" for path, lineno, passes in calls if not passes and f"{path}:{lineno}" not in KNOWN_EXEMPT
    ]

    assert not missing, (
        "get_adapter() called without an explicit sandbox= decision at:\n  "
        + "\n  ".join(missing)
        + "\n\nPass sandbox=identity.sandbox where an account-enriched ResolvedIdentity is "
        "in scope. On buy-keyed and deferred paths (update, performance, creative push, "
        "the approval executor, admin routes) use MediaBuyUoW.sandbox_mode_by_id(id) — or "
        "sandbox_mode(buy) when the row is already loaded. See AdCP 3.1.1 sandbox.mdx "
        "§Seller implementation."
    )


def test_guard_would_catch_a_regression() -> None:
    """Mutation self-test: the detector must actually flag a call missing sandbox=."""
    tree = ast.parse("adapter = get_adapter(principal, dry_run=False, tenant=tenant)\n")
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    assert not any(kw.arg == "sandbox" for kw in call.keywords), (
        "the detector's own predicate no longer distinguishes a sandbox-less call — this guard would pass vacuously"
    )


# Operations addressed by media_buy_id carry no account reference, so identity.sandbox is
# structurally False for them. Sourcing sandbox= from the identity on these paths is the
# original defect wearing the right keyword — the presence check above cannot see it.
_BUY_KEYED_MODULES = (
    "src/core/tools/media_buy_update.py",
    "src/core/tools/performance.py",
    "src/core/tools/media_buy_list.py",
    "src/core/tools/media_buy_delivery.py",
)


def test_buy_keyed_operations_do_not_source_sandbox_from_identity() -> None:
    """A correct keyword from the wrong source still dispatches sandbox buys to live."""
    offenders: list[str] = []
    for rel in _BUY_KEYED_MODULES:
        tree = safe_parse(REPO_ROOT / rel)
        assert tree is not None, f"{rel} is missing — update _BUY_KEYED_MODULES"
        for call in iter_call_expressions(tree, "get_adapter"):
            for kw in call.keywords:
                if kw.arg != "sandbox":
                    continue
                # sandbox=identity.sandbox
                if isinstance(kw.value, ast.Attribute) and kw.value.attr == "sandbox":
                    base = getattr(kw.value.value, "id", None)
                    if base == "identity":
                        offenders.append(f"{rel}:{call.lineno}")

    assert not offenders, (
        "buy-keyed operation sources sandbox= from identity.sandbox at:\n  "
        + "\n  ".join(offenders)
        + "\n\nidentity.sandbox is only populated when the request carries an account "
        "reference; these operations are addressed by media_buy_id. Derive the mode from "
        "the buy's account instead (uow.sandbox_mode_by_id / partition_by_sandbox_mode)."
    )
