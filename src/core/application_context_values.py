"""Transport-neutral validation and copying for opaque AdCP context values."""

from __future__ import annotations

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


def _empty_json_container(value: Any) -> dict[Any, Any] | list[Any] | None:
    if isinstance(value, dict):
        return {}
    if isinstance(value, list):
        return []
    return None


def _attach_detached_child(dest: Any, key: Any, item: Any) -> tuple[Any, Any] | None:
    child = _empty_json_container(item)
    detached = child if child is not None else item
    if isinstance(dest, dict):
        dest[key] = detached
    else:
        dest.append(detached)
    return (item, child) if child is not None else None


def detach_context_value(value: Any) -> Any:
    """Copy JSON containers iteratively and reject cycles instead of hanging."""
    root = _empty_json_container(value)
    if root is None:
        return value

    stack: list[tuple[str, Any, Any]] = [("enter", value, root)]
    active: set[int] = set()
    while stack:
        event, source, dest = stack.pop()
        source_id = id(source)
        if event == "exit":
            active.remove(source_id)
            continue
        if source_id in active:
            raise ApplicationContextViolation(
                "context must be an acyclic JSON object",
                "Remove cyclic references from context and retry.",
            )
        active.add(source_id)
        stack.append(("exit", source, dest))
        items = list(source.items()) if isinstance(source, dict) else list(enumerate(source))
        children: list[tuple[Any, Any]] = []
        for key, item in items:
            child = _attach_detached_child(dest, key, item)
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
