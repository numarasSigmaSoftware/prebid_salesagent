"""FastMCP middleware for centralized MCP identity resolution.

Makes one authoritative authentication decision per tool call and stores the
resolved identity on FastMCP context state.
Tool functions read the pre-resolved identity via ctx.get_state('identity')
instead of calling resolve_identity_from_context() directly.
"""

import logging

from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult

from src.core.auth_policy import AUTH_OPTIONAL_SKILLS
from src.core.transport_helpers import extract_headers_from_context, resolve_identity_from_context

logger = logging.getLogger(__name__)

# Compatibility alias for callers/tests that import the middleware policy.
# The transport-neutral object is the single source of truth.
AUTH_OPTIONAL_TOOLS = AUTH_OPTIONAL_SKILLS


class MCPAuthMiddleware(Middleware):
    """Resolve identity before tool execution and store on context state.

    After this middleware runs, tools read identity via:
        identity = ctx.get_state('identity')
        context_id = ctx.get_state('context_id')  # may be None
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext,
        call_next,
    ) -> ToolResult:
        tool_name = context.message.name
        require_auth = tool_name not in AUTH_OPTIONAL_TOOLS
        headers = extract_headers_from_context(context.fastmcp_context)

        try:
            identity = resolve_identity_from_context(
                context.fastmcp_context,
                require_valid_token=require_auth,
            )
            if require_auth and (identity is None or not identity.principal_id):
                from src.core.exceptions import classify_auth_credentials_error

                raise classify_auth_credentials_error(
                    headers,
                    missing_message="Authentication required for tool invocation",
                )
        except Exception as error:
            # Identity resolution runs before the decorated tool body, so its
            # failures cannot reach with_error_logging. Reuse both halves of the
            # shared boundary path: record exactly once, then preserve the
            # two-layer MCP wire contract.
            from src.core.exceptions import (
                AdCPAuthenticationError,
                AdCPAuthInvalidError,
                AdCPAuthMissingError,
                classify_auth_credentials_error,
            )
            from src.core.tool_error_logging import (
                _translate_to_tool_error,
                record_boundary_error,
            )

            wire_error = error
            if (
                require_auth
                and isinstance(error, AdCPAuthenticationError)
                and not isinstance(error, (AdCPAuthMissingError, AdCPAuthInvalidError))
            ):
                wire_error = classify_auth_credentials_error(
                    headers,
                    missing_message="Authentication required for tool invocation",
                )

            # A rejected request has no trusted principal. Client-controlled
            # host/x-adcp-tenant headers are routing hints, not proof that the
            # caller may write into that tenant's activity or audit sinks.
            record_boundary_error(
                "mcp",
                tool_name,
                error,
                tenant_id=None,
                principal_id=None,
            )
            _translate_to_tool_error(wire_error)

        if context.fastmcp_context:
            await context.fastmcp_context.set_state("identity", identity, serializable=False)

            # Raw wire arguments, captured BEFORE RequestCompatMiddleware
            # normalizes them — the idempotency payload-hash input (AdCP defines
            # payload equivalence over the request as the buyer sent it).
            if context.message.arguments is not None:
                await context.fastmcp_context.set_state(
                    "raw_wire_payload", dict(context.message.arguments), serializable=False
                )

            # Extract x-context-id from HTTP headers for tools that need it
            try:
                headers = get_http_headers(include_all=True) or {}
                ctx_id = headers.get("x-context-id")
                if ctx_id:
                    await context.fastmcp_context.set_state("context_id", ctx_id, serializable=False)
            except Exception:
                logger.debug("Could not set context_id state", exc_info=True)

        return await call_next(context)
