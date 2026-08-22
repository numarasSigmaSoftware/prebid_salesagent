"""Guard: a media buy's ``status`` is only ever written inside MediaBuyRepository.

The AdCP ``revision`` counter and the write-once ``confirmed_at`` stamp are both
produced by the repository's transition seams (``apply_status_transition``,
``apply_computed_status_transition``, ``update_status``, ``update_fields``). A raw
``media_buy.status = ...`` assignment anywhere else changes buyer-visible state
while leaving the buyer's optimistic-concurrency token frozen and, on a
seller-confirmed status, leaves a committed buy with no confirmation instant.

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
* A row is recognised by the ASSIGNMENT TARGET's name (``media_buy``, ``mb``,
  ``buy``, ``*_buy``, ``*_media_buy``). A row bound to some other name would slip
  past, so this guard cannot prove the absence of every raw write — which is why
  it also asserts that it actually reached each of the four modules that hold and
  transition a media buy, so the scan cannot silently shrink to nothing.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

# The one module allowed to assign a media buy's status: every seam lives here.
REPOSITORY_FILE = "src/core/database/repositories/media_buy.py"

# The production modules that load a MediaBuy row and transition it — six wired
# writes across four modules (operations.py carries approve AND reject; workflows.py
# the early-return and adapter-success arms). Every one must be reached by the scan
# below — if a module is moved or renamed and the list is not updated, the
# reachability test fails rather than quietly passing.
WIRED_TRANSITION_MODULES = (
    "src/services/media_buy_status_scheduler.py",
    "src/admin/blueprints/operations.py",
    "src/admin/blueprints/creatives.py",
    "src/admin/blueprints/workflows.py",
)

# Names an in-scope MediaBuy row is bound to across the wired call sites.
_ROW_NAMES = {"media_buy", "mb", "buy", "media_buy_row"}
_ROW_NAME_SUFFIXES = ("_buy", "_media_buy")


def _is_media_buy_row(node: ast.expr) -> bool:
    """True when ``node`` names something that reads as a MediaBuy ORM row."""
    if isinstance(node, ast.Name):
        name = node.id
    elif isinstance(node, ast.Attribute):
        name = node.attr
    else:
        return False
    return name in _ROW_NAMES or name.endswith(_ROW_NAME_SUFFIXES)


def _production_modules() -> list[Path]:
    """Every production ``.py`` under ``src/`` — test helpers shipped there excluded."""
    return [path for path in sorted(SRC.rglob("*.py")) if "tests" not in path.relative_to(ROOT).parts]


def _find_raw_status_writes() -> list[tuple[str, int, str]]:
    """Find ``<media buy>.status = ...`` assignments outside the repository.

    Returns ``(relative_path, line_number, target_name)``.
    """
    violations: list[tuple[str, int, str]] = []
    for path in _production_modules():
        rel_path = path.relative_to(ROOT).as_posix()
        if rel_path == REPOSITORY_FILE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign | ast.AugAssign):
                targets = [node.target]
            else:
                continue
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "status" and _is_media_buy_row(target.value):
                    violations.append((rel_path, target.lineno, ast.unparse(target)))
    return violations


class TestMediaBuyStatusWritesGoThroughTheRepository:
    """Only MediaBuyRepository may write a media buy's status."""

    @pytest.mark.arch_guard
    def test_no_raw_media_buy_status_assignment_in_production(self):
        """A raw ``.status =`` write bypasses the revision bump and confirmation stamp."""
        violations = _find_raw_status_writes()

        assert not violations, (
            "Media buy status written outside MediaBuyRepository — the write skips the "
            "AdCP revision bump and the write-once confirmed_at stamp, so the buyer sees "
            "the new state carrying a stale concurrency token:\n"
            + "\n".join(f"  {path}:{line}  {expr} = ..." for path, line, expr in violations)
            + "\n\nRoute the transition through MediaBuyRepository.apply_status_transition "
            "(fixed target) or .apply_computed_status_transition (target derived from the "
            "refreshed row). Do not add an allowlist entry."
        )

    @pytest.mark.arch_guard
    def test_the_scan_reaches_every_module_that_transitions_a_media_buy(self):
        """The guard is only meaningful if it actually reads the wired modules.

        A file-set that silently stops matching (a rename, a move, a changed
        ``rglob``) would make the test above pass by scanning nothing.
        """
        scanned = {path.relative_to(ROOT).as_posix() for path in _production_modules()}

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
