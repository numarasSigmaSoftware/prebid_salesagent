"""Authenticity guard for TransportResult.wire_response.

The UC-005 format_id federation-contract scenario asserts the ``{agent_url, id}``
object shape on ``wire_response`` for REST/A2A/MCP. That is only meaningful if
``wire_response`` carries the *real* serialized bytes rather than a re-serialization
of the already-validated typed payload — otherwise the wire assertions would be
tautological again (the typed payload can never be a bare string by construction).

These tests pin that contract against ``list_creative_formats`` so a future refactor
cannot quietly substitute a reconstruction. A2A provenance uses its transport-only
success/message fields. IMPL has no wire by definition.

MCP has no envelope-only markers (GH #1710): before that fix, the MCP wrapper
handed ``ToolResult`` the raw pydantic response object, which
FastMCP serializes via ``pydantic_core.to_jsonable_python()`` — bypassing
``AdCPBaseModel``'s ``exclude_none=True`` default, so unset optional fields
(``task_id``, ``adcp_version``) leaked onto the wire as ``null``. Those leaked
nulls were incidentally usable as "this must be real wire, not a reconstruction"
markers. The fix routes the MCP wrapper through ``mcp_result()``, whose dump is
the *same* one A2A/REST already use — so MCP's ``structured_content`` is now
BYTE-IDENTICAL to a plain payload dump: there is no separate MCP envelope layer,
by design (FastMCP's ``structured_content`` IS the tool's typed output). So MCP
authenticity is pinned two other ways below: provenance (the field IS the
captured ``CallToolResult.structured_content``) and round-trip fidelity (a
fabricated or partial dict wouldn't parse back into the response type and
re-dump identically).
"""

from __future__ import annotations

import pytest

from src.core.schemas import ListCreativeFormatsResponse
from tests.harness import CreativeFormatsEnv
from tests.harness.transport import Transport


@pytest.mark.requires_db
class TestWireResponseIsRealWire:
    """wire_response surfaces the real serialized success-path wire, per transport."""

    # Envelope-only keys present only because A2A wraps the payload — absent
    # from a bare payload reconstruction and from the REST HTTP body. MCP has no
    # envelope-only keys anymore (see module docstring): its wire is checked
    # separately via provenance and round-trip fidelity.
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
            for marker in self.ENVELOPE_MARKERS[Transport.A2A]:
                assert marker not in result.wire_response, (
                    f"REST wire (bare HTTP body) unexpectedly carries envelope field {marker!r}"
                )

    def test_a2a_wire_carries_envelope_fields(self, integration_db):
        """A2A wire carries transport-envelope fields a payload reconstruction would lack.

        A payload model_dump() exposes only the response model's fields (formats,
        creative_agents, pagination, ...). The A2A envelope adds success/message
        (injected by ``_serialize_for_a2a``, not part of the response model) —
        asserting these makes the oracle distinguish real serialized wire from a
        reconstruction.
        """
        with CreativeFormatsEnv() as env:
            result = env.call_via(Transport.A2A)
            assert isinstance(result.wire_response, dict), "A2A wire_response not a dict"
            assert "formats" in result.wire_response, "A2A wire_response missing formats"
            assert result.wire_response["success"] is True
            assert result.wire_response["message"]

    def test_mcp_wire_response_is_call_tool_structured_content(self, integration_db):
        """MCP wire_response comes from the actual in-memory CallToolResult."""
        with CreativeFormatsEnv() as env:
            result = env.call_via(Transport.MCP)
            assert result.raw_response is not None, "MCP dispatch did not expose its CallToolResult"
            assert result.wire_response == result.raw_response.structured_content
            assert "formats" in result.wire_response

    def test_mcp_wire_round_trips_through_the_response_type(self, integration_db):
        """MCP wire is a byte-faithful serialization of a valid response instance.

        MCP has no envelope-only markers to assert (see module docstring): its
        structured_content is now exactly ``response.model_dump(mode="json")``. A
        fabricated/partial reconstruction would either fail to construct
        ``ListCreativeFormatsResponse`` (missing/wrong-typed required fields) or
        fail to re-dump identically (extra/dropped/differently-shaped fields), so
        round-trip equality is the meaningful authenticity signal left post-fix.
        """
        with CreativeFormatsEnv() as env:
            result = env.call_via(Transport.MCP)
        assert isinstance(result.wire_response, dict), "MCP: wire_response not a dict"
        assert "formats" in result.wire_response, "MCP: wire_response missing formats"
        reparsed = ListCreativeFormatsResponse(**result.wire_response)
        assert reparsed.model_dump(mode="json") == result.wire_response, (
            "MCP wire_response does not round-trip through ListCreativeFormatsResponse — "
            "looks like a fabricated/partial reconstruction, not real wire"
        )

    def test_impl_has_no_wire(self, integration_db):
        """IMPL is an in-process call — no wire by definition."""
        with CreativeFormatsEnv() as env:
            result = env.call_via(Transport.IMPL)
            assert result.wire_response is None
