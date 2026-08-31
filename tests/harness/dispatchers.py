"""Dispatcher classes — one per transport.

Each dispatcher calls the env's transport-specific method and wraps the
result in a TransportResult. The env subclass provides the actual call logic;
the dispatcher only handles result wrapping and error capture.

On error, dispatchers capture the wire error envelope (the raw two-layer dict
the buyer would see) alongside the reconstructed exception.  New tests should
assert on ``result.wire_error_envelope`` via ``assert_envelope_shape()`` — see
``tests/CLAUDE.md`` § Error Verification Policy.

Usage (internal — called by BaseTestEnv.call_via)::

    dispatcher = DISPATCHERS[Transport.A2A]
    result = dispatcher.dispatch(env, **kwargs)
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from tests.harness.transport import (
    Transport,
    TransportResult,
    _envelope_from_adcp_error,
    derive_error_status,
)

if TYPE_CHECKING:
    from tests.harness._base import BaseTestEnv

# _envelope_from_adcp_error lives in transport.py, not here — both this module
# and client.py need it, and housing it in either would recreate the mutual
# lazy-import cycle untangled later (client.py used to lazily
# import it back from this module, while this module lazily imports dispatch
# functions FROM client.py — the two together being the "mutual" part).
#
# The MCP/A2A/REST error-path unwraps themselves are NOT re-implemented here:
# this module delegates to client.py's unwrap_mcp_error / unwrap_a2a_error /
# unwrap_rest_error, so there is one error unwrap per transport family for both
# dispatch paths (CLAUDE.md DRY invariant; remediation finding 1).


class ImplDispatcher:
    """Dispatch via direct ``_impl()`` call.

    IMPL is the in-process direct call — there is no wire by definition.
    ``wire_error_envelope`` is left ``None`` on this transport; the envelope
    that production WOULD emit at the boundary is exposed on the separate
    ``synthesized_error_envelope`` field so tests cannot accidentally lean
    on IMPL to catch real-wire regressions (a regression in the production
    boundary translator would not change what this dispatcher computes,
    because both call ``build_two_layer_error_envelope`` on the same
    in-memory exception). Use A2A, REST, or MCP for wire-shape coverage.
    """

    def dispatch(self, env: BaseTestEnv, **kwargs: Any) -> TransportResult:
        try:
            payload = env.call_impl(**kwargs)
        except Exception as exc:
            return TransportResult(
                error=exc,
                synthesized_error_envelope=_envelope_from_adcp_error(exc),
            )
        return TransportResult(payload=payload, envelope={"transport": "impl"})


class A2ADispatcher:
    """Dispatch via ``env.call_a2a`` — the A2A transport wrapper.

    Two A2A call shapes exist across envs:

    - **Full pipeline** (``_run_a2a_handler``): drives
      ``AdCPRequestHandler.on_message_send`` end-to-end (message parsing →
      skill routing → handler dispatch → ``_serialize_for_a2a`` →
      Task/Artifact framing). On a failed Task, the harness reconstructs the
      ``AdCPError`` from the artifact DataPart and stashes the REAL wire
      envelope on the exception via ``_wire_error_envelope`` — surfaced here
      as ``wire_error_envelope``.
    - **Direct raw** (``*_raw``): no Task framing, so no stash. There is no
      real wire; ``wire_error_envelope`` is ``None`` and the envelope
      production WOULD emit is exposed on ``synthesized_error_envelope``.

    Fields are split like ``McpDispatcher``: ``wire_error_envelope`` holds
    ONLY captured wire (``None`` when the raw path was taken), and
    ``synthesized_error_envelope`` always carries the boundary-builder output
    for the caught exception. This keeps ``wire_error_envelope`` honest — a
    raw-path env cannot masquerade its synthesized output as real wire.

    This is why the error branch below does NOT delegate to ``client.py``'s
    ``unwrap_a2a_error``: that helper folds the two fields back into one
    (``_wire_envelope_from_exception`` falls back to the synthesized envelope
    and publishes it as ``wire_error_envelope``), which is exactly the
    masquerade the split exists to prevent.
    """

    def dispatch(self, env: BaseTestEnv, **kwargs: Any) -> TransportResult:
        try:
            delivered = env.deliver_a2a(**kwargs)
        except Exception as exc:
            # `_wire_error_envelope` is set ONLY by the full-pipeline Task/
            # Artifact reconstruction (tests.harness._base's envelope->
            # exception helper) — a direct-raw exception (e.g. CreativeSyncEnv,
            # which raises straight from *_raw()) never has the attribute at
            # all. Its PRESENCE (not its value) is therefore the per-call
            # signal for "this dispatch mode promised to capture wire" —
            # exposed on TransportResult so callers like
            # tests/bdd/steps/_outcome_helpers.wire_error_envelope can tell a
            # legitimate no-wire dispatch apart from a stashing bug.
            wire_capture_promised = hasattr(exc, "_wire_error_envelope")
            wire = getattr(exc, "_wire_error_envelope", None)
            return TransportResult(
                error=exc,
                # Derived per-transport status: the A2A evidence is whether a
                # failed Task carried an AdCP envelope in its artifact DataPart.
                envelope={"transport": Transport.A2A.value, "status": derive_error_status(wire)},
                # Stash only — None on the raw path (no Task framing).
                wire_error_envelope=wire,
                # What production WOULD emit for the same exception; the only
                # honest error envelope for raw-path envs (e.g. CreativeSyncEnv).
                synthesized_error_envelope=_envelope_from_adcp_error(exc),
                wire_capture_unavailable=not wire_capture_promised,
            )
        # Real A2A wire: the artifact DataPart dict, carried back on the SAME
        # return value as the payload. It used to be read off env._last_wire_response
        # — one object reaching into another's private attribute, which is what
        # allowed a second writer and a stale wire.
        return TransportResult(
            payload=delivered.payload,
            envelope={"transport": "a2a"},
            wire_response=delivered.wire_response,
        )


class RestDispatcher:
    """Dispatch via FastAPI TestClient → route → _raw() → _impl().

    Identity flows through kwargs to env._run_rest_request(), which pops it
    and configures the FastAPI auth dep override per-request.

    Unlike other dispatchers, REST includes HTTP metadata in the envelope
    (status_code, content_type) since tests may assert on these.
    """

    def dispatch(self, env: BaseTestEnv, **kwargs: Any) -> TransportResult:
        from tests.harness.client import unwrap_rest_error, unwrap_rest_response

        try:
            endpoint = env.REST_ENDPOINT  # type: ignore[attr-defined]
            response = env._run_rest_request(endpoint, **kwargs)
        except Exception as exc:
            # ONE REST DELIVER-exception unwrap for both dispatch paths — it
            # derives status=transport_fault, because an exception here means no
            # HTTP body, hence no AdCP envelope, ever existed.
            return unwrap_rest_error(exc, Transport.REST)
        # unwrap_rest_response owns the status-code
        # branching, envelope tag, and the #1417 pristine-wire deepcopy rule —
        # the same function RestE2EDispatcher and the generic client's
        # _unwrap_rest delegate to below.
        return unwrap_rest_response(env, response, Transport.REST, env.parse_rest_response)


class McpDispatcher:
    """Dispatch via Client(mcp) — full FastMCP pipeline.

    Identity flows through kwargs to env.call_mcp() → _run_mcp_client(),
    which pops it and dispatches via FastMCP in-memory transport.
    """

    def dispatch(self, env: BaseTestEnv, **kwargs: Any) -> TransportResult:
        try:
            delivered = env.deliver_mcp(**kwargs)
        except Exception as exc:
            from tests.harness.client import unwrap_mcp_error

            # ONE MCP error unwrap for both dispatch paths (client.py) — it owns
            # the raw-ToolError unwrap, the REAL-wire-only envelope rule (never
            # the synthesized fallback), and the derived status. Unlike the A2A
            # sibling above, this helper already keeps wire and synthesized on
            # SEPARATE fields, so delegating here preserves the split.
            unwrapped = unwrap_mcp_error(exc, Transport.MCP)
            # The raw CallToolResult has no channel on DeliverResult, so it is
            # re-attached here from the env's per-call stash (see
            # BaseTestEnv._run_mcp_client) for authenticity checks.
            return replace(unwrapped, raw_response=env._last_mcp_raw_response)
        # Real MCP wire: the structured_content dict, carried back on the SAME
        # return value as the payload — see the A2A sibling above.
        return TransportResult(
            payload=delivered.payload,
            envelope={"transport": "mcp"},
            raw_response=env._last_mcp_raw_response,
            wire_response=delivered.wire_response,
        )


class RestE2EDispatcher:
    """Dispatch via real HTTP through nginx to the Docker stack.

    Exercises the full stack: nginx -> UnifiedAuthMiddleware ->
    resolve_identity() -> get_principal_from_token() DB lookup -> route
    handler -> _impl().

    WRAP (``env.build_rest_body`` / ``env.REST_ENDPOINT`` / ``env.REST_METHOD``)
    stays the per-env contract every dispatch path already uses — migrating
    that to the generic ``tests.harness.client._wrap_rest`` would require
    rewriting each env's bespoke request-shaping (e.g. ``MediaBuyDualEnv``'s
    create/update routing), an explicit non-goal of the transport-generic
    client design (see ``tests/harness/client.py``).

    DELIVER (the httpx call) stays here rather than delegating to
    ``tests.harness.client._deliver_e2e_rest``: that function derives its
    headers via ``e2e_identity_headers``, which reads ``identity.auth_token``
    and therefore cannot carry a ``WireAuth`` override (raw headers, no
    resolved identity), and its ``_rest_request_kwargs`` omits a body for
    bodiless verbs without turning it into ``params=``. Both are pinned by
    ``tests/harness/test_harness_base.py::
    test_e2e_rest_dispatcher_forwards_wire_auth_and_uses_method_payload``.
    Folding the two back together requires teaching those client helpers about
    ``WireAuth`` and GET query params.

    UNWRAP (the status-code/envelope handling) delegates to
    ``tests.harness.client.unwrap_rest_response`` —
    the one REST unwrap shared with the in-process ``RestDispatcher`` and the
    generic client's ``_unwrap_rest``. It derives the envelope tag from
    ``Transport.E2E_REST.value`` (``"e2e_rest"``) and keeps the graceful
    non-JSON-body fallback (#1420) that e2e_rest — the only e2e transport
    running today — has always had as its regression baseline.

    Ported from feature/media-buy-refactoring (PR #1360 lineage).
    """

    def dispatch(self, env: BaseTestEnv, **kwargs: Any) -> TransportResult:
        import httpx

        from tests.harness.client import e2e_identity_headers, unwrap_rest_response
        from tests.harness.transport import NO_IDENTITY_OVERRIDE, WireAuth

        if not env.e2e_config:
            return TransportResult(error=RuntimeError("E2E dispatch requires env.e2e_config (pass e2e_config= to env)"))

        # NO_IDENTITY_OVERRIDE default (not None): an omitted identity must fall
        # back to env.identity_for(transport), the same resolution every other
        # transport's omitted-identity dispatch gets — a bare ``None`` default
        # here would force every omitted-identity call unauthenticated instead.
        identity = kwargs.pop("identity", NO_IDENTITY_OVERRIDE)
        resolved_identity = env.identity_for(Transport.E2E_REST) if identity is NO_IDENTITY_OVERRIDE else identity

        # A WireAuth override carries RAW wire headers and no resolved identity,
        # so e2e_identity_headers (which reads identity.auth_token) cannot
        # express it. Every other identity shape — including an explicit None,
        # meaning "send without auth headers and let the live server's auth
        # middleware produce the real 401" — goes through the shared builder.
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if isinstance(resolved_identity, WireAuth):
            headers.update(resolved_identity.headers)
        else:
            headers.update(e2e_identity_headers(resolved_identity))

        body = env.build_rest_body(**kwargs)
        endpoint = env.REST_ENDPOINT  # type: ignore[attr-defined]
        method = getattr(env, "REST_METHOD", "post")

        # Bodiless verbs carry the request as query params, not a JSON body
        # (e.g. GET /capabilities); client._rest_request_kwargs only drops the
        # body for those verbs and never re-homes it as params.
        request_kwargs: dict[str, Any] = {"headers": headers}
        if method == "get":
            request_kwargs["params"] = body
        else:
            request_kwargs["json"] = body

        with httpx.Client(base_url=env.e2e_config.base_url, timeout=30) as client:
            response = getattr(client, method)(endpoint, **request_kwargs)

        return unwrap_rest_response(env, response, Transport.E2E_REST, env.parse_rest_response)


class McpE2EDispatcher:
    """Dispatch via real HTTP through nginx to the Docker stack's MCP endpoint.

    Delegates to ``AdCPTestClient`` (``tests/harness/client.py``,
    the wire-grading work) instead of duplicating the
    ADDRESS/WRAP/DELIVER/UNWRAP logic here a second time — ``client.call()``
    already builds the real ``fastmcp.Client`` against
    ``env.e2e_config.base_url`` and unwraps the response identically to the
    in-process ``McpDispatcher`` above (design doc §5).

    Unlike the other dispatchers on this legacy ``env.call_via(transport,
    **kwargs)`` path, per-env subclasses hardcode their MCP tool name as a
    string literal inside ``call_mcp()`` (e.g. ``ProductEnv.call_mcp`` calls
    ``self._run_mcp_client("get_products", ...)``) — there is no attribute to
    introspect it from generically, and unlike ``RestE2EDispatcher`` (which
    reads ``env.REST_ENDPOINT``/``env.REST_METHOD``) no env exposes an MCP
    equivalent. This dispatcher was a ``NotImplementedError`` placeholder with
    zero callers (no env ever dispatched ``Transport.E2E_MCP`` through
    ``call_via``), so this is not a breaking-change surface: callers must pass
    ``tool_name=`` explicitly in kwargs, the same tool identity
    ``AdCPTestClient.call()``'s first positional argument already requires.
    """

    def dispatch(self, env: BaseTestEnv, **kwargs: Any) -> TransportResult:
        from tests.harness.client import _dispatch_core, flatten_payload
        from tests.harness.transport import NO_IDENTITY_OVERRIDE, MissingToolNameError, Transport

        tool_name = kwargs.pop("tool_name", None)
        if tool_name is None:
            raise MissingToolNameError(
                "McpE2EDispatcher.dispatch requires tool_name= in kwargs (e.g. "
                'env.call_via(Transport.E2E_MCP, tool_name="get_products", req=...)) — '
                "there is no per-env attribute to derive it from generically. "
                "Prefer AdCPTestClient(env).call(tool_name, payload, Transport.E2E_MCP) directly."
            )

        identity = kwargs.pop("identity", NO_IDENTITY_OVERRIDE)
        req = kwargs.pop("req", None)
        payload = flatten_payload(req, **kwargs)

        return _dispatch_core(env, Transport.E2E_MCP, tool_name, payload, identity)


class A2AE2EDispatcher:
    """Dispatch via a real JSON-RPC ``message/send`` HTTP request to the live A2A endpoint.

    Unlike ``RestE2EDispatcher`` (which reuses each env's hand-written
    ``REST_ENDPOINT``/``build_rest_body``/``parse_rest_response`` overrides),
    this delegates entirely to ``AdCPTestClient``/``_deliver_e2e_a2a``
    (``tests/harness/client.py``, the wire-grading work) — the address,
    JSON-RPC envelope construction, and Task-state handling all live there,
    derived from the live ``create_agent_card()`` registration
    (``tests/harness/address_table.py``), not re-implemented per-env.

    Tool-name threading: unlike ``AdCPTestClient.call(tool, payload,
    transport)`` (which takes the tool name explicitly), the legacy
    ``env.call_via(transport, **kwargs)`` entry point this dispatcher is
    reached through carries no tool-name parameter — every OTHER dispatcher
    sidesteps this because the env subclass's own ``call_a2a``/``call_mcp``/
    ``call_rest`` override already has the tool name hard-coded in its body
    (e.g. ``self._run_a2a_handler("get_products", ...)``). Since this
    dispatcher must call the generic client instead of an env override, the
    caller supplies the tool name explicitly via a ``tool_name=`` kwarg (or
    an ``env.A2A_SKILL`` class attribute, for envs that want to declare it
    once) — same open question the wire-grading work's ``McpE2EDispatcher`` faces for
    ``Transport.E2E_MCP``, resolved independently here since neither
    dispatcher's fix depends on the other's.
    """

    def dispatch(self, env: BaseTestEnv, **kwargs: Any) -> TransportResult:
        from tests.harness.client import _dispatch_core, flatten_payload
        from tests.harness.transport import NO_IDENTITY_OVERRIDE, MissingToolNameError, Transport

        identity = kwargs.pop("identity", NO_IDENTITY_OVERRIDE)
        tool_name = kwargs.pop("tool_name", None) or getattr(env, "A2A_SKILL", None)
        if not tool_name:
            raise MissingToolNameError(
                "A2AE2EDispatcher.dispatch() needs a tool/skill name to resolve an address via "
                "AdCPTestClient — pass tool_name=... to env.call_via(Transport.E2E_A2A, ...) (or "
                "declare env.A2A_SKILL), or call AdCPTestClient(env).call(tool, payload, "
                "Transport.E2E_A2A) directly instead — the primary path this design promotes "
                "(see tests/harness/client.py — rewriting per-env shaping is a non-goal)."
            )

        req = kwargs.pop("req", None)
        payload = flatten_payload(req, **kwargs)

        return _dispatch_core(env, Transport.E2E_A2A, tool_name, payload, identity)


DISPATCHERS: dict[
    Transport,
    ImplDispatcher
    | A2ADispatcher
    | RestDispatcher
    | McpDispatcher
    | RestE2EDispatcher
    | McpE2EDispatcher
    | A2AE2EDispatcher,
] = {
    Transport.IMPL: ImplDispatcher(),
    Transport.A2A: A2ADispatcher(),
    Transport.REST: RestDispatcher(),
    Transport.MCP: McpDispatcher(),
    Transport.E2E_REST: RestE2EDispatcher(),
    Transport.E2E_MCP: McpE2EDispatcher(),
    Transport.E2E_A2A: A2AE2EDispatcher(),
}
