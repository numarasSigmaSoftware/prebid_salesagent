"""Authenticity guard for TransportResult.wire_response.

The UC-005 format_id federation-contract scenario asserts the ``{agent_url, id}``
object shape on ``wire_response`` for REST/A2A/MCP. That is only meaningful if
``wire_response`` carries the *real* serialized bytes rather than a re-serialization
of the already-validated typed payload — otherwise the wire assertions would be
tautological again (the typed payload can never be a bare string by construction).

These tests pin that contract against ``list_creative_formats`` so a future refactor
cannot quietly substitute a reconstruction. IMPL has no wire by definition.

MCP authenticity note: this test previously used field-presence markers
(``task_id``, ``adcp_version``) to distinguish real MCP wire from a
reconstruction. Those markers were only ever present because MCP's
``ToolResult`` used to serialize the raw response model through
``pydantic_core.to_jsonable_python()``, which leaks every unset optional
field (including the two markers) as an explicit wire ``null`` — the wire
this test was pinning as "real" was itself the bug that fix landed for (see
``src/core/transport_helpers.py::build_mcp_tool_result``). Once fixed,
unset optional fields on MCP are correctly OMITTED, so the markers vanish
and the premise inverts. MCP authenticity is now checked two ways instead:
(1) the wire is JSON-primitive-only (a reconstruction from the typed payload
via `mode="python"` or no mode would retain non-JSON-primitive Python
values -- datetimes, enums, AnyUrl); (2) MCP wire == REST wire, chaining
provenance to the REST test above (both transports serialize the same
response via the same `.model_dump(mode="json")` call).
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.harness import CreativeFormatsEnv
from tests.harness.transport import Transport


def _is_json_primitive_only(value: Any) -> bool:
    """True if *value* contains only JSON-native types (recursively).

    A real JSON wire round-trip can only ever produce str/int/float/bool/None
    plus list/dict of the same. A Python object graph that skipped real
    serialization (e.g. a typed payload reconstruction) would retain
    non-JSON-primitive values -- datetime, Enum, AnyUrl, Decimal, etc.
    ``bool`` is checked before ``int`` since ``bool`` is an ``int`` subclass.
    """
    if value is None or isinstance(value, str | int | float | bool):
        return True
    if isinstance(value, list):
        return all(_is_json_primitive_only(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_json_primitive_only(v) for k, v in value.items())
    return False


@pytest.mark.requires_db
class TestWireResponseIsRealWire:
    """wire_response surfaces the real serialized success-path wire, per transport."""

    # Envelope-only keys present only because A2A wraps the payload — absent
    # from a bare payload reconstruction and from the REST HTTP body. MCP has
    # no such marker post-fix (see module docstring); it's authenticated by
    # _is_json_primitive_only + parity with REST instead.
    ENVELOPE_MARKERS = {
        Transport.A2A: ("success", "message"),
    }

    def test_rest_wire_response_is_the_http_body(self, integration_db):
        """REST wire_response is the actual HTTP JSON body (provenance check).

        REST serializes the payload directly, so wire_response == payload.model_dump();
        asserting == raw_response.json() therefore pins *provenance* (the field is the
        real HTTP response body), not a reconstruction-difference. Symmetrically, the
        bare HTTP body must NOT carry the A2A transport-envelope markers.
        """
        with CreativeFormatsEnv() as env:
            result = env.call_via(Transport.REST)
            assert result.wire_response == result.raw_response.json()
            assert "formats" in result.wire_response
            for marker in (m for markers in self.ENVELOPE_MARKERS.values() for m in markers):
                assert marker not in result.wire_response, (
                    f"REST wire (bare HTTP body) unexpectedly carries envelope field {marker!r}"
                )

    def test_a2a_wire_carries_envelope_fields(self, integration_db):
        """A2A wire carries transport-envelope fields a payload reconstruction would lack.

        A payload model_dump() exposes only the response model's fields (formats,
        creative_agents, pagination, ...). The A2A envelope adds success/message.
        Asserting these makes the oracle distinguish real serialized wire from a
        reconstruction.
        """
        with CreativeFormatsEnv() as env:
            for transport, markers in self.ENVELOPE_MARKERS.items():
                result = env.call_via(transport)
                assert isinstance(result.wire_response, dict), f"{transport}: wire_response not a dict"
                assert "formats" in result.wire_response, f"{transport}: wire_response missing formats"
                for key in markers:
                    assert key in result.wire_response, (
                        f"{transport}: wire_response missing envelope field {key!r} — "
                        "looks like a payload reconstruction, not real wire"
                    )

    def test_mcp_wire_is_json_primitive_only(self, integration_db):
        """MCP wire contains only JSON-native types -- a reconstruction would not.

        Calling response.model_dump() (no mode, or mode="python") instead of
        response.model_dump(mode="json") -- the exact regression this guards
        against -- would leave datetime/AnyUrl objects in the dict; a real
        wire round-trip cannot produce those.
        """
        with CreativeFormatsEnv() as env:
            result = env.call_via(Transport.MCP)
            assert isinstance(result.wire_response, dict), "MCP: wire_response not a dict"
            assert "formats" in result.wire_response, "MCP: wire_response missing formats"
            assert _is_json_primitive_only(result.wire_response), (
                f"MCP wire_response contains non-JSON-primitive values: {result.wire_response!r}"
            )

    def test_mcp_wire_matches_rest_wire(self, integration_db):
        """MCP wire is byte-identical to REST wire -- the parity this fix establishes.

        Both transports serialize the same response via the same
        response.model_dump(mode="json") call (build_mcp_tool_result() for
        MCP; the REST dispatcher for REST), so they must produce the same
        dict. Chains provenance to test_rest_wire_response_is_the_http_body
        above: if REST is proven real wire and MCP matches it, MCP is real
        wire too.
        """
        with CreativeFormatsEnv() as env:
            mcp_result = env.call_via(Transport.MCP)
            rest_result = env.call_via(Transport.REST)
            assert mcp_result.wire_response == rest_result.wire_response

    def test_impl_has_no_wire(self, integration_db):
        """IMPL is an in-process call — no wire by definition."""
        with CreativeFormatsEnv() as env:
            result = env.call_via(Transport.IMPL)
            assert result.wire_response is None
