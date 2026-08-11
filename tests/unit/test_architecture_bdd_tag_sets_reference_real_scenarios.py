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


def _tag_collections(source: str) -> dict[str, set[str]]:
    """Every module- or block-level collection of scenario tags, by variable name.

    AST rather than regex: these are declared as set literals, frozensets, tuples and
    dict keys, at module scope and nested inside ``pytest_collection_modifyitems``,
    and a regex tuned to one of those shapes silently skips the others — the exact
    failure mode #1923 records for the sibling guard's discovery.
    """
    collections: dict[str, set[str]] = {}
    for node in ast.walk(ast.parse(source)):
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
        # frozenset({...}) / set({...}) are CALLS, not literals, so literal_eval
        # alone silently skips them — which is precisely the "models one declaration
        # form" blind spot this guard exists to stop being acceptable. Caught by the
        # planted-orphan self-test below, which passed against the first version of
        # this scanner because the set it planted into was a frozenset() call.
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in {"frozenset", "set"}:
            value = value.args[0] if value.args else ast.Set(elts=[])
        try:
            literal = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError):
            continue  # computed at runtime — not a literal tag list
        if isinstance(literal, dict):
            members = literal.keys()
        elif isinstance(literal, set | frozenset | list | tuple):
            members = literal
        else:
            continue
        tags = {m for m in members if isinstance(m, str) and m.startswith(TAG_PREFIX)}
        if tags:
            collections.setdefault(name, set()).update(tags)
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
        "declaration",
        [
            pytest.param('_PLANTED: set[str] = {"T-UC-999-planted"}', id="set-literal"),
            pytest.param('_PLANTED: frozenset[str] = frozenset({"T-UC-999-planted"})', id="frozenset-call"),
            pytest.param('_PLANTED = ("T-UC-999-planted",)', id="tuple"),
            pytest.param('_PLANTED = {"T-UC-999-planted": "reason"}', id="dict-keys"),
        ],
    )
    def test_the_scanner_sees_every_declaration_form(self, declaration):
        """A scanner that models ONE declaration form is an escape hatch.

        The first version of this guard used ``ast.literal_eval`` alone, so
        ``frozenset({...})`` — a Call, not a literal — was invisible: a planted
        orphan in ``_NO_E2E_REST_TAGS`` passed. Each form below is live in
        conftest.py today, so each is parametrized rather than asserted once.
        """
        found = _tag_collections(declaration)
        assert found.get("_PLANTED") == {"T-UC-999-planted"}, (
            f"scanner missed this declaration form, so a tag set written this way is "
            f"never checked: {declaration!r} -> {found}"
        )

    def test_exact_matching_rejects_a_prefix_collision(self):
        """The property the whole guard rests on: -049-1 must NOT match @-049-10.

        A substring check reports a live tag for one that does not exist, which is
        how two of these orphans survived an earlier manual sweep.
        """
        texts = ["@T-UC-005-inv-049-10\nScenario: x"]
        assert _tag_is_defined("T-UC-005-inv-049-10", texts)
        assert not _tag_is_defined("T-UC-005-inv-049-1", texts), (
            "exact matching is broken — a prefix collision reads as a defined tag"
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
