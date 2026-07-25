"""Transport-neutral validation and copying for opaque AdCP context values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

MAX_APPLICATION_CONTEXT_DEPTH = 64
MAX_APPLICATION_CONTEXT_NODES = 10_000


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
    """Validate size, depth, and acyclicity without recursive Python calls."""
    root = context_mapping(context)
    if root is None:
        return

    stack: list[tuple[str, dict[Any, Any] | list[Any], int]] = [("enter", root, 1)]
    active: set[int] = set()
    nodes = 0
    while stack:
        event, container, depth = stack.pop()
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
        stack.append(("exit", container, depth))
        if depth > MAX_APPLICATION_CONTEXT_DEPTH:
            raise ApplicationContextViolation(
                f"context exceeds the maximum nesting depth of {MAX_APPLICATION_CONTEXT_DEPTH}",
                "Flatten deeply nested context values or store the large object externally and pass a stable reference.",
            )
        values = list(container.values()) if isinstance(container, dict) else container
        for value in reversed(values):
            nodes += 1
            if nodes > MAX_APPLICATION_CONTEXT_NODES:
                raise ApplicationContextViolation(
                    f"context exceeds the maximum size of {MAX_APPLICATION_CONTEXT_NODES} values",
                    "Reduce context size or pass a stable external reference.",
                )
            if isinstance(value, (dict, list)):
                stack.append(("enter", value, depth + 1))


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
