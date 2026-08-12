"""Transport-neutral validation and copying for opaque AdCP context values."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


@dataclass(frozen=True)
class ApplicationContextViolation(ValueError):
    """A context value cannot be represented safely on the wire."""

    message: str
    suggestion: str

    def __str__(self) -> str:
        return self.message


def context_mapping(context: Any) -> dict[Any, Any] | None:
    """Return the opaque mapping carried by a context model or mapping."""
    if context is None:
        return None
    if isinstance(context, BaseModel):
        return context.model_extra or {}
    if isinstance(context, dict):
        return context
    return None


def validate_context_value(context: Any) -> None:
    """Validate acyclicity without imposing non-spec depth or size limits."""
    root = context_mapping(context)
    if root is None:
        return

    stack: list[tuple[str, dict[Any, Any] | list[Any]]] = [("enter", root)]
    active: set[int] = set()
    while stack:
        event, container = stack.pop()
        container_id = id(container)
        if event == "exit":
            active.remove(container_id)
            continue
        if container_id in active:
            raise ApplicationContextViolation(
                "context must be an acyclic JSON object",
                "Remove cyclic references from context and retry.",
            )
        active.add(container_id)
        stack.append(("exit", container))
        values = list(container.values()) if isinstance(container, dict) else container
        for value in reversed(values):
            if isinstance(value, (dict, list)):
                stack.append(("enter", value))


# Values ``json.dumps`` accepts verbatim. ``bool`` is listed explicitly even
# though it subclasses ``int``: membership is checked before the ``float``
# branch, and being explicit keeps the set readable as "the JSON scalars".
_JSON_SAFE_ATOMS = (str, bool, int, type(None))


def _json_safe_atom(value: Any) -> Any:
    """Coerce one non-container value into something every JSON encoder accepts.

    This exists because the boundary must not be able to raise. RFC 8259 has no
    ``NaN``/``Infinity``, but CPython's ``json.loads`` accepts those literals by
    default, so a buyer can put a non-finite float into ``context`` on any
    transport and it arrives here as a real ``float('nan')``. Echoing it back
    "unchanged" is not possible — ``JSONResponse`` (REST) raises ``ValueError:
    Out of range float values are not JSON compliant``, and because every caller
    of this module runs inside an exception handler, that raise SHADOWS the
    buyer's real error and the boundary emits no envelope at all. Non-finite
    floats therefore become JSON ``null``: the key stays present (context echo is
    positional as well as value-wise) and the envelope stays emittable.

    Non-JSON objects — a ``datetime`` reaching us through ``model_extra``, which
    Pydantic's ``mode="json"`` never sees because extras are merged raw — are
    rendered the way ``mode="json"`` would have rendered them, falling back to
    ``str`` so an unknown type still cannot break the encoder.
    """
    if isinstance(value, _JSON_SAFE_ATOMS):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    return str(value)


def _empty_json_container(value: Any) -> dict[Any, Any] | list[Any] | None:
    if isinstance(value, dict):
        return {}
    if isinstance(value, list):
        return []
    return None


def _attach_detached_child(dest: Any, key: Any, item: Any, active: set[int]) -> tuple[Any, Any] | None:
    # A container already on the path from the root closes a cycle: emit null for
    # that edge only, so the parent keeps every other key.
    closes_cycle = isinstance(item, dict | list) and id(item) in active
    child = None if closes_cycle else _empty_json_container(item)
    if closes_cycle:
        detached: Any = None
    else:
        detached = child if child is not None else _json_safe_atom(item)
    if isinstance(dest, dict):
        dest[key] = detached
    else:
        dest.append(detached)
    return (item, child) if child is not None else None


def detach_context_value(value: Any) -> Any:
    """Copy JSON containers iteratively, breaking cycles instead of hanging.

    A container that reaches itself is written as ``null`` at the point the cycle
    closes, and the rest of the structure is copied out in full. JSON has no way
    to express a cycle, so a faithful copy of one is unemittable either way — but
    this function runs on the response and error-formatting paths, where raising
    would shadow the buyer's real error and leave the boundary with no envelope
    at all. Rejecting a cyclic context is the REQUEST boundary's job
    (:func:`validate_context_value`, which still raises); by the time a value
    reaches here the only useful answer is one that can be serialized.

    ``active`` holds only the ids on the path from the root to the node being
    expanded, so a repeated reference that is NOT a cycle (a DAG) is copied out
    in full rather than being mistaken for one.
    """
    root = _empty_json_container(value)
    if root is None:
        return _json_safe_atom(value)

    stack: list[tuple[str, Any, Any]] = [("enter", value, root)]
    active: set[int] = set()
    while stack:
        event, source, dest = stack.pop()
        source_id = id(source)
        if event == "exit":
            active.remove(source_id)
            continue
        active.add(source_id)
        stack.append(("exit", source, dest))
        items = list(source.items()) if isinstance(source, dict) else list(enumerate(source))
        children: list[tuple[Any, Any]] = []
        for key, item in items:
            child = _attach_detached_child(dest, key, item, active)
            if child is not None:
                children.append(child)
        for child_source, child_dest in reversed(children):
            stack.append(("enter", child_source, child_dest))
    return root


def serialize_application_context(context: Any) -> dict[str, Any] | None:
    """Return a detached JSON object without masking an existing error path.

    This is the transport-neutral serialization leaf used by both response and
    exception formatting.  Invalid, cyclic, or non-object values are omitted;
    callers that validate incoming requests translate
    :class:`ApplicationContextViolation` at their transport boundary.
    """
    if context is None:
        return None
    try:
        if isinstance(context, dict):
            return detach_context_value(context)
        if isinstance(context, BaseModel):
            extra = context.model_extra or {}
            declared = context.model_dump(
                mode="json",
                exclude=set(extra),
                exclude_unset=True,
                exclude_none=False,
            )
            return detach_context_value({**declared, **extra})
    except (ApplicationContextViolation, ValueError, TypeError):
        return None
    return None
