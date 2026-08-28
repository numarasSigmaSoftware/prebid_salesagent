"""Guard: a bare static validation message must opt in to reaching the wire.

``safe_adcp_error`` scrubs an ``AdCPValidationError``'s message by default, because
business validators frequently interpolate rejected request values. That default is
right for interpolated messages and pure collateral damage for a bare string literal:
there is no request-derived value in a constant, so nothing can leak, and genericizing
it costs the buyer the one diagnostic they could have acted on — for errors whose
``recovery`` is ``correctable``, i.e. exactly the ones the buyer is expected to fix.

This guard scans ``src/`` for ``AdCP{Validation,InvalidRequest}Error(...)`` calls whose
FIRST positional argument is a plain string literal and which do NOT pass
``_wire_safe_message=True``. Interpolated messages (f-strings, ``.format()``, ``+``,
variables) are deliberately out of scope: those need a per-site audit of what they
interpolate, tracked separately, and must keep scrubbing until that audit happens.

Why a guard and not a one-time sweep: 31 such sites had accumulated by the time this
was noticed, all silently downgraded the moment the scrub landed. Nothing failed —
the wire contract stayed valid (code/recovery/status/field are all preserved), only
the human-readable half regressed, which no existing assertion covered.
"""

import ast
from pathlib import Path

from tests.unit._architecture_helpers import assert_violations_match_allowlist, iter_call_expressions

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_DIR = REPO_ROOT / "src"


def _target_class_names() -> set[str]:
    """The class names the scrub actually applies to, derived from the closure.

    ``safe_adcp_error`` gates on ``isinstance(exc, AdCPValidationError)``, so the
    scanned set is ``AdCPValidationError`` plus every subclass, transitively — NOT a
    hand-listed pair. A new subclass would otherwise be scrubbed by production while
    invisible to this guard, which is the exact silent downgrade the guard exists to
    catch.
    """
    from src.core.exceptions import AdCPValidationError

    names = {AdCPValidationError.__name__}
    frontier = [AdCPValidationError]
    while frontier:
        for sub in frontier.pop().__subclasses__():
            if sub.__name__ not in names:
                names.add(sub.__name__)
                frontier.append(sub)
    return names


_TARGET_CLASSES = _target_class_names()

# Empty by design. A new bare-literal raise site without the opt-in is a diagnostic
# regression, not debt to be parked here — add ``_wire_safe_message=True`` instead.
# Only add an entry if a literal message genuinely must be scrubbed, with the reason.
KNOWN_VIOLATIONS: set[tuple[str, int]] = set()

# The OTHER direction, and the one that can actually leak: an INTERPOLATED message that
# opts in. Each entry is an assertion that a human traced every interpolated value to
# something the buyer already has — their own request data or a sanitized projection —
# and never to adapter, DB or seller internals. Keep this small; a new entry is a claim
# about a specific f-string, not a place to park an unreviewed site.
AUDITED_INTERPOLATED_OPT_INS: dict[tuple[str, int], str] = {
    (
        "src/core/tools/media_buy_create.py",
        2721,
    ): "interpolates the duplicate product_ids from the buyer's OWN packages[] — their data, echoed back",
    (
        "src/core/tools/media_buy_create.py",
        3077,
    ): "interpolates targeting-validator violations describing the buyer's OWN targeting_overlay",
    (
        "src/core/validation_helpers.py",
        57,
    ): "format_validation_error() — the sanitized Pydantic projection: field paths and error types, never raw msg/input/ctx",
}


def _message_expr(node: ast.Call) -> ast.expr | None:
    """The expression supplying the error message, positional OR ``message=`` kwarg.

    Reading only ``args[0]`` made the kwarg form invisible to this guard, so a site
    written ``AdCPValidationError(message="...")`` was scanned by neither direction.
    """
    if node.args:
        return node.args[0]
    for kw in node.keywords:
        if kw.arg == "message":
            return kw.value
    return None


def _is_bare_string_literal_message(node: ast.Call) -> bool:
    """True iff the message expression is a plain string constant.

    An f-string is an ``ast.JoinedStr`` and a concatenation is an ``ast.BinOp``, so
    neither matches here — only a literal (including implicit adjacent-string
    concatenation, which the parser folds into one ``ast.Constant``).
    """
    expr = _message_expr(node)
    return isinstance(expr, ast.Constant) and isinstance(expr.value, str)


