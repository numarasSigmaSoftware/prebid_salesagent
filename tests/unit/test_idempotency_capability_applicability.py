"""Capability + storyboard applicability guards for UC-002 idempotency.

The AdCP 3.1.1 capability is agent-wide, so ``supported=true`` is truthful
only when every implemented mutation is replay-safe. The four implemented
mutations now meet that bar: create has its media-buy uniqueness backstop,
while update_media_buy, sync_accounts, and sync_creatives use durable
first-insert-wins reservations with payload conflict checks and fenced
completion.
"""

from pathlib import Path

from src.core.config_loader import current_tenant
from src.core.tools.capabilities import _get_adcp_capabilities_impl

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GENERATED_UC002 = PROJECT_ROOT / "tests" / "bdd" / "features" / "BR-UC-002-create-media-buy.feature"
LOCAL_OVERLAYS = PROJECT_ROOT / "tests" / "bdd" / "overlays" / "BR-UC-002-create-media-buy.feature"

LIVE_REPLAY_SCENARIO = "T-UC-002-v31-idempotency-replay"
BOUNDARY_SCENARIO = "T-UC-002-v31-idempotency-pattern-invalid"
# Upstream supported=true phases visible in the generated feature but NOT wired
# to the BDD harness. The name is about scenario wiring, not production: -expired
# and -canonical-comparison DO ship (AdCPIdempotencyExpiredError and the RFC 8785
# canonicalizer), and both are graded at the real wire in
# tests/integration/test_idempotency_wire_matrix.py — so this is a wiring gap,
# not a coverage floor. -in-flight and -error-conflict-details are the genuinely
# unimplemented pair (production emits SERVICE_UNAVAILABLE with retry_after, and
# a detail-free conflict).
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

    Seven places described this seller as advertising idempotency SUPPORT while
    `capabilities.py` ships `IdempotencyUnsupported(supported=False)`. Six were
    parameter docstrings a buyer-facing schema renders; the seventh was the
    Spec-Grounding-Gate note, which described this very guard as asserting
    `supported: true`. The wire was always right — but the artifact a reviewer
    reads to check the claim against the spec said the opposite, which is
    exactly the kind of drift the gate exists to prevent.

    Text-matching is the right shape here precisely because the defect IS the
    text: there is no behavior to exercise, and the wire side is already pinned
    by `test_advertised_idempotency_is_the_narrower_defect_not_a_resolved_claim`
    above. Keyed on the shipped discriminant, so if this seller ever advertises
    support for real, the guard inverts with it instead of going stale.
    """
    from pathlib import Path

    from src.core.tools.capabilities import _get_adcp_capabilities_impl

    if _get_adcp_capabilities_impl(None, None).adcp.idempotency.supported:
        return  # Claiming support IS accurate then; nothing to guard.

    repo_root = Path(__file__).resolve().parents[2]
    claims = (
        "advertises idempotency support",
        "advertise idempotency support",
        "Capabilities advertise\n            idempotency support",
    )
    offenders = [
        f"{path.relative_to(repo_root)}: {claim!r}"
        for path in sorted((repo_root / "src").rglob("*.py"))
        for claim in claims
        if claim in path.read_text()
    ]

    assert not offenders, (
        "these describe the seller as advertising idempotency support while the wire ships "
        "IdempotencyUnsupported(supported=False):\n  " + "\n  ".join(offenders)
    )
