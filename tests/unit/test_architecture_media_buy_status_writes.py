"""Guard: a media buy's lifecycle stamps are only written inside MediaBuyRepository.

Five fields are covered: ``status``, ``approved_at``, ``approved_by``,
``confirmed_at`` and ``revision``.

The AdCP ``revision`` counter and the write-once ``confirmed_at`` stamp are both
produced by the repository's transition seams (``apply_status_transition``,
``apply_computed_status_transition``, ``update_status``, ``update_fields``). A raw
``media_buy.status = ...`` assignment anywhere else changes buyer-visible state
while leaving the buyer's optimistic-concurrency token frozen and, on a
seller-confirmed status, leaves a committed buy with no confirmation instant.

``approved_at`` is covered for the same reason one level down: it is the instant
:meth:`MediaBuyRepository._stamp_confirmation_if_needed` prefers when back-filling
``confirmed_at``, so a raw approval stamp writes an input to the confirmation
contract from outside the seam that owns it. ``approved_by`` is covered because it
is the OTHER half of the same stamp — ``MediaBuyRepository.stamp_approval`` writes
both, and a guard that watched only one half would let a raw write reintroduce the
split the seam exists to prevent. ``confirmed_at`` itself is covered so the
write-once rule cannot be re-implemented (and mis-implemented) at a call site.
``revision`` is covered because a raw assignment there is the one write that can
move the buyer's concurrency token BACKWARDS or collapse two bumps onto one value —
the counter is only monotonic while every write is the repository's server-side
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
  landed in (see :func:`_media_buy_row_names`). Renaming a local cannot hide a
  write: the name is only in scope because the module itself bound it to a
  MediaBuy.

PROVENANCE IS TYPED, NOT SPELLED (this is the third build of this guard; the first
two shipped blind, each in a different direction):

* Build 1 matched the assignment target's NAME against a fixed list (``media_buy``,
  ``mb``, ``buy``, ``*_buy``). A raw write bound to ``mb_obj`` in
  ``src/core/tools/creatives/_assignments.py`` sat outside that list and the guard
  reported the module clean.
* Build 2 replaced that with a substring test on the CALL: any identifier in the
  call chain containing ``media_buy`` produced a row. That is blind in BOTH
  directions and shipped both errors:
  - FALSE NEGATIVE, the reason this build exists: ``src/admin/blueprints/operations.py``
    binds its rows as ``media_buy = approve_repo.get_by_id(...)``. Nothing in
    ``approve_repo.get_by_id`` carries the marker, so the module resolved ZERO rows
    and every lifecycle write in it was invisible. Injecting a raw
    ``media_buy.status = "rejected"`` there left the guard green.
  - FALSE POSITIVE: ``success, error_msg = execute_approved_media_buy(...)`` bound a
    bool and a str as media-buy rows, and ``adapter.get_media_buy_delivery(...)``
    bound a delivery response.
* This build resolves a producer by its RETURN ANNOTATION
  (:func:`_media_buy_returning_callables`, derived from ``src/`` itself) combined
  with a media-buy RECEIVER (:func:`_is_media_buy_source`: a local bound to
  ``MediaBuyRepository(...)``, or a ``uow.media_buys`` accessor). So
  ``approve_repo.get_by_id`` produces a row on the strength of its declared
  ``-> MediaBuy | None``, while ``execute_approved_media_buy`` (``-> tuple[bool,
  str | None]``) and ``get_media_buy_delivery`` do not, and ``uow.creatives.get_by_id``
  does not despite sharing a method name. Annotations are therefore load-bearing
  here, not merely a documented seed — the docstring of build 2 claimed
  "constructions and annotations" while the walk never consulted a return
  annotation at all.

THE REACHABILITY ORACLE is what stops build 4 from being needed. It is not enough
to assert the wired modules are in the scanned FILE SET — build 2 passed that test
while resolving zero rows in one of them. ``test_the_matcher_resolves_a_row_in_every_wired_module``
asserts the matcher resolves AT LEAST ONE media-buy row in EACH wired module, and
``WIRED_TRANSITION_MODULES`` is itself checked against the set derived from the
source (every module that calls a media-buy seam), so deleting an awkward module
from the list reddens instead of silently narrowing the guard.
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
# the confirmation stamp; ``approved_at``/``approved_by`` are the two halves of the
# approval stamp the confirmation back-fills from; ``confirmed_at`` is the stamp
# itself; ``revision`` is the buyer's concurrency token.
GUARDED_FIELDS = ("status", "approved_at", "approved_by", "confirmed_at", "revision")

# The production modules that load a MediaBuy row and transition it. Not maintained
# by hand alone: :func:`_modules_calling_a_media_buy_seam` derives the same set from
# the source, and the reachability test below asserts the two agree, so a module
# cannot be quietly dropped from the guard's obligations.
WIRED_TRANSITION_MODULES = (
    "src/admin/blueprints/creatives.py",
    "src/admin/blueprints/operations.py",
    "src/admin/blueprints/workflows.py",
    "src/core/tools/creatives/_assignments.py",
    "src/core/tools/media_buy_create.py",
    "src/services/media_buy_status_scheduler.py",
)

# The ORM class whose rows this guard protects.
MODEL_NAME = "MediaBuy"
# The repository that owns them, and the UoW attribute that exposes it.
REPOSITORY_CLASS = "MediaBuyRepository"
REPOSITORY_ACCESSOR = "media_buys"
_PRODUCER_MARKER = "media_buy"

# Mutating seams that MediaBuyRepository alone declares. Used to DERIVE which
# modules transition a media buy, so WIRED_TRANSITION_MODULES is checked against
# the code rather than trusted.
TRANSITION_SEAMS = frozenset(
    {
        "apply_status_transition",
        "apply_computed_status_transition",
        "stamp_approval",
        "update_status",
        "update_fields",
        "bump_revision",
    }
)


def _base_name(node: ast.expr) -> str | None:
    """The root ``Name`` of an attribute/subscript chain, if there is one."""
    while isinstance(node, ast.Attribute | ast.Subscript):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _annotation_names_the_model(annotation: ast.expr | None) -> bool:
    """True for ``MediaBuy``, ``MediaBuy | None``, ``list[MediaBuy]``, ``"MediaBuy"``.

    A string annotation is PARSED and walked rather than substring-matched, so
    ``-> "CreateMediaBuySuccess"`` (a wire schema, not a row) does not read as the
    model.
    """
    if annotation is None:
        return False
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name) and node.id == MODEL_NAME:
            return True
        if isinstance(node, ast.Attribute) and node.attr == MODEL_NAME:
            return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                inner = ast.parse(node.value, mode="eval").body
            except SyntaxError:
                continue
            if _annotation_names_the_model(inner):
                return True
    return False


def _media_buy_returning_callables() -> frozenset[str]:
    """Names of ``src/`` callables whose RETURN ANNOTATION names a MediaBuy.

    This is the type seed. ``MediaBuyRepository.get_by_id`` earns producer status
    from its declared ``-> MediaBuy | None``, not from any identifier spelling, so
    a row bound as ``media_buy = approve_repo.get_by_id(...)`` is recognised while
    ``execute_approved_media_buy`` (``-> tuple[bool, str | None]``) is not.

    Resolution is by unqualified name, which is an over-approximation shared with
    other repositories' identically named methods — :func:`_is_media_buy_source`
    supplies the receiver half that narrows it back.
    """
    names: set[str] = set()
    for path in _production_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _annotation_names_the_model(node.returns):
                names.add(node.name)
    return frozenset(names)


def _media_buy_repository_handles(tree: ast.AST) -> set[str]:
    """Names this module binds to a ``MediaBuyRepository(...)`` instance.

    The receiver half of the type seed: ``approve_repo = MediaBuyRepository(session,
    tenant_id)`` makes every ``approve_repo.<row-returning method>()`` call a
    producer, whatever the local is called.
    """
    handles: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and _annotation_is_the_repository(node.annotation):
            base = _base_name(node.target)
            if base is not None:
                handles.add(base)
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        constructs = isinstance(value, ast.Call) and (
            (isinstance(value.func, ast.Name) and value.func.id == REPOSITORY_CLASS)
            or (isinstance(value.func, ast.Attribute) and value.func.attr == REPOSITORY_CLASS)
        )
        if constructs:
            for target in node.targets:
                base = _base_name(target)
                if base is not None:
                    handles.add(base)
    return handles


def _annotation_is_the_repository(annotation: ast.expr | None) -> bool:
    """True when an annotation names ``MediaBuyRepository``."""
    if annotation is None:
        return False
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name) and node.id == REPOSITORY_CLASS:
            return True
        if isinstance(node, ast.Attribute) and node.attr == REPOSITORY_CLASS:
            return True
    return False


def _names_the_repository(node: ast.expr) -> bool:
    """True for ``MediaBuyRepository`` as a bare name or an attribute of a module."""
    if isinstance(node, ast.Name):
        return node.id == REPOSITORY_CLASS
    if isinstance(node, ast.Attribute):
        return node.attr == REPOSITORY_CLASS
    return False


def _is_media_buy_source(func: ast.expr, handles: set[str]) -> bool:
    """True when a call's RECEIVER is the media-buy repository.

    Four shapes, all of which occur in production:

    * a local bound to ``MediaBuyRepository(...)`` — ``approve_repo.get_by_id(...)``
      (admin approve route);
    * the UoW accessor that exposes it — ``uow.media_buys.get_by_id(...)``;
    * an inline construction — ``MediaBuyRepository(session, tid).apply_...(...)``
      (the cross-tenant status scheduler, which has no tenant to bind up front);
    * a class-qualified static call — ``MediaBuyRepository.get_all_by_statuses(...)``
      (the same scheduler's sweep).

    This is what keeps ``uow.creatives.get_by_id`` — same method name, different
    entity — out of scope.
    """
    if not isinstance(func, ast.Attribute):
        return False
    if _base_name(func) in handles:
        return True
    for part in ast.walk(func):
        if isinstance(part, ast.Attribute) and part.attr == REPOSITORY_ACCESSOR:
            return True
        if isinstance(part, ast.Call) and _names_the_repository(part.func):
            return True
        if _names_the_repository(part) and part is not func:
            return True
    return False


def _callee_name(func: ast.expr) -> str | None:
    """The final identifier of a call target: ``a.b.c()`` -> ``c``, ``f()`` -> ``f``."""
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _call_yields_media_buy(call: ast.Call, handles: set[str], returning: frozenset[str]) -> bool:
    """True when this call produces a MediaBuy row.

    Three ways, all typed rather than spelled: constructing the model, calling a
    row-returning method ON the media-buy repository, and calling a row-returning
    callable whose own name says it deals in media buys (``uow.assignments.
    find_package_with_media_buy`` -> ``tuple[MediaPackage, MediaBuy] | None``, a
    producer that lives on another repository).
    """
    if isinstance(call.func, ast.Name) and call.func.id == MODEL_NAME:
        return True
    callee = _callee_name(call.func)
    if callee is None or callee not in returning:
        return False
    return _is_media_buy_source(call.func, handles) or _PRODUCER_MARKER in callee


def _yields_media_buy(
    value: ast.expr | None,
    known: set[str],
    handles: set[str],
    returning: frozenset[str],
) -> bool:
    """True when this expression evaluates to (or contains) a MediaBuy row."""
    if value is None:
        return False
    if isinstance(value, ast.Call):
        if _call_yields_media_buy(value, handles, returning):
            return True
        return _base_name(value.func) in known
    if isinstance(value, ast.Name):
        return value.id in known
    if isinstance(value, ast.Attribute | ast.Subscript):
        return _base_name(value) in known
    if isinstance(value, ast.Tuple | ast.List | ast.Set):
        return any(_yields_media_buy(element, known, handles, returning) for element in value.elts)
    if isinstance(value, ast.BoolOp):
        return any(_yields_media_buy(operand, known, handles, returning) for operand in value.values)
    if isinstance(value, ast.IfExp):
        return _yields_media_buy(value.body, known, handles, returning) or _yields_media_buy(
            value.orelse, known, handles, returning
        )
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


def _media_buy_row_names(tree: ast.AST, returning: frozenset[str]) -> set[str]:
    """Names this module binds to a MediaBuy row, resolved by provenance.

    Seeded from parameter/variable annotations and model constructions, then
    propagated to a fixpoint through assignments, ``for`` targets and ``with ... as``
    bindings, so a row that changes hands several times stays recognised under every
    new name.
    """
    handles = _media_buy_repository_handles(tree)
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
                if _yields_media_buy(node.value, known, handles, returning):
                    for target in node.targets:
                        grew |= _bind(target, known)
            elif isinstance(node, ast.AnnAssign):
                if _yields_media_buy(node.value, known, handles, returning):
                    grew |= _bind(node.target, known)
            elif isinstance(node, ast.For | ast.AsyncFor):
                if _yields_media_buy(node.iter, known, handles, returning):
                    grew |= _bind(node.target, known)
            elif isinstance(node, ast.withitem):
                if node.optional_vars is not None and _yields_media_buy(node.context_expr, known, handles, returning):
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


def _raw_lifecycle_writes(
    rel_path: str, source: str, returning: frozenset[str] | None = None
) -> list[tuple[str, int, str]]:
    """Find ``<media buy>.<guarded field> = ...`` assignments in one module's source.

    Takes the source rather than a path so the controls below can drive the REAL
    scan with a synthetic module: a matcher that recognises nothing would otherwise
    report every real file clean and look like a passing guard.
    """
    resolved = _media_buy_returning_callables() if returning is None else returning
    tree = ast.parse(source, filename=rel_path)
    known = _media_buy_row_names(tree, resolved)
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
    returning = _media_buy_returning_callables()
    violations: list[tuple[str, int, str]] = []
    for path in _production_modules():
        rel_path = path.relative_to(ROOT).as_posix()
        if rel_path == REPOSITORY_FILE:
            continue
        violations.extend(_raw_lifecycle_writes(rel_path, path.read_text(encoding="utf-8"), returning))
    return violations


def _modules_calling_a_media_buy_seam() -> set[str]:
    """Production modules that call a MediaBuyRepository mutation seam.

    Derived from the source so ``WIRED_TRANSITION_MODULES`` cannot be trimmed to
    dodge the reachability obligation. The receiver test is what separates
    ``uow.media_buys.update_status`` from the identically named seams on the
    account and task repositories.
    """
    modules: set[str] = set()
    for path in _production_modules():
        rel_path = path.relative_to(ROOT).as_posix()
        if rel_path == REPOSITORY_FILE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
        handles = _media_buy_repository_handles(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in TRANSITION_SEAMS
                and _is_media_buy_source(node.func, handles)
            ):
                modules.add(rel_path)
                break
    return modules


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

# The shape that made this build necessary: the row comes off a repository handle
# whose NAME says nothing about media buys. Build 2 resolved zero rows here.
_REPOSITORY_HANDLE_SOURCE = """
def approve(db_session, tenant_id, media_buy_id, user_email):
    approve_repo = MediaBuyRepository(db_session, tenant_id)
    media_buy = approve_repo.get_by_id(media_buy_id)
    media_buy.status = "rejected"
    media_buy.approved_by = user_email
"""

# The same field names on a DIFFERENT entity must stay out of scope, or the guard
# would be unusable and get weakened back. Nothing here binds a media buy:
# ``uow.creatives.get_by_id`` shares a method name with the media-buy repository
# but not its receiver, ``execute_approved_media_buy`` returns ``(bool, str | None)``
# despite carrying the marker in its name, and a delivery response is not a row.
_OTHER_ENTITY_SOURCE = """
def review(uow, session, adapter, media_buy_id, tenant_id):
    creative = uow.creatives.get_by_id("c")
    creative.status = "approved"
    creative.approved_at = None
    creative.approved_by = "admin"
    sync_job = SyncJob(sync_id="s")
    sync_job.status = "completed"
    success, error_msg = execute_approved_media_buy(media_buy_id, tenant_id)
    success.status = "ignored"
    error_msg.revision = 3
    delivery_response = adapter.get_media_buy_delivery(media_buy_id)
    delivery_response.status = "delivering"
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
    def test_the_matcher_reports_a_raw_write_behind_a_repository_handle(self):
        """Positive control for the hole this build closes.

        ``approve_repo`` carries no media-buy marker, so the substring matcher this
        replaces resolved no rows in ``src/admin/blueprints/operations.py`` and
        every lifecycle write there was invisible. The row is recognised here only
        because ``MediaBuyRepository.get_by_id`` DECLARES ``-> MediaBuy | None``.
        """
        found = _raw_lifecycle_writes("synthetic_handle.py", _REPOSITORY_HANDLE_SOURCE)
        reported = {expr for _path, _line, expr in found}

        assert reported == {"media_buy.status", "media_buy.approved_by"}, (
            "the scan did not report a raw write on a row produced by a MediaBuyRepository "
            f"handle (got {sorted(reported)}) — provenance must follow the repository TYPE, "
            "not the spelling of the variable holding it"
        )

    @pytest.mark.arch_guard
    def test_the_matcher_leaves_other_entities_alone(self):
        """Symmetric control: false positives in both of the known directions.

        A matcher that flagged every ``.status =`` in ``src/`` would report ~50
        unrelated writes, and the pressure would be to weaken the guard rather
        than fix a real violation. This pins the two shapes the substring matcher
        got wrong in the OTHER direction as well — a marker-bearing callable that
        returns ``(bool, str | None)``, and an adapter delivery response.
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

        assert set(GUARDED_FIELDS) == {"status", "approved_at", "approved_by", "confirmed_at", "revision"}, (
            "the guarded field set changed — a media buy's status, both halves of its "
            "approval stamp, its confirmation instant and its revision token all belong "
            f"to the repository; got {GUARDED_FIELDS}"
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

    @pytest.mark.arch_guard
    def test_the_wired_module_list_matches_the_modules_that_call_a_seam(self):
        """``WIRED_TRANSITION_MODULES`` is derived from the code, not trusted.

        Without this, the reachability oracle below could be satisfied by deleting
        the module the matcher cannot see — turning a red guard green by narrowing
        its obligations instead of fixing the matcher.
        """
        derived = _modules_calling_a_media_buy_seam()

        assert set(WIRED_TRANSITION_MODULES) == derived, (
            "WIRED_TRANSITION_MODULES has drifted from the modules that actually call a "
            f"MediaBuyRepository seam.\n  missing from the list: {sorted(derived - set(WIRED_TRANSITION_MODULES))}"
            f"\n  listed but no longer calling a seam: {sorted(set(WIRED_TRANSITION_MODULES) - derived)}"
        )

    @pytest.mark.arch_guard
    def test_the_matcher_resolves_a_row_in_every_wired_module(self):
        """THE oracle: the matcher must SEE a media-buy row in each wired module.

        Two previous builds of this guard shipped blind because the only
        reachability check asserted the wired modules were in the scanned FILE SET.
        A file can be read, parsed, and yield ZERO recognised rows — which is
        exactly what happened to ``src/admin/blueprints/operations.py`` under the
        substring matcher, making every lifecycle write in it invisible while the
        guard reported four passing tests.

        A module that transitions a media buy must, by definition, be holding one.
        If the matcher cannot name a single row there, it cannot see a raw write
        there either, and this test is what says so out loud.
        """
        returning = _media_buy_returning_callables()
        blind: dict[str, str] = {}
        for module in WIRED_TRANSITION_MODULES:
            path = ROOT / module
            assert path.exists(), f"wired module {module} does not exist"
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
            if not _media_buy_row_names(tree, returning):
                blind[module] = "no media-buy row resolved"

        assert not blind, (
            "the provenance matcher resolves NO media-buy row in these modules, so every "
            f"lifecycle write in them is invisible to this guard: {sorted(blind)}. "
            "The module calls a MediaBuyRepository seam, so it holds a row — teach the "
            "matcher how that row is produced (see _call_yields_media_buy). Do NOT remove "
            "the module from WIRED_TRANSITION_MODULES: the list is checked against the "
            "code by test_the_wired_module_list_matches_the_modules_that_call_a_seam."
        )