def _iter_target_calls():
    """Yield ``(relative_path, node)`` for every construction of a target class."""
    for path in sorted(SCAN_DIR.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - a syntax error fails elsewhere, loudly
            continue
        rel = str(path.relative_to(REPO_ROOT))
        for cls_name in sorted(_TARGET_CLASSES):
            # iter_call_expressions also matches attribute calls (mod.AdCP...);
            # both forms are equally in scope for this guard.
            for node in iter_call_expressions(tree, cls_name):
                yield rel, node


def _opts_in(node: ast.Call) -> bool:
    return any(kw.arg == "_wire_safe_message" for kw in node.keywords)


def _iter_unopted_literal_sites() -> list[tuple[str, int, str]]:
    """Yield (relative_path, lineno, message_prefix) for each unopted literal site."""
    violations: list[tuple[str, int, str]] = []
    for rel, node in _iter_target_calls():
        if _opts_in(node) or not _is_bare_string_literal_message(node):
            continue
        expr = _message_expr(node)
        assert isinstance(expr, ast.Constant)
        violations.append((rel, node.lineno, expr.value[:60]))
    return violations


def _iter_unaudited_interpolated_opt_ins() -> list[tuple[str, int]]:
    """Yield each INTERPOLATED message that opts in without an audit entry."""
    return [
        (rel, node.lineno)
        for rel, node in _iter_target_calls()
        if _opts_in(node)
        and _message_expr(node) is not None
        and not _is_bare_string_literal_message(node)
        and (rel, node.lineno) not in AUDITED_INTERPOLATED_OPT_INS
    ]


def test_static_validation_messages_opt_in_to_the_wire():
    """A bare literal message must carry ``_wire_safe_message=True``.

    Fails loudly with each offending site so the fix is a one-line edit at a named
    location, not a hunt.
    """
    violations = _iter_unopted_literal_sites()
    unexpected = [(f, ln, msg) for f, ln, msg in violations if (f, ln) not in KNOWN_VIOLATIONS]

    assert not unexpected, (
        "Bare static validation message(s) will be scrubbed off the wire for no security "
        "benefit — a literal interpolates nothing, so there is nothing to leak.\n"
        "Add `_wire_safe_message=True` to each site below:\n"
        + "\n".join(f"  {f}:{ln}  {msg!r}" for f, ln, msg in unexpected)
    )


def test_interpolated_opt_ins_match_the_audit_list_exactly():
    """An INTERPOLATED message may reach the wire only with an audit entry, and every
    audit entry must still point at one.

    This is the direction that can actually leak, and the guard originally checked only
    the other one. A bare literal interpolates nothing, so opting it in is safe by
    construction; an f-string can carry whatever is in scope — an adapter response, a
    connection string, another principal's data — and ``_wire_safe_message=True`` on it
    is an unreviewed assertion that it does not.

    Exact-match rather than one-directional, so both failure modes are caught by one
    assertion:

    * a NEW interpolated opt-in with no audit entry — trace every interpolated value to
      buyer-supplied data or a sanitized projection before adding it;
    * a STALE entry whose site was deleted, reworded to a literal, or had its opt-in
      removed — a licence nobody is using, which left in place silently pre-approves
      whatever later lands on that line.
    """
    live = {
        (rel, node.lineno)
        for rel, node in _iter_target_calls()
        if _opts_in(node) and _message_expr(node) is not None and not _is_bare_string_literal_message(node)
    }

    assert_violations_match_allowlist(
        live,
        set(AUDITED_INTERPOLATED_OPT_INS),
        fix_hint=(
            "An f-string reaching the buyer wire can carry adapter/DB/seller internals. "
            "Trace every interpolated value to the buyer's own request data or a sanitized "
            "projection, then record that reason in AUDITED_INTERPOLATED_OPT_INS."
        ),
    )


def test_guard_detects_a_bare_literal_site():
    """The detector itself must fire — otherwise the guard above passes vacuously.

    Pins the discrimination the guard depends on: a bare literal without the opt-in
    is caught, the same literal WITH the opt-in is not, and an f-string is ignored
    either way (interpolated messages are out of scope by design, not by accident).
    """
    caught = ast.parse('AdCPValidationError("media_buy_id is required")').body[0].value
    opted = ast.parse('AdCPValidationError("x", _wire_safe_message=True)').body[0].value
    interpolated = ast.parse('AdCPValidationError(f"rejected {value}")').body[0].value

    assert _is_bare_string_literal_message(caught)
    assert _is_bare_string_literal_message(opted)  # literal, but the opt-in kwarg exempts it
    assert not any(kw.arg == "_wire_safe_message" for kw in caught.keywords)
    assert any(kw.arg == "_wire_safe_message" for kw in opted.keywords)
    assert not _is_bare_string_literal_message(interpolated)

    # The kwarg form must be visible to both directions — reading only args[0] made
    # `AdCPValidationError(message="...")` invisible to the guard entirely.
    kwarg_literal = ast.parse('AdCPValidationError(message="media_buy_id is required")').body[0].value
    kwarg_interpolated = ast.parse('AdCPValidationError(message=f"rejected {value}")').body[0].value
    assert _is_bare_string_literal_message(kwarg_literal)
    assert not _is_bare_string_literal_message(kwarg_interpolated)
    assert _message_expr(kwarg_interpolated) is not None

    # And the leak direction: an interpolated message WITH the opt-in is what the second
    # detector must see. Detected structurally here; the scan applies the audit list.
    interpolated_opt_in = ast.parse('AdCPValidationError(f"leaked {secret}", _wire_safe_message=True)').body[0].value
    assert _opts_in(interpolated_opt_in)
    assert not _is_bare_string_literal_message(interpolated_opt_in)
