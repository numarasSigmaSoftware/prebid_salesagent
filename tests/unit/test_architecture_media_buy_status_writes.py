"""Guard: a media buy's lifecycle stamps are only written inside MediaBuyRepository.

Four fields are covered: ``status``, ``approved_at``, ``confirmed_at`` and ``revision``.

The AdCP ``revision`` counter and the write-once ``confirmed_at`` stamp are both
produced by the repository's transition seams (``apply_status_transition``,
``apply_computed_status_transition``, ``update_status``, ``update_fields``). A raw
``media_buy.status = ...`` assignment anywhere else changes buyer-visible state
while leaving the buyer's optimistic-concurrency token frozen and, on a
seller-confirmed status, leaves a committed buy with no confirmation instant.

``approved_at`` is covered for the same reason one level down: it is the instant
:meth:`MediaBuyRepository._stamp_confirmation_if_needed` prefers when back-filling
``confirmed_at``, so a raw approval stamp writes an input to the confirmation
contract from outside the seam that owns it. ``confirmed_at`` itself is covered so
the write-once rule cannot be re-implemented (and mis-implemented) at a call site;
``MediaBuyRepository.stamp_approval`` is the sanctioned approval seam. ``revision``
is covered because a raw assignment there is the one write that can move the
buyer's concurrency token BACKWARDS or collapse two bumps onto one value — the
counter is only monotonic while every write is the repository's server-side
``coalesce(revision, 0) + 1`` expression.

That is exactly what happened at every wired site before it was routed through a
seam, so the invariant the counter's docstrings state ("bumped on every
successful mutation", "no path can leave a committed buy without a confirmation
instant") needs a mechanism that reddens when it breaks — not just prose. This is
that mechanism: reintroduce a raw ``.status =`` write in any production module and
this test fails.

There is deliberately NO allowlist. Every production write site routes through a
seam today; a new violation is a bug to fix at the call site, not an entry to add.

SCOPE (this is a shape check on the assignment, not a type check on the object):
* Only ``src/`` is scanned, and within it only production modules — a ``tests``
  path segment is excluded, because ``src/admin/tests/conftest.py`` builds a
  ``Mock()`` media buy and setting ``.status`` on a mock writes no row. That is a
  scan boundary (production code), not a per-violation exemption.
* The repository module itself is the sanctioned home for the write.
* A row is recognised by PROVENANCE, never by the spelling of the variable it
  landed in (see :func:`_media_buy_row_names`). An earlier version of this guard
  matched the assignment target's NAME against a fixed list (``media_buy``,
  ``mb``, ``buy``, ``*_buy``); a raw write bound to ``mb_obj`` in
  ``src/core/tools/creatives/_assignments.py`` sat outside that list and the guard
  reported the module clean. Renaming a local can no longer hide a write: the name
  is only in scope because the module itself bound it to a MediaBuy.
* Provenance also keeps ``creative.approved_at`` (a Creative row, a different
  entity with its own approval semantics) and ``sync_job.status`` out of scope —
  nothing in those modules binds those names to a media buy.
* The matcher is an over-approximation of dataflow, so it can still miss an
  exotic binding. It is pinned in both directions below: a positive control feeds
  a synthetic raw write through the real scan and requires it to be reported, and
  the module set is pinned so the scan cannot silently shrink to nothing.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

# The one module allowed to assign these fields on a media buy: every seam lives here.
REPOSITORY_FILE = "src/core/database/repositories/media_buy.py"

# The lifecycle fields the repository owns. ``status`` drives the revision bump and
# the confirmation stamp; ``approved_at`` is the instant that stamp back-fills from;
# ``confirmed_at`` is the stamp itself; ``revision`` is the buyer's concurrency token.
GUARDED_FIELDS = ("status", "approved_at", "confirmed_at", "revision")

# The production modules that load a MediaBuy row and transition it — every one must
# be reached by the scan below; if a module is moved or renamed and the list is not
# updated, the reachability test fails rather than quietly passing.
WIRED_TRANSITION_MODULES = (
    "src/services/media_buy_status_scheduler.py",
    "src/admin/blueprints/operations.py",
    "src/admin/blueprints/creatives.py",
    "src/admin/blueprints/workflows.py",
    "src/core/tools/creatives/_assignments.py",
)

# The ORM class whose rows this guard protects. Provenance is traced back to this
# name (a construction, an annotation) or to a producer whose own name says it
# returns one — never to the name the caller happened to give the result.
MODEL_NAME = "MediaBuy"
_PRODUCER_MARKER = "media_buy"


def _names_a_media_buy_producer(node: ast.expr) -> bool:
    """True when any identifier in a call target says it deals in media buys.

    ``uow.media_buys.get_by_id`` and ``repo.find_package_with_media_buy`` both
    qualify. Only whether SOME identifier carries the marker matters, so the
    chain is not reassembled into a dotted string.
    """
    return any(
        _PRODUCER_MARKER in (part.attr if isinstance(part, ast.Attribute) else part.id)
        for part in ast.walk(node)
        if isinstance(part, ast.Attribute | ast.Name)
    )


def _base_name(node: ast.expr) -> str | None:
    """The root ``Name`` of an attribute/subscript chain, if there is one."""
    while isinstance(node, ast.Attribute | ast.Subscript):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _annotation_names_the_model(annotation: ast.expr | None) -> bool:
    """True for ``MediaBuy``, ``MediaBuy | None``, ``"MediaBuy"``, ``models.MediaBuy``."""
    if annotation is None:
        return False
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name) and node.id == MODEL_NAME:
            return True
        if isinstance(node, ast.Attribute) and node.attr == MODEL_NAME:
            return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and MODEL_NAME in node.value:
            return True
    return False


def _yields_media_buy(value: ast.expr | None, known: set[str]) -> bool:
    """True when this expression evaluates to (or contains) a MediaBuy row.

    Three provenance sources, none of which is the receiving variable's name:
    constructing the model, calling a producer that says ``media_buy`` in its own
    name (``uow.media_buys.get_by_id``, ``find_package_with_media_buy``), and
    re-reading something already known to hold one.
    """
    if value is None:
        return False
    if isinstance(value, ast.Call):
        if isinstance(value.func, ast.Name) and value.func.id == MODEL_NAME:
            return True
        if _names_a_media_buy_producer(value.func):
            return True
        return _base_name(value.func) in known
    if isinstance(value, ast.Name):
        return value.id in known
    if isinstance(value, ast.Attribute | ast.Subscript):
        return _base_name(value) in known
    if isinstance(value, ast.Tuple | ast.List | ast.Set):
        return any(_yields_media_buy(element, known) for element in value.elts)
    if isinstance(value, ast.BoolOp):
        return any(_yields_media_buy(operand, known) for operand in value.values)
    if isinstance(value, ast.IfExp):
        return _yields_media_buy(value.body, known) or _yields_media_buy(value.orelse, known)
    return False


def _bind(target: ast.expr, known: set[str]) -> bool:
    """Record every name this assignment target binds; True if the set grew.

    Tuple targets bind all their elements and a subscript/attribute target binds
    its container, both deliberately over-approximating: a buy stashed in a dict
    and pulled back out in a later loop (the shape that hid a raw write from the
    name-matching version of this guard) stays traceable.
    """
    grew = False
    stack = [target]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Name):
            if node.id not in known:
                known.add(node.id)
                grew = True
        elif isinstance(node, ast.Tuple | ast.List):
            stack.extend(node.elts)
        elif isinstance(node, ast.Attribute | ast.Subscript):
            base = _base_name(node)
            if base is not None and base not in known:
                known.add(base)
                grew = True
    return grew


def _media_buy_row_names(tree: ast.AST) -> set[str]:
    """Names this module binds to a MediaBuy row, resolved by provenance.

    Seeded from annotations and model constructions, then propagated to a fixpoint
    through assignments, ``for`` targets and ``with ... as`` bindings, so a row
    that changes hands several times stays recognised under every new name.
    """
    known: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and _annotation_names_the_model(node.annotation):
            _bind(node.target, known)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            arguments = node.args
            for argument in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]:
                if _annotation_names_the_model(argument.annotation):
                    known.add(argument.arg)

    grew = True
    while grew:
        grew = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                if _yields_media_buy(node.value, known):
                    for target in node.targets:
                        grew |= _bind(target, known)
            elif isinstance(node, ast.AnnAssign):
                if _yields_media_buy(node.value, known):
                    grew |= _bind(node.target, known)
            elif isinstance(node, ast.For | ast.AsyncFor):
                if _yields_media_buy(node.iter, known):
                    grew |= _bind(node.target, known)
            elif isinstance(node, ast.withitem):
                if node.optional_vars is not None and _yields_media_buy(node.context_expr, known):
                    grew |= _bind(node.optional_vars, known)
    return known


def _is_media_buy_row(node: ast.expr, known: set[str]) -> bool:
    """True when ``node`` reads a name this module bound to a MediaBuy row."""
    if isinstance(node, ast.Name):
        return node.id in known
    if isinstance(node, ast.Attribute):
        return node.attr in known or _base_name(node) in known
    return False


def _production_modules() -> list[Path]:
    """Every production ``.py`` under ``src/`` — test helpers shipped there excluded."""
    return [path for path in sorted(SRC.rglob("*.py")) if "tests" not in path.relative_to(ROOT).parts]


def _raw_lifecycle_writes(rel_path: str, source: str) -> list[tuple[str, int, str]]:
    """Find ``<media buy>.<guarded field> = ...`` assignments in one module's source.

    Takes the source rather than a path so the positive control below can drive
    the REAL scan with a synthetic module: a matcher that recognises nothing would
    otherwise report every real file clean and look like a passing guard.
    """
    tree = ast.parse(source, filename=rel_path)
    known = _media_buy_row_names(tree)
    violations: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign | ast.AugAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and target.attr in GUARDED_FIELDS
                and _is_media_buy_row(target.value, known)
            ):
                violations.append((rel_path, target.lineno, ast.unparse(target)))
    return violations


def _find_raw_lifecycle_writes() -> list[tuple[str, int, str]]:
    """Every raw lifecycle write in production, outside the repository module."""
    violations: list[tuple[str, int, str]] = []
    for path in _production_modules():
        rel_path = path.relative_to(ROOT).as_posix()
        if rel_path == REPOSITORY_FILE:
            continue
        violations.extend(_raw_lifecycle_writes(rel_path, path.read_text(encoding="utf-8")))
    return violations


# A raw write on a row that changes hands three times and ends up in a variable
# named nothing like a media buy — reduced from the shape that actually shipped in
# src/core/tools/creatives/_assignments.py and was reported clean by the
# name-matching version of this guard.
_RENAMED_ROW_SOURCE = """
def process(tenant, assignments):
    tracked = {}
    with CreativeUoW(tenant) as uow:
        for package_id in assignments:
            db_package, db_media_buy = uow.assignments.find_package_with_media_buy(package_id)
            tracked[db_package.media_buy_id] = db_media_buy
        for _key, mb_obj in tracked.items():
            mb_obj.status = "pending_creatives"
            mb_obj.revision = 7
