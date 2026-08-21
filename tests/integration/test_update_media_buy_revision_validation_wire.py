"""Integration wire pins: schema-invalid ``revision`` on update_media_buy.

These tests complement the BDD outlines (T-UC-003-partition-revision /
T-UC-003-boundary-revision) by pinning the real wire envelope per transport via
the harness (``result.wire_error_envelope`` + ``assert_envelope_shape``):

- below-minimum revision (0): an ``int`` on every transport, so it reaches the
  Pydantic ``ge=1`` constraint at the shared ``adcp_validation_boundary``
  everywhere -> the same schema-rejection code on A2A, MCP, and REST alike.
- wrong-type revisions (including numeric string ``"7"``): every transport
  emits the same schema-rejection code at the shared request-schema boundary.
- whole-number float (``1.0``): draft-07 ``type: integer`` matches any number
  with a zero fractional part, so the pinned schema ACCEPTS it — on every
  transport, not just the one whose wire happens to deliver protobuf doubles.
- non-integral float (``7.5``): rejected everywhere, with the same code.
"""

from __future__ import annotations

import pytest

from tests.harness.transport import Transport
from tests.helpers import assert_envelope_shape

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

# All three wire transports surface a real wire envelope for update errors.
_WIRE_TRANSPORTS = [Transport.A2A, Transport.MCP, Transport.REST]

# The wire code every transport emits for a request-schema violation, produced by
# the single shared ``adcp_validation_boundary``. Named once here so the point of
# these tests stays "every transport agrees", not "this literal string".
_SCHEMA_REJECTION_CODE = "VALIDATION_ERROR"


