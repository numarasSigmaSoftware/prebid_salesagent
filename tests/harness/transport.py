"""Transport enum and TransportResult for multi-transport behavioral tests.

Defines the seven dispatch transports (IMPL, A2A, REST, MCP + E2E variants)
and a frozen result container that separates transport-specific envelope from
shared payload.

Usage::

    result = env.call_via(Transport.REST, creatives=[...])
    assert result.is_success
    assert result.payload.creatives[0].action == CreativeAction.created
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from tests.helpers import pinned_schema


@functools.lru_cache(maxsize=1)
def _pinned_error_metadata() -> dict[str, dict[str, str]]:
    """code -> {recovery, suggestion} from the installed SDK's error-code enum.

    Only the ``recovery`` field is actually read by this module (assert_wire_error
    below) — verified safe to source from the SDK tree: the SDK's enum is a
    strict superset of the older vendored fixture (92 vs 64 codes, fixture-only
    set empty) and its ``recovery`` classification is IDENTICAL across every one
    of the 64 shared codes (0 divergences). ``suggestion`` DOES diverge on 4
    codes between the two sources — but this module never reads that field
    (extract_wire_suggestion below reads the WIRE's own suggestion text, not
    this metadata), so that divergence has no effect here. Consumers that DO
    grade ``suggestion`` content (test_architecture_error_suggestion_enum_conformance.py)
    stay on the vendored fixture — see docs/adcp-spec-version.md "Pinned schema sources"
    (which also cites the command to reproduce the 64-code fixture count).
    """
    return pinned_schema.load("error-code.json")["enumMetadata"]


def extract_wire_suggestion(envelope: dict | None) -> str | None:
    """The buyer-facing ``suggestion`` from a two-layer AdCP wire error envelope.

    STRICT error.json conformance: ``suggestion`` is a top-level sibling of
    code/message/field/retry_after/recovery on the error object (in either the
    ``errors[0]`` or the envelope-level ``adcp_error`` layer). A suggestion
    buried in the free-form ``details`` dict is NOT at the protocol position
    and deliberately does not satisfy this lookup — emitters that bury it are
    conformance bugs the harness must surface, not mask (#1417).
    Single source of truth for both ``TransportResult.assert_wire_error`` and
    the BDD ``_wire_suggestion`` step (#1417). Returns ``None`` when
    there is no envelope (IMPL / no-wire).
    """
    if not envelope:
        return None
    errors = envelope.get("errors") or [{}]
    adcp_error = envelope.get("adcp_error") or {}
    return errors[0].get("suggestion") or adcp_error.get("suggestion")


class Transport(StrEnum):
    """Dispatch transports for behavioral tests."""

    IMPL = "impl"  # Direct _impl() call
    A2A = "a2a"  # _raw() A2A wrapper
    REST = "rest"  # FastAPI TestClient → route → _raw() → _impl()
    MCP = "mcp"  # Mock Context → MCP wrapper → _impl()
    E2E_REST = "e2e_rest"  # Real HTTP via httpx → nginx → server
    E2E_MCP = "e2e_mcp"  # Real MCP via httpx → nginx → server (placeholder)
    E2E_A2A = "e2e_a2a"  # Real A2A via httpx → nginx → server (placeholder)


# Maps Transport → ResolvedIdentity.protocol value
TRANSPORT_PROTOCOL: dict[Transport, str] = {
    Transport.IMPL: "mcp",  # _impl doesn't inspect protocol; keep default
    Transport.A2A: "a2a",
    Transport.REST: "rest",
    Transport.MCP: "mcp",
    Transport.E2E_REST: "rest",
    Transport.E2E_MCP: "mcp",
    Transport.E2E_A2A: "a2a",
}


@dataclass(frozen=True)
class E2EConfig:
    """Configuration for E2E transport dispatch.

    Attributes:
        base_url: Docker stack URL (e.g., ``http://localhost:8092``).
        postgres_url: Docker PostgreSQL URL for factory data writes.
    """

    base_url: str
    postgres_url: str


@dataclass(frozen=True)
class TransportResult:
    """Normalized result from any transport dispatch.

    Attributes:
        payload: Pydantic response model (shared assertions target this).
        envelope: Transport-specific metadata (HTTP status, ToolResult, etc.).
        error: Exception raised during dispatch, if any.
        raw_response: Unprocessed transport response (httpx.Response, ToolResult, etc.).
        wire_response: Serialized success-path response body as a dict, captured
            from the real wire (REST HTTP JSON body, MCP structured_content, A2A
            artifact DataPart). ``None`` on error and on IMPL (no wire — serialize
            the typed ``payload`` instead). Lets success-path tests assert the
            actual serialized shape (e.g. the v3.1 format_id federation contract).
        wire_error_envelope: Raw two-layer error envelope dict captured from
            the actual wire bytes (REST HTTP body, MCP ToolError content text,
            A2A failed-Task artifact DataPart). ``None`` on success or on the
            IMPL transport, which has no wire. This is the canonical field
            for error verification — see ``tests/CLAUDE.md`` § Error
            Verification Policy.
        synthesized_error_envelope: Two-layer envelope produced by
            ``build_two_layer_error_envelope`` against an in-process
            ``AdCPError`` — what production WOULD emit at the boundary.
            Populated for IMPL and as an A2A diagnostic fallback only when
            the harness failed to capture a real wire envelope. Tests
            asserting on this field verify the envelope-builder contract,
            NOT the wire shape — a regression in the production boundary
            translator would not be caught here.
    """

    payload: BaseModel | None = None
    envelope: dict[str, Any] = field(default_factory=dict)
    error: Exception | None = None
    raw_response: Any = None
    wire_response: dict[str, Any] | None = None
    wire_error_envelope: dict[str, Any] | None = None
    synthesized_error_envelope: dict[str, Any] | None = None

    @property
    def is_success(self) -> bool:
        return self.error is None and self.payload is not None

    @property
    def is_error(self) -> bool:
        return self.error is not None

    def wire_dict(self) -> dict[str, Any]:
        """Return the full success-path wire body as the buyer would see it.

        Single source of truth for the anti-tautology guard: BDD's
        ``wire_dict(ctx)`` (``tests/bdd/steps/_outcome_helpers.py``) delegates
        here rather than re-implementing the rule, so an integration test and a
        BDD scenario asserting the same wire shape share one guard instead of
        two copies that can silently drift. A real-wire transport (a2a/mcp/rest/
        e2e_rest) that failed to stash ``wire_response`` raises instead of
        falling through to a re-serialized ``model_dump`` — which would
        normalize away the exact wire-shape regression these oracles exist to
        catch (e.g. an MCP explicit-null leak). Only IMPL legitimately
        has no wire.
        """
        transport = self.envelope.get("transport")
        if self.wire_response is None and transport != "impl":
            raise AssertionError(f"{transport}: wire_response missing — result has no stashed wire body")
        if self.wire_response is not None:
            return self.wire_response
        assert self.payload is not None, "no payload to serialize — dispatch did not succeed"
        return self.payload.model_dump(mode="json")

    def assert_wire_error(
        self,
        code: str,
        *,
        recovery: str | None = None,
        require_suggestion: bool = False,
        message_substr: str | None = None,
        field: str | None = None,
    ) -> None:
        """Assert this result carries the AdCP two-layer wire error ``code``.

        Transport-independent: reads the normalized ``wire_error_envelope`` the
        dispatcher captured for whatever transport produced this result, so the
        same call holds on a2a/mcp/rest. Recovery defaults to the PINNED AdCP
        enum's classification for ``code`` (pin-wins), making the assertion
        non-vacuous without per-scenario duplication. This is the single
        harness-provided way to verify an error on the wire — step definitions
        must not hand-roll envelope parsing.

        ``field`` pins WHICH request field the rejection blames, on both layers
        of the envelope. Two validators sharing one code and one message shape
        are told apart only by this attribute, so without it here every caller
        needing that distinction had to reach into the envelope by hand — the
        parsing this method exists to keep out of test bodies.
        """
        self._assert_error_envelope(
            self.wire_error_envelope,
            code,
            source="wire",
            recovery=recovery,
            require_suggestion=require_suggestion,
            message_substr=message_substr,
            field=field,
        )

    def assert_synthesized_error(
        self,
        code: str,
        *,
        recovery: str | None = None,
        require_suggestion: bool = False,
        message_substr: str | None = None,
    ) -> None:
        """Assert an explicitly non-wire diagnostic error envelope.

        This method is for in-process A2A paths that raise before a Task
        artifact exists. It first proves that no real wire envelope was
        captured, preventing the synthesized builder output from silently
        replacing buyer-visible bytes.
        """
        assert self.wire_error_envelope is None, (
            "A real wire_error_envelope was captured; assert_wire_error() must "
            "be used instead of grading synthesized diagnostic output"
        )
        self._assert_error_envelope(
            self.synthesized_error_envelope,
            code,
            source="synthesized",
            recovery=recovery,
            require_suggestion=require_suggestion,
            message_substr=message_substr,
        )

    def _assert_error_envelope(
        self,
        envelope: dict[str, Any] | None,
        code: str,
        *,
        source: str,
        recovery: str | None,
        require_suggestion: bool,
        message_substr: str | None,
        field: str | None = None,
    ) -> None:
        """Shared code/recovery/suggestion/field assertion for an explicit source."""
        from tests.helpers import assert_envelope_shape

        meta = _pinned_error_metadata()
        spec = meta.get(code)
        assert spec is not None, (
            f"{code!r} is not a canonical AdCP error code (pinned error-code.json). "
            "Reconcile the feature to a canonical code."
        )
        expected_recovery = recovery if recovery is not None else spec["recovery"]

        assert envelope is not None, (
            f"Expected a {source} rejection with {code}, but no {source}_error_envelope was captured "
            f"(is_error={self.is_error}, payload={self.payload!r}). The operation either "
            "succeeded or errored before reaching a transport."
        )
        assert_envelope_shape(envelope, code, recovery=expected_recovery, message_substr=message_substr)
        if require_suggestion:
            suggestion = extract_wire_suggestion(envelope)
            assert suggestion, f"Expected a non-empty suggestion in the {code} {source} envelope: {envelope}"
        if field is not None:
            errors = envelope.get("errors") or []
            assert errors, (
                f"Expected the {code} {source} envelope to blame field {field!r}, "
                f"but it carries no errors entry: {envelope}"
            )
            # Both layers, deliberately: the AdCP envelope mirrors the first error
            # at the top level, and a rejection that names the field in only one of
            # them is a half-populated envelope, not a passing one.
            for layer, block in (("errors[0]", errors[0]), ("adcp_error", envelope.get("adcp_error") or {})):
                assert block.get("field") == field, (
                    f"Expected {layer}.field == {field!r} in the {code} {source} envelope, "
                    f"got {block.get('field')!r}: {envelope}"
                )
