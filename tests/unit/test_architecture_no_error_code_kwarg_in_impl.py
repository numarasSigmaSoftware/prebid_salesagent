"""Guard: error_code= must not bypass the typed AdCPError hierarchy.

The wire error code is the identity of a typed AdCPError subclass. Passing
``error_code=`` to an ``AdCP*Error(...)`` constructor bypasses that hierarchy and
can leak a code that is neither standard nor mapped (the original A1 defect). The
only sanctioned override is ``AdCPError.synthesize(error_code=...)``, used by the
shared scrub constructor for wire codes the class hierarchy does not model.

This guard scans ``src/core/`` and ``src/adapters/`` for any call that passes
``error_code=`` to an ``AdCP*Error`` constructor OR to ``.synthesize()``. After the
error-emission migration, the only such call is the shared ``_scrubbed_error`` site —
pinned in the allowlist so a new bypass (or a new synthesize caller) fails the build.
"""

import ast
from collections.abc import Iterator
from pathlib import Path

from tests.unit._architecture_helpers import assert_violations_match_allowlist, iter_call_expressions

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = [REPO_ROOT / "src" / "core", REPO_ROOT / "src" / "adapters"]

# The only sanctioned error_code= site: the shared scrub constructor that calls
# AdCPError.synthesize(). Keyed by (relative_path, enclosing_function_name) so the
# allowlist survives line-number shifts.
KNOWN_VIOLATIONS = {
    ("src/core/exceptions.py", "_scrubbed_error"),
}


def _func_targets_adcp_error_or_synthesize(func: ast.expr) -> bool:
    """True if a Call's func is an AdCP*Error constructor or a .synthesize() call."""
    if isinstance(func, ast.Name):
        return func.id.startswith("AdCP") and func.id.endswith("Error")
    if isinstance(func, ast.Attribute):
        if func.attr == "synthesize":
            return True
        return func.attr.startswith("AdCP") and func.attr.endswith("Error")
    return False


def _call_has_error_code_in_details(call: ast.Call) -> bool:
    """True if a Call passes ``details={...}`` with an ``error_code`` key (the smuggling form).

    ``error_code=`` is the kwarg bypass; ``details={"error_code": "X"}`` is the
    indirection variant that smuggles a second code into the envelope's
    ``errors[0].details`` while the typed class still sets the wire ``adcp_error.code``.
    Both bypass the "wire code = subclass identity" invariant. Only inline dict
    literals are matched (a ``details=`` built from a variable would slip through, but
    every known site uses a literal).
    """
    for kw in call.keywords:
        if kw.arg == "details" and isinstance(kw.value, ast.Dict):
            if any(isinstance(k, ast.Constant) and k.value == "error_code" for k in kw.value.keys):
                return True
    return False


def _iter_adcp_error_calls() -> Iterator[tuple[str, str, ast.Call]]:
    """Yield (relative_path, enclosing_function, call) for every AdCP*Error/synthesize call."""
    for scan_dir in SCAN_DIRS:
        for py_file in scan_dir.rglob("*.py"):
            try:
                tree = ast.parse(py_file.read_text(), filename=str(py_file))
            except SyntaxError:
                continue
            rel_path = str(py_file.relative_to(REPO_ROOT))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for child in iter_call_expressions(node):
                    if _func_targets_adcp_error_or_synthesize(child.func):
                        yield rel_path, node.name, child


def _find_error_code_kwargs() -> list[tuple[str, str, int]]:
    """Find error_code= kwargs on AdCP*Error/synthesize calls. (relative_path, function, lineno)."""
    return [
        (rel, func, call.lineno)
        for rel, func, call in _iter_adcp_error_calls()
        if any(kw.arg == "error_code" for kw in call.keywords)
    ]


def _find_error_code_in_details() -> list[tuple[str, str, int]]:
    """Find details={"error_code": ...} smuggling on AdCP*Error calls. (relative_path, function, lineno)."""
    return [
        (rel, func, call.lineno)
        for rel, func, call in _iter_adcp_error_calls()
        if _call_has_error_code_in_details(call)
    ]


class TestNoErrorCodeKwargInImpl:
    """error_code= on AdCPError constructors/synthesize is allowlisted to one site."""

    def test_no_new_error_code_kwargs(self):
        """No NEW error_code= bypass beyond the shared scrub constructor."""
        new_violations = [
            f"  {rel}:{lineno} in {func}()"
            for rel, func, lineno in _find_error_code_kwargs()
            if (rel, func) not in KNOWN_VIOLATIONS
        ]
        assert not new_violations, (
            f"Found {len(new_violations)} NEW error_code= site(s) on AdCP*Error/synthesize.\n"
            "Raise a typed AdCPError subclass instead; the only sanctioned override is "
            "AdCPError.synthesize() at the allowlisted shared scrub constructor.\n" + "\n".join(new_violations)
        )

    def test_known_violations_not_stale(self):
        """Every allowlisted (file, function) must still contain a sanctioned site."""
        actual = {(rel, func) for rel, func, _ in _find_error_code_kwargs()}
        assert_violations_match_allowlist(
            actual,
            KNOWN_VIOLATIONS,
            fix_hint="Remove fixed entries from KNOWN_VIOLATIONS.",
        )

    def test_violation_count_capped_at_one(self):
        """Exactly one sanctioned error_code= site exists."""
        all_sites = {(rel, func) for rel, func, _ in _find_error_code_kwargs()}
        msg = f"error_code= sites changed.\nFound: {sorted(all_sites)}\nAllowlist: {sorted(KNOWN_VIOLATIONS)}"
        assert all_sites == KNOWN_VIOLATIONS, msg


class TestNoErrorCodeInDetails:
    """details={"error_code": ...} smuggling is forbidden — the wire code is the typed class identity.

    Allowlist is empty: there is no sanctioned site for the indirection variant. A raise
    that needs a specific wire code uses a typed subclass (create one if no standard code
    matches); buyer-actionable detail goes under a non-``error_code`` key (e.g. ``creative_errors``).
    """

    def test_no_error_code_in_details_literal(self):
        sites = [f"  {rel}:{lineno} in {func}()" for rel, func, lineno in _find_error_code_in_details()]
        assert not sites, (
            f'Found {len(sites)} details={{"error_code": ...}} smuggling site(s). The wire code must come '
            "from the typed AdCPError subclass identity, not a smuggled details sub-code. Migrate to a typed "
            "subclass and drop the details error_code key (keep other detail keys):\n" + "\n".join(sites)
        )
