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

from tests.unit._architecture_helpers import iter_call_expressions

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_DIR = REPO_ROOT / "src"

_TARGET_CLASSES = {"AdCPValidationError", "AdCPInvalidRequestError"}

# Empty by design. A new bare-literal raise site without the opt-in is a diagnostic
# regression, not debt to be parked here — add ``_wire_safe_message=True`` instead.
# Only add an entry if a literal message genuinely must be scrubbed, with the reason.
KNOWN_VIOLATIONS: set[tuple[str, int]] = set()


def _is_bare_string_literal_message(node: ast.Call) -> bool:
    """True iff the call's first positional arg is a plain string constant.

    An f-string is an ``ast.JoinedStr`` and a concatenation is an ``ast.BinOp``, so
    neither matches here — only a literal (including implicit adjacent-string
    concatenation, which the parser folds into one ``ast.Constant``).
    """
    if not node.args:
        return False
    first = node.args[0]
    return isinstance(first, ast.Constant) and isinstance(first.value, str)


def _iter_unopted_literal_sites() -> list[tuple[str, int, str]]:
    """Yield (relative_path, lineno, message_prefix) for each unopted literal site."""
    violations: list[tuple[str, int, str]] = []
    for path in sorted(SCAN_DIR.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - a syntax error fails elsewhere, loudly
            continue
        for cls_name in sorted(_TARGET_CLASSES):
            for node in iter_call_expressions(tree, cls_name):
                # iter_call_expressions also matches attribute calls (mod.AdCP...);
                # both forms are equally in scope for this guard.
                if any(kw.arg == "_wire_safe_message" for kw in node.keywords):
                    continue
                if not _is_bare_string_literal_message(node):
                    continue
                rel = str(path.relative_to(REPO_ROOT))
                violations.append((rel, node.lineno, node.args[0].value[:60]))
    return violations


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
