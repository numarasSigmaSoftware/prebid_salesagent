"""Guard: every scenario tag named in tests/bdd/conftest.py must exist in a feature file.

A tag set that names a scenario which no longer exists routes nothing. It reads as
live coverage — an xfail that is "protecting" something, an exclusion that is
"suppressing" something — while being inert, and nothing else in the suite says so:
the scenario is simply gone, so no test fails and no count changes.

This is the THIRD deadness mechanism found in these sets, and the first two were each
fixed one instance at a time before the class was closed:

1. Overlap with ``_TRANSPORT_INDEPENDENT_SCENARIO_TAGS`` — the transport-independent
   check returns before any e2e_rest gate is consulted, so the entry excludes nothing.
   (Hollowed out ``_NO_E2E_REST_TAGS``, then all eleven
   ``_UC004_E2E_WEBHOOK_INTERNAL_TAGS`` entries.)
2. Unreachable gate — ``_NO_REST_UC_TAG_PREFIXES`` strips E2E_REST from the transport
   list for a whole UC, so every ``is_e2e_rest``-gated block for it is dead regardless
   of membership. (A ~74-line UC-019 block.)
3. THIS: the tag names no scenario at all.

Mechanisms 1 and 2 are tracked for a proper AST-derived rewrite in #1923. Mechanism 3
is cheap to close completely and is closed here, over EVERY tag collection in the file
rather than the one that happened to be cited — which is the difference between fixing
an instance and fixing a class.

Exact matching matters: a substring search for ``T-UC-005-inv-049-1`` also matches
``@T-UC-005-inv-049-10``, so a naive grep reports a live tag for one that does not
exist. The trailing ``(?![\\w-])`` is what makes the check honest.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFTEST = REPO_ROOT / "tests" / "bdd" / "conftest.py"
FEATURES_DIR = REPO_ROOT / "tests" / "bdd" / "features"

TAG_PREFIX = "T-UC-"

# Collections whose members are deliberately PREFIXES rather than whole tags, so
# "does this tag exist" is the wrong question for them. Keyed by name with the
# reason, so adding one is a decision rather than an oversight.
PREFIX_COLLECTIONS = {
    "_NO_REST_UC_TAG_PREFIXES": "members are UC prefixes matched with str.startswith, not whole tags",
}


def _feature_texts() -> list[str]:
    return [p.read_text() for p in sorted(FEATURES_DIR.rglob("*.feature"))]


def _tag_is_defined(tag: str, texts: list[str]) -> bool:
    """True if ``@tag`` appears in a feature file as a WHOLE tag.

    ``(?![\\w-])`` prevents ``-049-1`` from matching ``@...-049-10``.
    """
    pattern = re.compile(r"@" + re.escape(tag) + r"(?![\w-])")
    return any(pattern.search(text) for text in texts)


INLINE_LABEL = "<inline gate / not in a named collection>"


def _is_tag_constant(node: ast.AST) -> bool:
    """A WHOLE scenario tag, as opposed to a prefix or a sentence that starts with one.

    Two exclusions, both structural rather than by enumeration:

    * A tag never contains whitespace. Several xfail reasons begin with the tag they
      describe ("T-UC-002-inv-015-6 create_media_buy harness wiring is tracked in
      #1652"), and reporting those as routed tags would make this guard cry wolf on
      prose.
    * A bare UC prefix ("T-UC-019", "T-UC-003-ext-") is matched with ``startswith``,
      so "does this exact tag exist" is the wrong question — the same reason
      ``PREFIX_COLLECTIONS`` exists for named collections. Detected at the use site
      below, since a prefix is only identifiable by how it is consumed.
    """
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith(TAG_PREFIX)
        and not any(ch.isspace() for ch in node.value)
    )


def _startswith_arguments(tree: ast.AST) -> set[int]:
    """Node ids of constants passed to ``.startswith(...)`` — prefixes, not tags."""
    return {
        id(arg)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "startswith"
        for arg in node.args
        if isinstance(arg, ast.Constant)
    }


def _tag_collections(source: str) -> dict[str, set[str]]:
    """EVERY scenario tag written anywhere in the file, labelled by where it sits.

    Enumerates tag CONSTANTS, not tag-collection SHAPES. The first version of this
    function modelled shapes — ``literal_eval`` of a named assignment, plus a special
    case for ``frozenset(...)`` — and so scanned 183 of the 270 tags in conftest.py.
    It missed two whole categories:

    * members nested inside tuples/lists (``_UC019_PARAM_XFAIL`` and ten siblings are
      lists of tuples, so the top-level members are tuples, not strings, and the
      ``isinstance(m, str)`` filter dropped all 61 of their tags);
    * bare literals in ``if "T-UC-..." in marker_names`` gates, which belong to no
      collection at all — 30 sites.

    A live orphan (``T-UC-019-partition-principal-invalid``) sat in both categories
    while this guard reported 9/9 green. Its own docstring had named that failure
    ("a regex tuned to one of those shapes silently skips the others") and its
    self-test enumerated four shapes against a file that declares more. Zero
    violations meant the matcher was too narrow, not that nothing was wrong.

    Walking constants removes the shape question entirely: a tag is a tag wherever it
    is written, and a NEW declaration style cannot open a hole. Names are recovered
    only to make the failure message point somewhere useful.
    """
    tree = ast.parse(source)
    collections: dict[str, set[str]] = {}
    claimed: set[int] = set()

    # Statement-level strings are docstrings/prose, not routing. A tag NAMED in a
    # docstring must not be reported as routed.
    documentation = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr) and _is_tag_constant(n.value)}
    # Prefixes consumed by startswith() are not whole tags -- see _is_tag_constant.
    documentation |= _startswith_arguments(tree)

    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign):
            target = node.targets[0] if len(node.targets) == 1 else None
            value = node.value
        else:
            continue
        name = getattr(target, "id", None)
        if not name or value is None:
            continue
        for child in ast.walk(value):
            if _is_tag_constant(child) and id(child) not in documentation:
                collections.setdefault(name, set()).add(child.value)
                claimed.add(id(child))

    # Anything not inside a named assignment: inline `in marker_names` gates, call
    # arguments, comprehensions. Unlabelled but still routed, so still checked.
    for node in ast.walk(tree):
        if _is_tag_constant(node) and id(node) not in claimed and id(node) not in documentation:
            collections.setdefault(INLINE_LABEL, set()).add(node.value)

    return collections


class TestBddTagSetsReferenceRealScenarios:
    """Every tag routed in conftest.py names a scenario that exists."""

    def test_the_scanner_finds_the_known_collections(self):
        """A scan that silently matched nothing would pass the orphan test trivially."""
        collections = _tag_collections(CONFTEST.read_text())
        assert "_XFAIL_TAGS" in collections, (
            f"scanner did not find _XFAIL_TAGS — the AST walk is stale. Found: {sorted(collections)}"
        )
        assert len(collections) >= 4, f"expected several tag collections, found {sorted(collections)}"

    def test_the_scanner_reads_feature_files(self):
        texts = _feature_texts()
        assert texts, f"no feature files under {FEATURES_DIR} — the orphan test would flag everything"

    @pytest.mark.parametrize(
        "declaration,label",
        [
            pytest.param('_P: set[str] = {"T-UC-999-p"}', "_P", id="set-literal"),
            pytest.param('_P: frozenset[str] = frozenset({"T-UC-999-p"})', "_P", id="frozenset-call"),
            pytest.param('_P = ("T-UC-999-p",)', "_P", id="tuple"),
            pytest.param('_P = {"T-UC-999-p": "reason"}', "_P", id="dict-keys"),
            pytest.param('_P = [("T-UC-999-p", "why", True)]', "_P", id="list-of-tuples"),
            pytest.param('_P = {"T-UC-999-p"} | OTHER', "_P", id="set-union"),
            pytest.param('_P = {"a": {"T-UC-999-p"}}', "_P", id="nested-dict-value"),
            pytest.param('if "T-UC-999-p" in marker_names:\n    pass', INLINE_LABEL, id="inline-gate"),
            pytest.param('item.add_marker(x) if "T-UC-999-p" in m else None', INLINE_LABEL, id="inline-expr"),
        ],
    )
    def test_the_scanner_sees_every_way_a_tag_can_be_written(self, declaration, label):
        """Nine forms, all live in conftest.py, all reachable.

        The previous self-test enumerated four and the scanner modelled four, so the
        test agreed with the bug: list-of-tuples rows, inline gates and set-unions all
        smuggled tags past it, and 87 of 270 tags went unchecked. Enumerating forms is
        why this now walks CONSTANTS instead — a tenth form cannot open a hole.
        """
        found = _tag_collections(declaration)
        assert found.get(label) == {"T-UC-999-p"}, (
            f"scanner missed this form, so a tag written this way is never checked: {declaration!r} -> {found}"
        )

    def test_a_tag_named_only_in_a_docstring_is_not_routing(self):
        """Prose about a tag is not a route, and must not be reported as one."""
        assert not _tag_collections('"""See T-UC-999-p for context."""')

    # Every T-UC- string in conftest.py that is deliberately NOT a routed tag.
    # Enumerated, not derived: the coverage test below compares against a MINIMAL
    # predicate (startswith only) rather than _is_tag_constant, so narrowing that
    # predicate cannot keep the coverage claim satisfied by shrinking both sides at
    # once. A clause dropping constants containing "partition", for instance, would
    # silently remove 32 tags from the scan and still pass a self-referential check.
    #
    # 12 UC prefixes consumed by str.startswith, and 2 xfail reasons that happen to
    # begin with the tag they describe.
    BY_DESIGN_UNSCANNED: frozenset[str] = frozenset(
        {
            "T-UC-002",
            "T-UC-002-ext-",
            "T-UC-003",
            "T-UC-003-ext-",
            "T-UC-004",
            "T-UC-004-filter",
            "T-UC-005",
            "T-UC-006",
            "T-UC-011",
            "T-UC-018",
            "T-UC-019",
            "T-UC-026",
            "T-UC-002-inv-015-6 create_media_buy harness wiring is tracked in #1652",
            "T-UC-018-ext-c list_creatives validation harness wiring is tracked in #1652",
        }
    )

    def test_the_scanner_reaches_every_tag_in_conftest(self):
        """Coverage of the SCAN itself, measured against a source-independent baseline.

        The previous version built both sides from ``_is_tag_constant``, so narrowing
        that predicate satisfied the claim by shrinking the numerator and denominator
        together. Here the baseline is every string constant starting with the tag
        prefix — no shared filter — and the gap must equal BY_DESIGN_UNSCANNED exactly.

        Exactly, not ``<=``: a new unscanned tag is a hole, and an entry that stops
        being unscanned means the list is stale and hides the next one.
        """
        source = CONFTEST.read_text()
        every = {
            n.value
            for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith(TAG_PREFIX)
        }
        scanned = set().union(*_tag_collections(source).values())
        assert every - scanned == self.BY_DESIGN_UNSCANNED, (
            "the set of T-UC- constants this guard does not scan changed.\n"
            f"newly invisible (nothing checks these): {sorted((every - scanned) - self.BY_DESIGN_UNSCANNED)}\n"
            f"stale entries (now scanned, remove them): {sorted(self.BY_DESIGN_UNSCANNED - (every - scanned))}"
        )

    def test_no_routed_tag_is_an_orphan(self):
        texts = _feature_texts()
        orphans: dict[str, list[str]] = {}
        for name, tags in sorted(_tag_collections(CONFTEST.read_text()).items()):
            if name in PREFIX_COLLECTIONS:
                continue
            missing = sorted(t for t in tags if not _tag_is_defined(t, texts))
            if missing:
                orphans[name] = missing
        assert not orphans, (
            "these tags are routed in tests/bdd/conftest.py but name no scenario in any "
            f"feature file, so they route nothing: {orphans}. Remove the entry (the "
            "scenario is gone), or restore the tag on the scenario it was meant to cover."
        )

    @pytest.mark.parametrize("name,reason", sorted(PREFIX_COLLECTIONS.items()))
    def test_prefix_collections_still_exist(self, name, reason):
        """A skip-list entry for a collection that no longer exists hides the next one."""
        assert name in CONFTEST.read_text(), (
            f"{name} is skipped as a prefix collection ({reason}) but is no longer in "
            "conftest.py — drop it from PREFIX_COLLECTIONS"
        )
