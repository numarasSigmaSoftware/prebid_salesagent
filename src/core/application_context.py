"""Lossless AdCP application-context serialization at transport boundaries.

AdCP ``context`` is an opaque JSON object.  Generated SDK models default to
``exclude_none=True`` when dumped, which is appropriate for schema-owned
optional fields but not for caller-owned context: an explicitly supplied JSON
``null`` is data and must survive the request/response round trip.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from src.core.application_context_values import (
    ApplicationContextViolation,
    detach_context_value,
    serialize_application_context,
    validate_context_value,
)
from src.core.exceptions import AdCPValidationError

_CONTEXT_UNSET = object()


def validate_application_context(context: Any) -> None:
    """Translate a transport-neutral value violation to the AdCP error vocabulary."""
    try:
        validate_context_value(context)
    except ApplicationContextViolation as exc:
        raise AdCPValidationError(
            exc.message,
            field="context",
            suggestion=exc.suggestion,
            context=None,
        ) from exc


def _response_context(response: Any) -> Any:
    """Find a direct or flattened-wrapper application context."""
    direct = getattr(response, "context", None)
    if direct is not None:
        return direct

    # create/update result wrappers flatten their typed ``response`` model at
    # the wire boundary, so the application context lives one level down.
    nested_response = getattr(response, "response", None)
    return getattr(nested_response, "context", None)


def _response_with_context_cleared(response: Any) -> Any:
    """Return a shallow copy of ``response`` with any (nested) context value blanked.

    ``model_dump()`` on a response carrying a pathologically deep context
    trips Pydantic's own serializer recursion guard — ``core/context.json``
    sets no depth ceiling, and ``_detach`` (this module's own iterative
    serializer) has none either, so nothing upstream bounds how deep a buyer
    can nest it. Left unhandled, the crash happens before this module's own
    safe iterative serialization ever runs, because ``model_dump()`` walks the
    whole model, context included. Clearing the field first (via
    ``model_copy``, which does not re-run validators) removes the one
    unbounded value from Pydantic's walk;
    the real serialized context is injected back into the dumped dict
    afterward by the caller. ``exclude={"context": ...}`` was considered and
    rejected: ``CreateMediaBuyResult``/``UpdateMediaBuyResult`` flatten their
    inner ``response`` via a custom ``model_serializer(mode="wrap")`` that
    calls ``self.response.model_dump()`` directly, without forwarding an
    externally supplied ``exclude=`` into that nested call — so ``exclude``
    cannot reliably reach a flattened wrapper's context, while replacing the
    field value up front works uniformly for direct and wrapped shapes alike.
    A response with no context field, or an already-None one, is returned
    unchanged (``getattr`` degrades to ``None``, and clearing ``None`` to
    ``None`` would be a no-op copy anyway).

    Only engages for genuine ``BaseModel`` instances. A test double (e.g. a
    bare ``unittest.mock.MagicMock()`` standing in for a response, common
    across this codebase's transport-wrapper tests) auto-creates ANY attribute
    access as a truthy child mock — ``getattr(mock, "context", None)`` never
    legitimately returns ``None`` — so the unguarded version of this check
    always took the "clear" branch, called the mock's own auto-mocked
    ``model_copy()``, and returned a DIFFERENT child mock whose
    ``model_dump()`` no longer carries whatever return value the test
    configured. Non-``BaseModel`` responses fall through unchanged, matching
    this function's pre-existing behavior for them.
    """
    if not isinstance(response, BaseModel):
        return response
    if getattr(response, "context", None) is not None:
        return response.model_copy(update={"context": None})
    nested_response = getattr(response, "response", None)
    if (
        nested_response is not None
        and isinstance(nested_response, BaseModel)
        and getattr(nested_response, "context", None) is not None
    ):
        cleared_nested = nested_response.model_copy(update={"context": None})
        return response.model_copy(update={"response": cleared_nested})
    return response


def dump_adcp_response(
    response: Any,
    *,
    context: Any = _CONTEXT_UNSET,
    mode: str = "json",
    **kwargs: Any,
) -> dict[str, Any]:
    """Serialize a response and restore its opaque application context exactly.

    The response model keeps its canonical serializer for every schema-owned
    field. Only the root ``context`` member is overwritten with the lossless
    application-context representation. ``context=`` may be supplied by a
    transport wrapper; otherwise a direct response context (or the context on a
    flattened result wrapper's inner response) is used.

    The model is dumped with its context (direct or flattened-wrapper) blanked
    first — see ``_response_with_context_cleared`` — so a pathologically deep
    context cannot crash Pydantic's own serializer before the safe iterative
    path below ever runs; the real context is restored afterward.
    """
    if isinstance(response, dict):
        try:
            data = detach_context_value(response)
        except ApplicationContextViolation:
            data = {}
    else:
        data = _response_with_context_cleared(response).model_dump(mode=mode, **kwargs)

    candidate = _response_context(response) if context is _CONTEXT_UNSET else context
    serialized = serialize_application_context(candidate)
    if serialized is not None:
        data["context"] = serialized
    return data


__all__ = [
    "dump_adcp_response",
    "serialize_application_context",
    "validate_application_context",
]
