"""Shared MCP transport-wrapper helper for building ``ToolResult`` responses."""

from __future__ import annotations

from typing import Any

from adcp.types.base import AdCPBaseModel
from fastmcp.tools.tool import ToolResult

# The same sentinel object ``dump_adcp_response`` compares against by identity:
# "no context supplied by the wrapper, derive it from the response". A private
# copy here would be a different object and would silently disable that
# derivation, so the import is deliberate.
from src.core.application_context import _CONTEXT_UNSET, dump_adcp_response


def mcp_result(response: AdCPBaseModel, content: str | None = None, *, context: Any = _CONTEXT_UNSET) -> ToolResult:
    """Build a ``ToolResult`` with a spec-compliant ``structured_content``.

    ``structured_content`` must be a plain dict via ``model_dump()``: FastMCP's
    ``ToolResult`` serializes non-dict ``structured_content`` via
    ``pydantic_core.to_jsonable_python()``, which bypasses ``model_dump()``
    overrides (Pattern #4 nested serialization) and ``AdCPBaseModel``'s
    ``exclude_none=True`` default -- so protocol/spec-optional fields the model
    leaves unset would otherwise serialize as invalid wire ``null`` instead of
    being omitted.

    The parameter is bound to ``AdCPBaseModel``, not ``pydantic.BaseModel``,
    because that ``exclude_none=True`` default IS the contract this helper
    exists to preserve. A plain pydantic model routed through here would
    type-check, re-leak the nulls, and still satisfy every "did it go through
    mcp_result?" structural check -- so the bound is where it has to be caught.

    The dump goes through ``dump_adcp_response`` rather than
    ``response.model_dump(mode="json")`` directly so the opaque application
    ``context`` survives the round trip byte-exact (an explicitly supplied JSON
    ``null`` is data, and ``exclude_none=True`` would drop it), and so an
    ABSENT context is echoed as absence rather than as ``context: null``.
    ``context=`` is the value the wrapper received from the caller; omitting it
    falls back to whatever context the response itself carries.
    """
    return ToolResult(
        content=content if content is not None else str(response),
        structured_content=dump_adcp_response(response, context=context),
    )
