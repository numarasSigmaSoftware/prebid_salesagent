"""Capability + storyboard applicability guards for UC-002 idempotency.

The AdCP 3.1.1 capability is agent-wide, so ``supported=true`` is truthful
only when every implemented mutation is replay-safe. The four implemented
mutations now meet that bar: create has its media-buy uniqueness backstop,
while update_media_buy, sync_accounts, and sync_creatives use durable
first-insert-wins reservations with payload conflict checks and fenced
completion.
"""

import re
from pathlib import Path

from src.core.config_loader import current_tenant
from src.core.tools.capabilities import _get_adcp_capabilities_impl

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GENERATED_UC002 = PROJECT_ROOT / "tests" / "bdd" / "features" / "BR-UC-002-create-media-buy.feature"
LOCAL_OVERLAYS = PROJECT_ROOT / "tests" / "bdd" / "overlays" / "BR-UC-002-create-media-buy.feature"

LIVE_REPLAY_SCENARIO = "T-UC-002-v31-idempotency-replay"
BOUNDARY_SCENARIO = "T-UC-002-v31-idempotency-pattern-invalid"
# Upstream supported=true phases visible in the generated feature but NOT wired
# to the BDD harness. The name is about scenario wiring, not production: -expired,
# -canonical-comparison, and -in-flight ALL ship (AdCPIdempotencyExpiredError,
# the RFC 8785 canonicalizer, and AdCPIdempotencyInFlightError with retry_after,
# respectively), and all three are graded at the real wire in
# tests/integration/test_idempotency_wire_matrix.py — so those are wiring gaps
# (unwired Given step / tag), not a coverage floor. -error-conflict-details is
# the one genuinely unimplemented pair member at the code level (production
# emits a detail-free conflict).
REMAINING_UNWIRED_SCENARIOS = frozenset(
    {
        "T-UC-002-v31-idempotency-in-flight",
        "T-UC-002-v31-idempotency-expired",
        "T-UC-002-v31-idempotency-canonical-comparison",
        "T-UC-002-v31-error-conflict-details",
    }
)


def test_advertised_idempotency_matches_mutation_wide_replay_support():
    current_tenant.set(None)
    capability = _get_adcp_capabilities_impl(None, None).adcp.idempotency

    assert capability.supported is True
    assert capability.replay_ttl_seconds == 86_400
    assert capability.in_flight_max_seconds == 300


def test_generated_replay_scenario_is_live_and_boundary_fixture_durable():
    """Pin the live upstream replay scenario and the exact boundary fixture through regeneration."""
    generated_text = GENERATED_UC002.read_text()

    # The upstream replay scenario grades production replay directly — no
    # local overlay may reconcile it away again while replay is implemented.
    assert f"@{LIVE_REPLAY_SCENARIO}" in generated_text
    assert "v3.1 idempotency_key replay returns existing media buy without re-execution" in generated_text
    assert 'the response should include the previously created "media_buy_id"' in generated_text
    assert "no new ad platform order should have been created" in generated_text

    assert all(f"@{scenario_id}" in generated_text for scenario_id in REMAINING_UNWIRED_SCENARIOS), (
        "Keep the unimplemented upstream supported=true phases visible; they are not passing claims."
    )

    assert f"@{BOUNDARY_SCENARIO}" in generated_text
    assert "| <256 chars>" in generated_text
    assert "Local scenario overlays applied" in generated_text

    local_text = LOCAL_OVERLAYS.read_text()
    assert f"@{BOUNDARY_SCENARIO}" in local_text
    assert "| <256 chars>" in local_text
    # This scenario grades create_media_buy's real replay behavior directly
    # (via MediaBuyCreateEnv), independent of what the capabilities block
    # advertises — a local overlay removing it here because the agent-wide
    # capability now declares `false` would silently un-grade a real,
    # verified behavior for the wrong reason.
    assert f"@{LIVE_REPLAY_SCENARIO}" not in local_text


