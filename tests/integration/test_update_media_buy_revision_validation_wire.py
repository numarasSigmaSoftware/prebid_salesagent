"""The `revision` value contract, graded on the real wire of all three transports.

One rule decides what a `revision` value may be, so MCP, REST and A2A must reject
and accept exactly the same inputs. The pinned update-media-buy-request.json types
the field as {"type": "integer", "minimum": 1} -- and under draft-07 `integer`
admits any number with a zero fractional part, so 2.0 is VALID and must be accepted
on every transport (A2A carries JSON numbers as doubles).

The present-but-null case is the one that can only be decided at the boundary: the
schema gives `revision` no null arm, so an explicit null is a violation, but further
in it is indistinguishable from an omitted key. Each boundary therefore runs the
shared gate itself, and each is graded here.

Dispatch goes through MediaBuyDualEnv's RAW flat-kwargs form on purpose. Building an
UpdateMediaBuyRequest in the test process would raise inside the test for exactly the
values under test, so the rejection would never reach a wire to be graded.
"""

import pytest

from tests.factories import MediaBuyFactory
from tests.harness.media_buy_dual import MediaBuyDualEnv
from tests.harness.transport import Transport

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

_MEDIA_BUY_ID = "mb_revision_wire"

#: The three real wire transports. IMPL is excluded deliberately -- it has no wire,
#: and the boundary gate under test is precisely what IMPL does not run.
WIRE_TRANSPORTS = [Transport.MCP, Transport.REST, Transport.A2A]

#: Values the pinned schema does not admit. Each must be refused identically everywhere.
REJECTED_VALUES = [
    pytest.param("7", id="numeric-string"),
    pytest.param(True, id="bool-true"),
    pytest.param(1.5, id="fractional"),
    pytest.param(0, id="below-minimum"),
    pytest.param(-1, id="negative"),
]


def _seed(env: MediaBuyDualEnv) -> None:
    tenant, principal, _product, _pricing = env.setup_media_buy_data()
    MediaBuyFactory(
        tenant=tenant,
        principal=principal,
        media_buy_id=_MEDIA_BUY_ID,
        status="active",
    )
    env._commit_factory_data()
    # The REST leg builds its PUT URL from this attribute (the flat-kwargs form has no
    # request model to read media_buy_id off). Leaving it unset points REST at
    # "NOT_SEEDED", which answers MEDIA_BUY_NOT_FOUND -- an error, so a bare
    # "is_error" assertion would pass without the gate ever running.
    env._seeded_media_buy_id = _MEDIA_BUY_ID


def _update(env: MediaBuyDualEnv, transport: Transport, **kwargs):
    """Send a flat update body over *transport*'s real wire."""
    return env.call_via(transport, media_buy_id=_MEDIA_BUY_ID, paused=True, **kwargs)


@pytest.mark.parametrize("transport", WIRE_TRANSPORTS)
class TestRevisionValueContractOnEveryWire:
    @pytest.mark.parametrize("bad_value", REJECTED_VALUES)
    def test_rejects_a_schema_violation(self, integration_db, transport, bad_value):
        with MediaBuyDualEnv() as env:
            _seed(env)
            result = _update(env, transport, revision=bad_value)
        assert result.is_error, (
            f"{transport} accepted revision={bad_value!r}, which the pinned schema forbids; payload={result.payload!r}"
        )
        # Pin the CODE, not merely "something went wrong": the buy exists and the
        # request is otherwise valid, so VALIDATION_ERROR is the only reason this
        # may fail. Any other code means the gate did not run.
        result.assert_wire_error("VALIDATION_ERROR", recovery="correctable")

    def test_rejects_present_but_null(self, integration_db, transport):
        """An explicit null is a schema violation, not "no token supplied".

        Reading it as absent would hand the buyer a 200 on an update whose
        concurrency check never ran -- the exact silent failure this gate exists
        to prevent. Only the boundary can still tell null from omitted.
        """
        with MediaBuyDualEnv() as env:
            _seed(env)
            result = _update(env, transport, revision=None)
        assert result.is_error, (
            f"{transport} read an explicitly-supplied null revision as 'absent' and "
            f"processed the update anyway; payload={result.payload!r}"
        )
        result.assert_wire_error("VALIDATION_ERROR", recovery="correctable")

    def test_accepts_a_whole_number_float(self, integration_db, transport):
        """2.0 is schema-valid under draft-07 `integer` and must not be rejected.

        A2A delivers integers as doubles, so refusing this form would refuse a
        conformant buyer on one transport and accept it on the others.
        """
        with MediaBuyDualEnv() as env:
            _seed(env)
            result = _update(env, transport, revision=1.0)
        assert not result.is_error, (
            f"{transport} rejected the schema-valid whole-number float 1.0: "
            f"{result.wire_error_envelope or result.error!r}"
        )

    def test_accepts_a_plain_integer(self, integration_db, transport):
        with MediaBuyDualEnv() as env:
            _seed(env)
            result = _update(env, transport, revision=1)
        assert not result.is_error, (
            f"{transport} rejected a matching integer token: {result.wire_error_envelope or result.error!r}"
        )

    def test_omitting_revision_is_still_accepted(self, integration_db, transport):
        """revision is optional; the gate must not turn absence into a rejection."""
        with MediaBuyDualEnv() as env:
            _seed(env)
            result = _update(env, transport)
        assert not result.is_error, (
            f"{transport} rejected an update that supplied no revision at all: "
            f"{result.wire_error_envelope or result.error!r}"
        )
