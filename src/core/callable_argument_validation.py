"""Validate FastMCP-style callable arguments without invoking the callable.

Pydantic's ``validate_call`` schema combines argument validation and function
execution.  Read idempotency must validate before reserving a key, so this
module extracts only the argument schema and runs it independently.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import copy
from typing import Any

from fastmcp.tools.function_tool import without_injected_parameters
from fastmcp.utilities.types import get_cached_typeadapter
from pydantic_core import SchemaValidator


def validate_callable_arguments(function: Callable[..., Any], arguments: dict[str, Any]) -> None:
    """Validate ``arguments`` against ``function`` without calling ``function``."""
    adapter = get_cached_typeadapter(without_injected_parameters(function))
    schema: dict[str, Any] = dict(copy(adapter.core_schema))
    call_schema = schema.get("schema") if schema.get("type") == "definitions" else schema
    if not isinstance(call_schema, dict) or call_schema.get("type") != "call":
        raise TypeError(f"{function.__name__} does not expose a callable argument schema")
    if schema.get("type") == "definitions":
        schema["schema"] = call_schema["arguments_schema"]
        validation_schema = schema
    else:
        validation_schema = call_schema["arguments_schema"]
    SchemaValidator(validation_schema).validate_python(arguments)
