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


# Sites whose sandbox= is a hard-wired literal, with the reason it cannot be derived.
# A literal is normally the defect this guard exists to catch — it satisfies the
# presence check while dispatching every request to one mode — so an entry here must
# say why the mode is genuinely static. Allowlists only shrink.
LITERAL_EXEMPT: dict[str, str] = {
    "src/core/tools/products.py": (
        "identity.sandbox is never populated on this path (no enrich_identity_with_account "
        "call), so the value was already constantly False; the literal states it honestly"
    ),
    "src/core/tools/capabilities.py": (
        "GetAdcpCapabilitiesRequest has no account field in the pinned SDK, so the mode is "
        "dead by protocol rather than by omission"
    ),
}


def _get_adapter_calls() -> list[tuple[str, int, bool, ast.expr | None]]:
    """Return (relative_path, lineno, passes_sandbox, value_node) per get_adapter() call."""
    found: list[tuple[str, int, bool, ast.expr | None]] = []
    for root in SCAN_ROOTS:
        for path in root.rglob("*.py"):
            tree = safe_parse(path)
            if tree is None:
                continue
            for call in iter_call_expressions(tree, "get_adapter"):
                value = next((kw.value for kw in call.keywords if kw.arg == "sandbox"), None)
                found.append((str(path.relative_to(REPO_ROOT)), call.lineno, value is not None, value))
    return found


def test_every_get_adapter_call_decides_sandbox_explicitly() -> None:
    """A call site that omits sandbox= silently dispatches sandbox buys to a real adapter."""
    calls = _get_adapter_calls()
    assert calls, "found no get_adapter() calls — the scan roots are wrong"

    missing = [
        f"{path}:{lineno}"
        for path, lineno, passes, _value in calls
        if not passes and f"{path}:{lineno}" not in KNOWN_EXEMPT
    ]

    assert not missing, (
        "get_adapter() called without an explicit sandbox= decision at:\n  "
        + "\n  ".join(missing)
        + "\n\nPass sandbox=identity.sandbox where an account-enriched ResolvedIdentity is "
        "in scope. On buy-keyed and deferred paths (update, performance, creative push, "
        "the approval executor, admin routes) derive it from the buy's account: with a UoW "
        "in scope use BuyKeyedSandboxMixin.sandbox_mode(buy) — or sandbox_mode_by_id(id) when "
        "only the id is held — and without one call "
        "account_helpers.sandbox_mode_for_buy(accounts, buy). Named for the MIXIN, not a concrete "
        "UoW: deferred creative push holds an AdminCreativeUoW, so MediaBuyUoW.sandbox_mode_by_id "
        "is an AttributeError there, and the admin detail route holds no UoW at all. "
        "See AdCP 3.1.1 sandbox.mdx §Seller implementation."
    )


def test_guard_would_catch_a_regression() -> None:
    """Mutation self-test: the detector must actually flag a call missing sandbox=."""
    tree = ast.parse("adapter = get_adapter(principal, dry_run=False, tenant=tenant)\n")
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    assert not any(kw.arg == "sandbox" for kw in call.keywords), (
        "the detector's own predicate no longer distinguishes a sandbox-less call — this guard would pass vacuously"
    )


# Sites where identity.sandbox is the CORRECT source: the request itself carries an
# account reference, which enrich_identity_with_account resolves at the boundary before
# _impl runs. Everywhere else the identity is unenriched and the flag is structurally
# False, so reading it is the original defect wearing the right keyword.
# Keyed by ``module::function``, NOT by module. A module-wide entry exempts every
# get_adapter call in the file, and media_buy_create.py holds five across four
# functions — only two of which are request-scoped. Keying it by file therefore
# un-guarded the approval executor, the site the round that added this arm named as
# its own example: reverting it to identity.sandbox left the guard green.
IDENTITY_KEYED_SITES: dict[str, str] = {
    "src/core/tools/media_buy_create.py::_create_media_buy_impl": (
        "create_media_buy declares and forwards `account`; the boundary enriches the "
        "identity before _impl, so identity.sandbox is the resolved account's mode"
    ),
    "src/core/tools/media_buy_create.py::_execute_adapter_media_buy_creation": (
        "called from _create_media_buy_impl with the mode it already resolved from the "
        "request's account reference, and passed down as a plain argument"
    ),
}


def _resolves_to_identity_sandbox(value: ast.expr, func: ast.FunctionDef | ast.AsyncFunctionDef | None) -> bool:
    """True when ``value`` reaches ``identity.sandbox`` — directly or through a local.

    The previous arm only matched ``sandbox=identity.sandbox`` written inline, so it was
    inert: every scanned site passes a bare Name (``_mb_sandbox``, ``is_sandbox``,
    ``partition_is_sandbox``, ``sandbox``). Deleting its whole body left the guard green,
    and reverting a module to the original defect in its own idiom did not redden it.
    Resolving one assignment hop is what makes the arm able to fail at all.
    """

    def _is_identity_attr(node: ast.expr) -> bool:
        # identity.sandbox, self.identity.sandbox, ctx.identity.sandbox ...
        if not (isinstance(node, ast.Attribute) and node.attr == "sandbox"):
            return False
        base = node.value
        if isinstance(base, ast.Name):
            return base.id == "identity"
        return isinstance(base, ast.Attribute) and base.attr == "identity"

    if _is_identity_attr(value):
        return True
    if not (isinstance(value, ast.Name) and func is not None):
        return False
    # One hop: find the local's assignment inside the same function.
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            targets = [tgt.id for tgt in node.targets if isinstance(tgt, ast.Name)]
            if value.id in targets and _is_identity_attr(node.value):
                return True
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == value.id and node.value is not None:
                if _is_identity_attr(node.value):
                    return True
    return False