"""

# The same field names on a DIFFERENT entity must stay out of scope, or the guard
# would be unusable and get weakened back. Nothing here binds a media buy.
_OTHER_ENTITY_SOURCE = """
def review(uow, session):
    creative = uow.creatives.get_by_id("c")
    creative.status = "approved"
    creative.approved_at = None
    sync_job = SyncJob(sync_id="s")
    sync_job.status = "completed"
"""


class TestMediaBuyStatusWritesGoThroughTheRepository:
    """Only MediaBuyRepository may write a media buy's lifecycle stamps."""

    @pytest.mark.arch_guard
    def test_no_raw_media_buy_lifecycle_assignment_in_production(self):
        """A raw write bypasses the revision bump and the confirmation stamp."""
        violations = _find_raw_lifecycle_writes()

        assert not violations, (
            "Media buy lifecycle state written outside MediaBuyRepository — the write "
            "skips the AdCP revision bump and the write-once confirmed_at stamp, so the "
            "buyer sees the new state carrying a stale concurrency token:\n"
            + "\n".join(f"  {path}:{line}  {expr} = ..." for path, line, expr in violations)
            + "\n\nRoute a status change through MediaBuyRepository.apply_status_transition "
            "(fixed target) or .apply_computed_status_transition (target derived from the "
            "refreshed row), and an approval stamp through .stamp_approval. Do not add an "
            "allowlist entry."
        )

    @pytest.mark.arch_guard
    def test_the_matcher_reports_a_raw_write_on_a_renamed_row(self):
        """Positive control: the scan must actually recognise a raw write.

        Without this, the guard above is unfalsifiable — a row matcher that
        returns False for everything reports every production file clean and the
        suite stays green. Drives the real ``_raw_lifecycle_writes`` with a
        synthetic module whose receiver is named ``mb_obj``, three re-bindings
        away from the repository call that produced it.
        """
        found = _raw_lifecycle_writes("synthetic_module.py", _RENAMED_ROW_SOURCE)
        reported = {expr for _path, _line, expr in found}

        assert reported == {"mb_obj.status", "mb_obj.revision"}, (
            "the scan did not report the raw lifecycle writes in the synthetic module "
            f"(got {sorted(reported)}) — a row is recognised by provenance, so renaming "
            "the variable it lands in must not hide the write"
        )

    @pytest.mark.arch_guard
    def test_the_matcher_leaves_other_entities_alone(self):
        """Symmetric control: Creative and SyncJob own their own status fields.

        A matcher that flagged every ``.status =`` in ``src/`` would report ~50
        unrelated writes, and the pressure would be to weaken the guard rather
        than fix a real violation.
        """
        found = _raw_lifecycle_writes("synthetic_other.py", _OTHER_ENTITY_SOURCE)

        assert found == [], (
            f"the scan claimed a media-buy write on another entity's row: {found} — "
            "nothing in that module binds a MediaBuy"
        )

    @pytest.mark.arch_guard
    def test_the_scan_reaches_every_module_that_transitions_a_media_buy(self):
        """The guard is only meaningful if it actually reads the wired modules.

        A file-set that silently stops matching (a rename, a move, a changed
        ``rglob``) would make the test above pass by scanning nothing. The same
        applies to the field set: a guard narrowed back to ``status`` alone would
        stop seeing the approval stamps and the concurrency token, so the covered
        fields are pinned here too.
        """
        scanned = {path.relative_to(ROOT).as_posix() for path in _production_modules()}

        assert set(GUARDED_FIELDS) == {"status", "approved_at", "confirmed_at", "revision"}, (
            "the guarded field set changed — a media buy's status, its approval instant, "
            f"its confirmation stamp and its revision token all belong to the repository; got {GUARDED_FIELDS}"
        )

        missing = [module for module in WIRED_TRANSITION_MODULES if module not in scanned]
        assert not missing, (
            f"the status-write guard did not reach {missing} — these modules hold a MediaBuy "
            "row and transition it, so a raw write there must be visible to the scan. Update "
            "WIRED_TRANSITION_MODULES if a module genuinely moved."
        )
        assert REPOSITORY_FILE in scanned, (
            f"{REPOSITORY_FILE} is not in the scanned set, so the guard's one exclusion no "
            "longer excludes anything real — the scan path is wrong."
        )
