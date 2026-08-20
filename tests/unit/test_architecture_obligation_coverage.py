"""Guard: Behavioral obligations must have test coverage.

Every obligation tagged with ``**Layer** behavioral`` in docs/test-obligations/
must either:
1. Have a matching ``Covers: <obligation-id>`` in a test (integration or unit), OR
2. Be listed in the KNOWN_UNCOVERED allowlist (JSON file)

The guard scans:
- Integration tests: tests/integration/test_*_v3.py + behavioral files
- Unit entity tests: tests/unit/test_media_buy.py, test_creative.py, test_delivery.py

Allowlist policy (#1228 G2 / #1609; ADR-009 on #1579):
- May **grow** when a new behavioral obligation is documented without a test yet
  (intentional backlog; reviewable in that PR).
- Must **shrink** when a ``Covers:`` test lands — the stale-entry test fails CI
  until the ID is removed from ``obligation_coverage_allowlist.json``.
- Size must equal ``behavioral - covered`` (exact match).
- There is no hard numeric ceiling; do not re-frame this as unratcheted amnesty.
- Reconcile with the general structural-guard rule "allowlists can only shrink":
  that rule applies to *violation* allowlists (new rows = new invariant debt).
  This file is a *coverage backlog* — growth means we documented an untested
  obligation (reviewable); covered IDs must still leave the list.

See: scripts/tag_obligation_ids.py (assigns IDs to obligation docs)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.unit._architecture_helpers import assert_violations_match_allowlist

OBLIGATIONS_DIR = Path(__file__).resolve().parents[2] / "docs" / "test-obligations"
INTEGRATION_DIR = Path(__file__).resolve().parents[2] / "tests" / "integration"
UNIT_DIR = Path(__file__).resolve().parents[2] / "tests" / "unit"
ALLOWLIST_FILE = Path(__file__).resolve().parent / "obligation_coverage_allowlist.json"

# Unit test entity files that carry Covers: tags
_UNIT_ENTITY_FILES = [
    "test_media_buy.py",
    "test_creative.py",
    "test_create_media_buy_behavioral.py",
    "test_update_media_buy_behavioral.py",
    "test_delivery.py",
    "test_delivery_poll_behavioral.py",
    "test_delivery_service_behavioral.py",
    "test_webhook_delivery_service.py",
    "test_product.py",
    "test_product_schema_obligations.py",
    "test_property_list_schema.py",
    "test_quiet_failure_propagation.py",
    "test_get_products_impl_coverage.py",
    "test_creative_formats_behavioral.py",
    "test_get_media_buys.py",
]

# Integration behavioral test files (non-v3) that carry canonical Covers: tags
_INTEGRATION_BEHAVIORAL_FILES = [
    "test_creative_repository.py",
    "test_creative_formats_behavioral.py",
    "test_creative_sync_behavioral.py",
    "test_creative_sync_data_preservation.py",
    "test_creative_sync_transport.py",
]

# Obligation ID pattern: PREFIX-SECTION-SEQ (e.g., UC-002-MAIN-01, BR-RULE-006-01)
_OBLIGATION_ID_RE = re.compile(r"[A-Z][A-Z0-9]+-[\w-]+-\d{2}")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _get_all_obligation_ids() -> set[str]:
    """Extract every ``**Obligation ID** <id>`` from obligation docs."""
    ids: set[str] = set()
    for md in OBLIGATIONS_DIR.glob("*.md"):
        for m in re.finditer(r"\*\*Obligation ID\*\*\s+(\S+)", md.read_text()):
            ids.add(m.group(1))
    return ids


def _obligation_ids_for_layer(layer: str) -> set[str]:
    """Obligation IDs declared ``**Layer** <layer>``.

    One collector for both layers. The schema-layer version was added as a copy of
    the behavioral one with a single literal changed -- parameter substitution,
    which is the duplication the DRY invariant names, and invisible to R0801 at
    this size. Two copies would drift the moment the obligation format changes.
    """
    ids: set[str] = set()
    marker = f"**Layer** {layer}"
    for md in sorted(OBLIGATIONS_DIR.glob("*.md")):
        lines = md.read_text().splitlines()
        for i, line in enumerate(lines):
            m = re.search(r"\*\*Obligation ID\*\*\s+(\S+)", line)
            if m and i + 1 < len(lines) and marker in lines[i + 1]:
                ids.add(m.group(1))
    return ids


def _get_behavioral_obligations() -> set[str]:
    """Obligation IDs where ``**Layer** behavioral``."""
    return _obligation_ids_for_layer("behavioral")


def _get_covered_obligations() -> set[str]:
    """Extract ``Covers: <id>`` tags from test docstrings.

    Scans both integration tests (test_*_v3.py) and unit entity tests.
    Only matches single-line ``Covers: ID`` patterns (not bullet lists).
    """
    covered: set[str] = set()

    def _scan_file(path: Path) -> None:
        for line in path.read_text().splitlines():
            m = re.search(r"Covers:\s+([\w-]+)", line)
            if m and _OBLIGATION_ID_RE.match(m.group(1)):
                covered.add(m.group(1))

    # Integration tests (v3 behavioral files with formal obligation IDs)
    for tf in INTEGRATION_DIR.glob("test_*_v3.py"):
        _scan_file(tf)
    for tf in INTEGRATION_DIR.glob("test_*_behavioral.py"):
        _scan_file(tf)

    # Integration behavioral tests (non-v3 files with canonical Covers: tags)
    for name in _INTEGRATION_BEHAVIORAL_FILES:
        tf = INTEGRATION_DIR / name
        if tf.exists():
            _scan_file(tf)

    # All integration tests (includes former integration_v2 files)
    for tf in INTEGRATION_DIR.glob("test_*.py"):
        _scan_file(tf)

    # Unit entity tests
    for name in _UNIT_ENTITY_FILES:
        tf = UNIT_DIR / name
        if tf.exists():
            _scan_file(tf)

    return covered


def _get_scenario_lines() -> list[tuple[str, int, str]]:
    """Find all ``#### Scenario:`` lines across UC/BR-UC docs.

    Returns list of (filename, line_number, line_text).
    """
    results = []
    for md in sorted(OBLIGATIONS_DIR.glob("*.md")):
        if md.name in ("business-rules.md", "constraints.md"):
            continue
        lines = md.read_text().splitlines()
        for i, line in enumerate(lines):
            if line.startswith("#### Scenario:"):
                results.append((md.name, i + 1, line.strip()))
    return results


def _load_allowlist() -> set[str]:
    """Load the known-uncovered allowlist from JSON."""
    if not ALLOWLIST_FILE.exists():
        return set()
    return set(json.loads(ALLOWLIST_FILE.read_text()))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestObligationCoverage:
    """Structural guard: behavioral obligations must have test coverage."""

    @pytest.mark.arch_guard
    def test_no_new_uncovered_behavioral_obligations(self):
        """Every behavioral obligation has a test or is in the allowlist.

        Adding a new scenario to the obligation docs without a corresponding
        test (or allowlist entry) fails this test.
        """
        behavioral = _get_behavioral_obligations()
        covered = _get_covered_obligations()
        allowlist = _load_allowlist()

        uncovered_and_not_allowed = behavioral - covered - allowlist

        assert not uncovered_and_not_allowed, (
            f"Found {len(uncovered_and_not_allowed)} behavioral obligation(s) with no "
            f"test and not in the allowlist.\n"
            f"Either write a test with 'Covers: <id>' or add to the "
            f"allowlist (obligation_coverage_allowlist.json):\n"
            + "\n".join(f"  {oid}" for oid in sorted(uncovered_and_not_allowed))
        )

    @pytest.mark.arch_guard
    def test_known_uncovered_are_still_obligations(self):
        """Every allowlist entry must reference a real obligation ID.

        Prevents the allowlist from containing phantom entries.
        """
        all_ids = _get_all_obligation_ids()
        allowlist = _load_allowlist()

        phantom = allowlist - all_ids
        assert not phantom, (
            f"Found {len(phantom)} allowlist entries that don't match any obligation ID.\n"
            f"These IDs may have been renamed or removed from the obligation docs:\n"
            + "\n".join(f"  {oid}" for oid in sorted(phantom))
        )

    @pytest.mark.arch_guard
    def test_known_uncovered_not_already_covered(self):
        """If an obligation is covered by a test, remove it from the allowlist.

        Prevents the allowlist from becoming stale when tests are written.
        """
        covered = _get_covered_obligations()
        allowlist = _load_allowlist()

        stale = allowlist & covered
        assert not stale, (
            f"Found {len(stale)} obligation(s) in the allowlist that already have "
            f"tests.\n"
            f"Remove these from obligation_coverage_allowlist.json:\n" + "\n".join(f"  {oid}" for oid in sorted(stale))
        )

    @pytest.mark.arch_guard
    def test_all_scenarios_have_obligation_ids(self):
        """Every ``#### Scenario:`` in UC/BR-UC docs must have an ``**Obligation ID**`` tag.

        Run ``python scripts/tag_obligation_ids.py`` to fix untagged scenarios.
        """
        scenarios = _get_scenario_lines()
        untagged = []

        for md in sorted(OBLIGATIONS_DIR.glob("*.md")):
            if md.name in ("business-rules.md", "constraints.md"):
                continue
            lines = md.read_text().splitlines()
            for i, line in enumerate(lines):
                if line.startswith("#### Scenario:"):
                    if i + 1 >= len(lines) or "**Obligation ID**" not in lines[i + 1]:
                        untagged.append(f"  {md.name}:{i + 1}: {line.strip()}")

        assert not untagged, (
            f"Found {len(untagged)} scenario(s) without **Obligation ID** tags.\n"
            f"Run: python scripts/tag_obligation_ids.py\n" + "\n".join(untagged)
        )

    @pytest.mark.arch_guard
    def test_no_duplicate_obligation_ids(self):
        """Obligation IDs must be unique across all docs."""
        seen: dict[str, list[str]] = {}
        for md in sorted(OBLIGATIONS_DIR.glob("*.md")):
            content = md.read_text()
            for m in re.finditer(r"\*\*Obligation ID\*\*\s+(\S+)", content):
                oid = m.group(1)
                seen.setdefault(oid, []).append(md.name)

        duplicates = {oid: files for oid, files in seen.items() if len(files) > 1}
        assert not duplicates, f"Found {len(duplicates)} duplicate obligation ID(s):\n" + "\n".join(
            f"  {oid}: {', '.join(files)}" for oid, files in sorted(duplicates.items())
        )

    @pytest.mark.arch_guard
    def test_tests_reference_valid_obligations(self):
        """``Covers:`` tags in tests must reference real obligation IDs."""
        all_ids = _get_all_obligation_ids()
        covered = _get_covered_obligations()

        invalid = covered - all_ids
        assert not invalid, (
            f"Found {len(invalid)} Covers: tag(s) referencing non-existent obligation IDs:\n"
            + "\n".join(f"  {oid}" for oid in sorted(invalid))
        )

    @pytest.mark.arch_guard
    def test_obligation_count_documented(self):
        """Track the total obligation and coverage counts for monitoring."""
        all_ids = _get_all_obligation_ids()
        behavioral = _get_behavioral_obligations()
        covered = _get_covered_obligations()
        allowlist = _load_allowlist()

        # Informational — prints counts in verbose mode
        print(f"\n  Obligation IDs:   {len(all_ids)} total")
        print(f"  Behavioral:       {len(behavioral)}")
        print(f"  Covered:          {len(covered)} ({len(covered)}/{len(behavioral)} behavioral)")
        print(f"  Allowlisted:      {len(allowlist)}")
        print(f"  Gap:              {len(behavioral - covered - allowlist)}")

        # Allowlist must exactly match the uncovered behavioral set
        expected_allowlist_size = len(behavioral - covered)
        assert len(allowlist) == expected_allowlist_size, (
            f"Allowlist size ({len(allowlist)}) doesn't match uncovered behavioral count "
            f"({expected_allowlist_size}). Update the allowlist with:\n"
            f"  python scripts/tag_obligation_ids.py && "
            f"regenerate obligation_coverage_allowlist.json"
        )


def _schema_layer_obligation_ids() -> set[str]:
    """Obligation IDs declared ``**Layer** schema``.

    The behavioral collector above filtered ``**Layer** behavioral`` only, so every
    schema-layer obligation was ungraded and a ``Covers:`` tag on one was
    decoration -- deleting it reddened nothing.
    """
    return _obligation_ids_for_layer("schema")


# Schema-layer obligations with no ``Covers:`` tag today. SHRINK ONLY.
#
# 131 of the 327 schema obligations ALREADY carried Covers: tags that
# nothing graded; ratcheting the 196 uncovered ones turns those into enforced
# coverage in one step, and makes removing any of their tags fail. Adding an entry
# here is adding an ungraded obligation and is not allowed; it only shrinks.
SCHEMA_LAYER_UNCOVERED_ALLOWLIST: frozenset[str] = frozenset(
    {
        "BR-RULE-005-01",
        "BR-RULE-006-01",
        "BR-RULE-007-01",
        "BR-RULE-011-01",
        "BR-RULE-012-01",
        "BR-RULE-018-01",
        "BR-RULE-039-01",
        "BR-RULE-045-01",
        "BR-RULE-051-01",
        "BR-RULE-062-01",
        "BR-RULE-064-01",
        "BR-RULE-069-01",
        "CONSTR-ADCP-DOMAIN-01",
        "CONSTR-ADVERTISING-POLICIES-01",
        "CONSTR-APPROVAL-MODE-01",
        "CONSTR-ASSET-TYPES-FILTER-01",
        "CONSTR-ASYNC-RESPONSE-GET-PRODUCTS-01",
        "CONSTR-AVAILABLE-METRIC-01",
        "CONSTR-BATCH-FREQUENCY-01",
        "CONSTR-BILLING-01",
        "CONSTR-BRAND-MANIFEST-POLICY-01",
        "CONSTR-BUDGET-AMOUNT-01",
        "CONSTR-CAPABILITIES-FEATURES-01",
        "CONSTR-CHANNELS-01",
        "CONSTR-CONTENT-STANDARDS-CALIBRATION-EXEMPLARS-01",
        "CONSTR-CONTENT-STANDARDS-SCOPE-01",
        "CONSTR-CONTEXT-ECHO-01",
        "CONSTR-CREATIVE-AGENT-ASSET-TYPE-01",
        "CONSTR-CREATIVE-AGENT-FORMAT-TYPE-01",
        "CONSTR-CREATIVE-SORT-FIELD-01",
        "CONSTR-CREATIVE-STATUS-01",
        "CONSTR-DAILY-SPEND-CAP-01",
        "CONSTR-DELIVERY-DATE-RANGE-01",
        "CONSTR-DELIVERY-MODE-01",
        "CONSTR-DELIVERY-TYPE-01",
        "CONSTR-DRY-RUN-PREVIEW-01",
        "CONSTR-END-TIME-01",
        "CONSTR-EVENT-TYPE-01",
        "CONSTR-FEEDBACK-SOURCE-01",
        "CONSTR-FORMAT-ID-STRUCTURE-01",
        "CONSTR-FORMAT-TYPE-FILTER-01",
        "CONSTR-GET-MEDIA-BUY-DELIVERY-REQUEST-01",
        "CONSTR-GET-PRODUCTS-RESPONSE-01",
        "CONSTR-IS-RESPONSIVE-FILTER-01",
        "CONSTR-LIST-CREATIVES-FIELDS-01",
        "CONSTR-MEASUREMENT-PERIOD-01",
        "CONSTR-MEDIA-BUY-01",
        "CONSTR-MEDIA-BUY-IDENTIFICATION-01",
        "CONSTR-METRIC-TYPE-01",
        "CONSTR-MINIMUM-SPEND-01",
        "CONSTR-PACING-01",
        "CONSTR-PACKAGE-01",
        "CONSTR-PERF-FEEDBACK-CREATIVE-ID-01",
        "CONSTR-PERF-FEEDBACK-PACKAGE-ID-01",
        "CONSTR-PERFORMANCE-INDEX-01",
        "CONSTR-PREVIEW-OUTPUT-FORMAT-01",
        "CONSTR-PRICING-OPTION-01",
        "CONSTR-PRICING-OPTION-XOR-01",
        "CONSTR-PRODUCT-01",
        "CONSTR-PRODUCT-MIN-CARDINALITY-01",
        "CONSTR-PROPERTY-LIST-BASE-PROPERTIES-01",
        "CONSTR-PROPERTY-LIST-FILTERS-01",
        "CONSTR-PROPERTY-LIST-PAGINATION-01",
        "CONSTR-PROPERTY-TYPE-01",
        "CONSTR-PROTOCOLS-01",
        "CONSTR-PUBLISHER-DOMAINS-FILTER-01",
        "CONSTR-SAMPLING-METHOD-01",
        "CONSTR-SI-TERMINATION-REASON-01",
        "CONSTR-SI-TRANSACTION-ACTION-01",
        "CONSTR-SIGNAL-CATALOG-TYPES-FILTER-01",
        "CONSTR-SIGNAL-DELIVER-TO-01",
        "CONSTR-SIGNAL-MAX-CPM-FILTER-01",
        "CONSTR-SIGNAL-MAX-RESULTS-01",
        "CONSTR-SIGNAL-MIN-COVERAGE-FILTER-01",
        "CONSTR-SIGNAL-SPEC-01",
        "CONSTR-SORT-DIRECTION-01",
        "CONSTR-STATUS-FILTER-01",
        "CONSTR-TARGETING-01",
        "CONSTR-TASK-STATUS-01",
        "CONSTR-TASK-TYPE-01",
        "CONSTR-TASKS-SORT-FIELD-01",
        "CONSTR-VALIDATION-MODE-01",
        "CONSTR-WCAG-LEVEL-01",
        "CONSTR-WEBHOOK-CREDENTIALS-01",
        "UC-002-ALT-ASAP-START-TIMING-04",
        "UC-002-ALT-ASAP-START-TIMING-05",
        "UC-002-ALT-PROPOSAL-BASED-MEDIA-05",
        "UC-002-ALT-PROPOSAL-BASED-MEDIA-07",
        "UC-002-ALT-PROPOSAL-BASED-MEDIA-08",
        "UC-002-ALT-PROPOSAL-BASED-MEDIA-09",
        "UC-002-ALT-PROPOSAL-BASED-MEDIA-10",
        "UC-002-ALT-PROPOSAL-BASED-MEDIA-11",
        "UC-002-ALT-WITH-INLINE-CREATIVES-04",
        "UC-002-CC-MINIMUM-SPEND-PER-01",
        "UC-002-CC-MINIMUM-SPEND-PER-03",
        "UC-002-CC-MINIMUM-SPEND-PER-04",
        "UC-002-CC-SCHEMA-COMPLIANCE-01",
        "UC-002-CC-SCHEMA-COMPLIANCE-02",
        "UC-002-CC-SCHEMA-COMPLIANCE-03",
        "UC-002-EXT-A-02",
        "UC-002-EXT-A-03",
        "UC-002-EXT-B-02",
        "UC-002-EXT-B-03",
        "UC-002-EXT-C-01",
        "UC-002-EXT-C-03",
        "UC-002-EXT-C-05",
        "UC-002-EXT-D-03",
        "UC-002-EXT-D-04",
        "UC-002-EXT-E-02",
        "UC-002-EXT-F-03",
        "UC-002-EXT-F-04",
        "UC-002-EXT-F-05",
        "UC-002-EXT-G-02",
        "UC-002-EXT-G-03",
        "UC-002-EXT-H-01",
        "UC-002-EXT-H-04",
        "UC-002-EXT-K-02",
        "UC-002-EXT-M-02",
        "UC-002-EXT-M-04",
        "UC-002-EXT-N-03",
        "UC-002-EXT-N-05",
        "UC-002-MAIN-06",
        "UC-002-MAIN-08",
        "UC-002-MAIN-11",
        "UC-002-MAIN-12",
        "UC-002-MAIN-13",
        "UC-002-MAIN-16",
        "UC-002-MAIN-18",
        "UC-002-SHARED-IMPLEMENTATION-PATTERN-01",
        "UC-002-UPG-08",
        "UC-003-ALT-PAUSE-RESUME-CAMPAIGN-04",
        "UC-003-ALT-UPLOAD-INLINE-CREATIVES-03",
        "UC-003-EXT-G-02",
        "UC-004-MAIN-07",
        "UC-004-MAIN-08",
        "UC-005-EXT-A-02",
        "UC-005-EXT-B-06",
        "UC-005-EXT-B-07",
        "UC-005-EXT-B-08",
        "UC-005-EXT-B-09",
        "UC-005-EXT-B-10",
        "UC-005-EXT-B-11",
        "UC-005-EXT-B-12",
        "UC-005-EXT-B-13",
        "UC-005-EXT-B-14",
        "UC-005-EXT-B-15",
        "UC-005-EXT-B-16",
        "UC-005-MAIN-MCP-08",
        "UC-005-MAIN-MCP-09",
        "UC-007-EXT-C-01",
        "UC-007-EXT-C-03",
        "UC-007-EXT-C-04",
        "UC-007-MAIN-MCP-01",
        "UC-007-MAIN-MCP-04",
        "UC-007-MAIN-MCP-08",
        "UC-007-MAIN-MCP-09",
        "UC-007-SCHEMA-01",
        "UC-007-SCHEMA-02",
        "UC-008-EXT-B-03",
        "UC-008-MAIN-MCP-03",
        "UC-008-MAIN-MCP-04",
        "UC-008-MAIN-MCP-05",
        "UC-008-MAIN-MCP-06",
        "UC-009-MAIN-MCP-05",
        "UC-009-MAIN-MCP-06",
        "UC-009-SCHEMA-02",
        "UC-009-SCHEMA-03",
        "UC-010-EXT-E-01",
        "UC-010-MAIN-MCP-03",
        "UC-010-MAIN-MCP-04",
        "UC-010-SCHEMA-01",
        "UC-010-SCHEMA-02",
        "UC-011-EXT-D-02",
        "UC-011-EXT-E-01",
        "UC-011-EXT-G-01",
        "UC-011-EXT-G-02",
        "UC-011-EXT-G-03",
        "UC-011-EXT-G-04",
        "UC-011-MAIN-03",
        "UC-011-MAIN-05",
        "UC-011-MAIN-08",
        "UC-011-MAIN-12",
        "UC-011-MAIN-16",
        "UC-011-SCHEMA-02",
        "UC-011-SCHEMA-03",
        "UC-011-SCHEMA-04",
        "UC-011-SCHEMA-05",
        "UC-012-EXT-A-01",
        "UC-012-EXT-A-07",
        "UC-012-EXT-C-05",
        "UC-012-SCHEMA-03",
        "UC-012-SCHEMA-04",
        "UC-013-EXT-A-08",
        "UC-013-EXT-B-01",
        "UC-013-EXT-B-09",
        "UC-013-SCHEMA-06",
    }
)


class TestSchemaLayerObligationsAreRatcheted:
    """Schema-layer obligations are graded too, on a shrink-only ratchet."""

    def test_the_collector_finds_schema_obligations(self):
        """A collector matching nothing would make every assertion below vacuous."""
        ids = _schema_layer_obligation_ids()
        assert len(ids) > 100, f"expected hundreds of schema-layer obligations, found {len(ids)}"

    # NO separate size ceiling, deliberately. A MAX_UNCOVERED literal beside the list
    # it counts is the falsifiable-comment shape this suite removes elsewhere, and
    # DERIVING it from len() makes both a `<=` and an `==` assertion tautological --
    # the list is the state, so nothing in this file can hold the list accountable to
    # itself. (The `<=` form was also unreachable: anything tripping it tripped the
    # `==` first.)
    #
    # This follows the repo's own precedent, stated in
    # test_architecture_e2e_rest_escape_hatches: "There is deliberately no separate
    # count <= len(pin) ratchet: the exact-set comparison already fails in both
    # directions." Here the exact-set check below refuses an uncovered obligation that
    # is NOT allowlisted, and refuses an allowlisted one that IS now covered. What it
    # cannot refuse is someone adding a genuinely-new uncovered obligation to the list
    # instead of covering it -- that is a review judgement, tracked in #1935, and
    # pretending a self-referential assertion catches it is worse than saying so.

    def test_schema_layer_coverage_matches_the_ratchet(self):
        """Both directions in one assertion, via the shared helper.

        The "stale entries" half is what makes a ``Covers:`` tag load-bearing: an
        obligation that gained coverage must leave the allowlist, so deleting its tag
        later cannot silently return it to the allowed set. Without that half the tag
        is decoration again — the exact defect this class closes.

        Routed through ``assert_violations_match_allowlist`` rather than a hand-rolled
        pair of set-diffs; ``test_architecture_no_handrolled_allowlist_diff`` enforces
        that, and caught this guard doing it by hand on the first draft.
        """
        uncovered = {(o,) for o in _schema_layer_obligation_ids() - _get_covered_obligations()}
        assert_violations_match_allowlist(
            uncovered,
            {(o,) for o in SCHEMA_LAYER_UNCOVERED_ALLOWLIST},
            fix_hint=(
                "a NEW entry means a schema-layer obligation has no `Covers:` tag — add one to the "
                "test that grades it rather than extending the allowlist, which only shrinks. "
                "A STALE entry means it is now covered — remove it, so dropping that tag fails here."
            ),
        )