def _enclosing_function(tree: ast.Module, call: ast.Call):
    """The innermost function containing *call*, or None at module scope."""
    best = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.lineno <= call.lineno and (node.end_lineno or node.lineno) >= call.lineno:
            if best is None or node.lineno > best.lineno:
                best = node
    return best


def test_only_account_carrying_paths_source_sandbox_from_identity() -> None:
    """A correct keyword from the wrong source still dispatches sandbox buys to live.

    Scans every module with a get_adapter call rather than a hardcoded four. The old
    list named update/performance/list/delivery while the failure message advertised
    "update, performance, creative push, the approval executor, admin routes" — so
    injecting the defect at the approval executor or the admin route left the guard
    green at exactly the sites the message told the reader were covered.
    """
    offenders: list[str] = []
    for root in SCAN_ROOTS:
        for path in root.rglob("*.py"):
            tree = safe_parse(path)
            if tree is None:
                continue
            rel = str(path.relative_to(REPO_ROOT))
            for call in iter_call_expressions(tree, "get_adapter"):
                value = next((kw.value for kw in call.keywords if kw.arg == "sandbox"), None)
                if value is None:
                    continue
                func = _enclosing_function(tree, call)
                site = f"{rel}::{func.name}" if func is not None else rel
                if site in IDENTITY_KEYED_SITES:
                    continue
                if _resolves_to_identity_sandbox(value, func):
                    offenders.append(f"{rel}:{call.lineno} (in {func.name if func else '<module>'})")

    assert not offenders, (
        "sandbox= resolves to identity.sandbox on a path that does not carry an account "
        "reference:\n  " + "\n  ".join(offenders) + "\n\nidentity.sandbox is only populated where the boundary ran "
        "enrich_identity_with_account. Derive the mode from the buy's account instead "
        "(BuyKeyedSandboxMixin.sandbox_mode / sandbox_mode_by_id / "
        "partition_by_sandbox_mode), or add the module to IDENTITY_KEYED_SITES with the "
        "reason its requests carry an account."
    )


def test_identity_arm_catches_both_the_inline_and_the_via_local_form() -> None:
    """Self-test, both signs and both idioms — the arm was previously inert.

    Production writes the via-local form at every site, so an arm that only matched the
    inline form could never fire. A negative case is included because an arm that flags
    everything is as useless as one that flags nothing.
    """
    inline = ast.parse("def f(identity):\n    return get_adapter(p, sandbox=identity.sandbox)\n")
    via_local = ast.parse("def f(identity):\n    s = identity.sandbox\n    return get_adapter(p, sandbox=s)\n")
    via_self = ast.parse("def f(self):\n    s = self.identity.sandbox\n    return get_adapter(p, sandbox=s)\n")
    derived = ast.parse("def f(uow, mb):\n    s = uow.sandbox_mode(mb)\n    return get_adapter(p, sandbox=s)\n")

    def _flags(tree: ast.Module) -> bool:
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "get_adapter")
        value = next(kw.value for kw in call.keywords if kw.arg == "sandbox")
        return _resolves_to_identity_sandbox(value, _enclosing_function(tree, call))

    assert _flags(inline), "the arm no longer catches the inline identity.sandbox form"
    assert _flags(via_local), "the arm does not follow a local assignment — this is how it was inert"
    assert _flags(via_self), "the arm misses self.identity.sandbox"
    assert not _flags(derived), "the arm flags a correct buy-keyed derivation"


def test_no_call_site_hard_wires_the_sandbox_mode() -> None:
    """A literal satisfies the presence check while deciding nothing.

    Replacing any site's expression with ``sandbox=False`` — the cheapest way to
    satisfy arm 1 — left that arm green at 12 of 12 sites, and the unit suite
    byte-identical for several of them. Presence and value are different claims, so
    they get different arms: this one rejects a constant unless the site is listed in
    LITERAL_EXEMPT with a written reason.
    """
    offenders = [
        f"{path}:{lineno}"
        for path, lineno, _passes, value in _get_adapter_calls()
        if isinstance(value, ast.Constant) and path not in LITERAL_EXEMPT
    ]

    assert not offenders, (
        "get_adapter() called with a hard-wired sandbox= literal at:\n  "
        + "\n  ".join(offenders)
        + "\n\nA constant dispatches every request to one mode regardless of the account. "
        "Derive it (identity.sandbox where the request carries an account reference, "
        "uow.sandbox_mode*/partition_by_sandbox_mode on buy-keyed paths), or add the site "
        "to LITERAL_EXEMPT with the reason the mode is genuinely static."
    )


def test_literal_arm_would_catch_a_hard_wired_site() -> None:
    """Self-test with BOTH signs: the detector must flag a literal and pass a derivation.

    The existing self-test at the top re-implements arm 1's predicate against a
    synthetic string rather than calling the detector, and covers only one sign. An
    arm that never fires and an arm with no true positive read identically from the
    outside — green.
    """
    flagged = ast.parse("a = get_adapter(p, sandbox=False)\n")
    derived = ast.parse("a = get_adapter(p, sandbox=identity.sandbox)\n")

    def _value(tree: ast.Module) -> ast.expr | None:
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
        return next((kw.value for kw in call.keywords if kw.arg == "sandbox"), None)

    assert isinstance(_value(flagged), ast.Constant), "the literal arm no longer recognises a constant"
    assert not isinstance(_value(derived), ast.Constant), "the literal arm would flag a legitimate derivation"