def test_no_source_docstring_claims_the_opposite_of_the_shipped_discriminant():
    """Prose that contradicts the wire is a defect, not a typo.

    Historical: at authoring time, seven places described this seller as
    advertising idempotency SUPPORT while the capability wire shipped
    `supported=False`. Six were parameter docstrings a buyer-facing schema
    renders; the seventh was the Spec-Grounding-Gate note, which described
    this very guard as asserting `supported: true`. The wire was always
    right — but the artifact a reviewer reads to check the claim against the
    spec said the opposite, which is exactly the kind of drift the gate
    exists to prevent.

    Text-matching is the right shape here precisely because the defect IS the
    text: there is no behavior to exercise, and the wire side is already pinned
    by `test_advertised_idempotency_matches_mutation_wide_replay_support`
    above. Keyed on the shipped discriminant, so if this seller ever advertises
    support for real, the guard inverts with it instead of going stale.

    Current state: production now ships `supported=True` (the intended
    end-state — see the module docstring), so the early return below fires
    unconditionally and this guard is dormant. Retained rather than deleted:
    if idempotency support is ever withdrawn again, the guard reawakens
    exactly where it left off, instead of needing to be re-authored.
    """
    from pathlib import Path

    from src.core.tools.capabilities import _get_adcp_capabilities_impl

    if _get_adcp_capabilities_impl(None, None).adcp.idempotency.supported:
        return  # Claiming support IS accurate then; nothing to guard.

    repo_root = Path(__file__).resolve().parents[2]
    # Patterns, not substrings. The first pass here matched only the phrasings
    # used in the source docstrings that had just been corrected, so it stayed
    # green against the very defect it was written for: the grounding note
    # spells the same inversion as "the live `supported: true` discriminant".
    # Fitting the matcher to the fixed sites is the failure this guard exists
    # to stop, and it happened twice — scan root first, then patterns.
    #
    # Each pattern asserts something about THIS seller's live posture.
    # Descriptions of the schema union ("`supported: true` requires
    # replay_ttl_seconds") and of the rejected alternative ("`supported: true`
    # claims every mutating call is safe to retry blind") are legitimate and
    # must not match — hence "live/current/asserts", not a bare
    # "supported: true".
    claim_patterns = (
        r"advertises?\s+idempotency\s+support",
        r"reports?\s+idempotency\s+support",
        r"[Ii]dempotency\s+advertised",
        r"(?:live|current)\s+`?supported:\s*true`?",
        r"asserts?\s+(?:the\s+)?(?:live\s+)?`?supported:\s*true`?",
    )
    # Scanning src/ only was the original mistake: the site that mattered most
    # was .claude/notes/pr1546-adcp-3.1.1-grounding.md, the Spec-Grounding-Gate
    # record a reviewer reads to check the claim against the spec — and it sat
    # outside the guard fitted to the sites that had just been corrected.
    scan_roots = (repo_root / "src", repo_root / "docs", repo_root / ".claude" / "notes")
    # Release notes are a HISTORICAL record, not a description of current
    # behavior. docs/releases/2.0.0.md says 2.0.0 "advertises idempotency
    # support ... with a 24-hour replay window", and that is TRUE of 2.0.0:
    # it shipped 2026-07-01, and the flip to supported=false is 2026-07-24, in
    # this PR. Forcing an edit there would falsify the changelog, so the
    # exclusion is deliberate — not an allowlist of inconvenient hits.
    excluded_roots = (repo_root / "docs" / "releases",)

    def _in_scope(path: Path) -> bool:
        return not any(path.is_relative_to(excluded) for excluded in excluded_roots)

    offenders = []
    for root in scan_roots:
        for suffix in ("*.py", "*.md"):
            for path in sorted(root.rglob(suffix)):
                if not _in_scope(path):
                    continue
                text = path.read_text()
                for pattern in claim_patterns:
                    for match in re.finditer(pattern, text):
                        # "advertises idempotency as unsupported" is the CORRECT
                        # wording and contains the offending prefix.
                        window = text[match.start() : match.end() + 40]
                        if "unsupported" in window:
                            continue
                        line = text.count("\n", 0, match.start()) + 1
                        offenders.append(f"{path.relative_to(repo_root)}:{line}: {match.group(0)!r}")

    assert not offenders, (
        "these describe the seller as advertising idempotency support while the wire ships "
        "IdempotencyUnsupported(supported=False):\n  " + "\n  ".join(offenders)
    )