class TestUpdateRevisionValidationWire:
    """Schema-invalid ``revision`` values must emit the documented wire codes.

    Uses the shared ``env_with_media_buy`` fixture (tests/integration/conftest.py)
    — the single home for the dual-env + seeded-buy setup.
    """

    @pytest.mark.parametrize("transport", _WIRE_TRANSPORTS, ids=lambda t: t.value)
    def test_below_min_revision_emits_invalid_request_on_every_transport(self, env_with_media_buy, transport):
        """revision=0 violates the schema minimum (ge=1) at the shared Pydantic
        boundary on every transport -> a correctable schema rejection on the wire."""
        env, media_buy = env_with_media_buy
        result = env.call_via(transport, media_buy_id=media_buy.media_buy_id, paused=True, revision=0)

        assert result.is_error, "expected a validation error for revision=0"
        assert result.wire_error_envelope is not None, "wire envelope not captured"
        assert_envelope_shape(result.wire_error_envelope, _SCHEMA_REJECTION_CODE, recovery="correctable")

    @pytest.mark.parametrize("transport", _WIRE_TRANSPORTS, ids=lambda t: t.value)
    @pytest.mark.parametrize("revision", ["not-an-int", "7"], ids=["non_numeric", "numeric_string"])
    def test_wrong_type_revision_emits_invalid_request_on_every_transport(
        self, env_with_media_buy, transport, revision
    ):
        """A non-integer revision emits the same correctable schema rejection on the
        wire for all transports."""
        env, media_buy = env_with_media_buy
        result = env.call_via(transport, media_buy_id=media_buy.media_buy_id, paused=True, revision=revision)

        assert result.is_error, "expected a validation error for a non-integer revision"
        assert result.wire_error_envelope is not None, "wire envelope not captured"
        assert_envelope_shape(result.wire_error_envelope, _SCHEMA_REJECTION_CODE, recovery="correctable")

    @pytest.mark.parametrize("transport", _WIRE_TRANSPORTS, ids=lambda t: t.value)
    def test_whole_number_float_revision_is_accepted_on_every_transport(self, env_with_media_buy, transport):
        """``1.0`` satisfies draft-07 ``{"type": "integer", "minimum": 1}`` and matches
        the fresh buy's revision, so it must SUCCEED — identically on all transports.

        Before this pin the shared gate rejected any float, and A2A passed only because
        the skill handler hand-coerced whole-number floats to int before the gate saw
        them: A2A was the sole transport meeting the pinned schema.
        """
        env, media_buy = env_with_media_buy
        result = env.call_via(transport, media_buy_id=media_buy.media_buy_id, paused=True, revision=1.0)

        assert result.is_success, f"expected revision=1.0 to be accepted, got error: {result.error!r}"

    @pytest.mark.parametrize("transport", _WIRE_TRANSPORTS, ids=lambda t: t.value)
    def test_stale_revision_on_a_terminal_buy_emits_conflict_on_every_transport(self, env_with_media_buy, transport):
        """A stale token against a since-terminal buy must reach the buyer as CONFLICT.

        The optimistic-concurrency gate runs BEFORE the terminal-state gate, so the
        buyer is told to refetch-and-retry rather than that the buy is gone. Graded on
        the real wire envelope per transport: the precedence is a buyer-visible contract,
        and a grader that asserts on a raised exception from an in-process call proves
        nothing about what any transport actually emitted — swapping the two gates would
        leave such a test green on every wire.
        """
        from src.core.database.repositories import MediaBuyUoW

        env, media_buy = env_with_media_buy

        # Drive the buy terminal out-of-band; this bumps the persisted revision 1 -> 2,
        # so the token below is stale AND the buy is terminal — both gates are eligible.
        with MediaBuyUoW(env.identity.tenant_id) as uow:
            uow.media_buys.update_status(media_buy.media_buy_id, "completed")

        result = env.call_via(transport, media_buy_id=media_buy.media_buy_id, paused=True, revision=1)

        assert result.is_error, "expected a CONFLICT for a stale token on a terminal buy"
        assert result.wire_error_envelope is not None, "wire envelope not captured"
        assert_envelope_shape(result.wire_error_envelope, "CONFLICT", recovery="transient")

    @pytest.mark.parametrize("transport", _WIRE_TRANSPORTS, ids=lambda t: t.value)
    def test_non_integral_float_revision_emits_invalid_request_on_every_transport(self, env_with_media_buy, transport):
        """``7.5`` has a non-zero fractional part, so draft-07 ``integer`` rejects it —
        accepting whole-number floats must not widen the gate to every float."""
        env, media_buy = env_with_media_buy
        result = env.call_via(transport, media_buy_id=media_buy.media_buy_id, paused=True, revision=7.5)

        assert result.is_error, "expected a validation error for revision=7.5"
        assert result.wire_error_envelope is not None, "wire envelope not captured"
        assert_envelope_shape(result.wire_error_envelope, _SCHEMA_REJECTION_CODE, recovery="correctable")

    @pytest.mark.parametrize("transport", _WIRE_TRANSPORTS, ids=lambda t: t.value)
    def test_stale_revision_conflict_recovery_is_transient_on_the_wire(self, env_with_media_buy, transport):
        """A schema-VALID but STALE revision emits CONFLICT/transient on the wire.

        The optimistic-concurrency ``recovery`` classification is buyer-facing —
        it tells the buyer to re-read and retry. It was pinned nowhere on the wire:
        the ``media_buy_revision_conflict`` factory sets ``recovery="transient"``,
        but the only CONFLICT grades were reconstructed-exception ``error_code``
        asserts at the ``_impl`` layer, so flipping the factory to ``correctable``
        stayed green everywhere. This pins the wire ``recovery`` per transport;
        a factory regression now reddens here.
        """
        env, media_buy = env_with_media_buy
        # A fresh buy is at revision 1; 999 is a valid int that mismatches, so it
        # passes schema validation and reaches the revision-conflict gate.
        result = env.call_via(transport, media_buy_id=media_buy.media_buy_id, paused=True, revision=999)

        assert result.is_error, "expected a CONFLICT for a stale revision"
        assert result.wire_error_envelope is not None, "wire envelope not captured"
        assert_envelope_shape(result.wire_error_envelope, "CONFLICT", recovery="transient")
