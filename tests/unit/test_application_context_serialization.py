"""AdCP application context is an opaque, lossless JSON object."""

from typing import Any

import pytest
from adcp.types import ContextObject

from src.core.application_context import (
    MAX_APPLICATION_CONTEXT_DEPTH,
    dump_adcp_response,
    serialize_application_context,
    validate_application_context,
)
from src.core.exceptions import AdCPValidationError, build_two_layer_error_envelope
from src.core.schemas._base import CreateMediaBuyResult, CreateMediaBuySuccess
from src.core.schemas.product import GetProductsResponse


def _nested_context(depth: int) -> dict[str, Any]:
    """Build a context nested exactly ``depth`` objects deep."""
    root: dict[str, Any] = {}
    cursor = root
    for _ in range(depth - 1):
        cursor["nested"] = {}
        cursor = cursor["nested"]
    cursor["leaf"] = "value"
    return root


def test_typed_context_preserves_explicit_nulls_without_synthesizing_fields() -> None:
    raw = {
        "correlation_id": "ctx-null",
        "nullable": None,
        "nested": {"value": None},
    }
    context = ContextObject.model_validate(raw)

    assert serialize_application_context(context) == raw
    assert serialize_application_context(ContextObject.model_validate({})) == {}


def test_plain_context_is_detached_recursively() -> None:
    raw = {"nested": {"value": None}}
    serialized = serialize_application_context(raw)

    raw["nested"]["value"] = "mutated"
    assert serialized == {"nested": {"value": None}}


def test_cyclic_context_is_rejected_and_safe_serialization_drops_it() -> None:
    raw: dict[str, Any] = {}
    raw["self"] = raw

    with pytest.raises(AdCPValidationError, match="acyclic"):
        validate_application_context(raw)

    assert serialize_application_context(raw) is None


def test_cyclic_sequence_is_rejected_and_error_dump_cannot_hang() -> None:
    sequence: list[Any] = []
    sequence.append(sequence)
    raw = {"sequence": sequence}

    with pytest.raises(AdCPValidationError, match="acyclic"):
        validate_application_context(raw)

    assert serialize_application_context(raw) is None
    assert dump_adcp_response(raw) == {}
    error = AdCPValidationError("invalid request", field="context", context=raw)
    envelope = build_two_layer_error_envelope(error)
    assert "context" not in envelope
    assert envelope["adcp_error"]["code"] == "VALIDATION_ERROR"
    assert envelope["errors"][0]["code"] == "VALIDATION_ERROR"


def test_shared_noncyclic_container_is_copied_per_occurrence() -> None:
    shared = {"value": 1}
    raw = {"left": shared, "right": shared}

    serialized = serialize_application_context(raw)

    assert serialized == raw
    assert serialized["left"] is not serialized["right"]


def test_deeply_nested_plain_context_survives_intact() -> None:
    """The low-level iterative copier itself remains recursion-independent."""
    raw = _nested_context(5000)

    assert serialize_application_context(raw) == raw


def test_deeply_nested_typed_context_survives_intact() -> None:
    """The ``ContextObject`` branch must not hand a deep structure to Pydantic's
    own serializer, whose internal recursion guard trips independently of
    Python's — reading ``model_extra`` directly and detaching it ourselves
    sidesteps that guard entirely.
    """
    raw = _nested_context(5000)
    context = ContextObject.model_validate(raw)

    assert serialize_application_context(context) == raw


def test_application_context_accepts_the_transport_safe_depth_limit() -> None:
    validate_application_context(_nested_context(MAX_APPLICATION_CONTEXT_DEPTH))


def test_application_context_rejects_depth_before_business_logic_runs() -> None:
    with pytest.raises(AdCPValidationError, match="maximum nesting depth") as exc_info:
        validate_application_context(_nested_context(MAX_APPLICATION_CONTEXT_DEPTH + 1))

    assert exc_info.value.field == "context"
    assert exc_info.value.context is None


def test_response_dump_restores_lossless_context_and_omits_absence() -> None:
    raw = {"nullable": None, "nested": {"value": None}}
    with_context = GetProductsResponse(
        products=[],
        context=ContextObject.model_validate(raw),
    )
    without_context = GetProductsResponse(products=[])

    assert dump_adcp_response(with_context)["context"] == raw
    assert "context" not in dump_adcp_response(without_context)


def test_deeply_nested_context_on_a_successful_direct_response_survives_intact() -> None:
    """A SUCCESS path must not crash just because the ERROR path was fixed.

    ``dump_adcp_response`` used to call ``response.model_dump()`` on the whole
    model BEFORE this module's own safe iterative serialization ever ran —
    Pydantic's own recursion guard trips walking the deep context field before
    the safe path is reached, regardless of how deep-context handling was
    fixed elsewhere. Reproduced directly against ``GetProductsResponse``.
    """
    raw = _nested_context(3000)
    response = GetProductsResponse(products=[], context=ContextObject.model_validate(raw))

    assert dump_adcp_response(response)["context"] == raw


def test_mocked_response_still_uses_its_own_configured_model_dump() -> None:
    """A bare ``MagicMock()`` response must not be silently swapped out.

    Regression guard: ``getattr(mock, "context", None)`` never legitimately
    returns ``None`` on an unconfigured ``MagicMock`` — every attribute access
    auto-creates a truthy child mock — so an unguarded "clear context, then
    model_copy" step took the clear branch, called the mock's own auto-mocked
    ``model_copy()``, and returned a DIFFERENT child mock whose
    ``model_dump()`` no longer carried the value the test configured. Many
    transport-wrapper tests in this codebase stub ``_impl`` with a bare
    ``MagicMock`` this way.
    """
    from unittest.mock import MagicMock

    mock_response = MagicMock()
    mock_response.model_dump.return_value = {"products": [], "metadata": {}}

    assert dump_adcp_response(mock_response) == {"products": [], "metadata": {}}


def test_deeply_nested_context_on_a_flattened_wrapper_response_survives_intact() -> None:
    """The flattened-wrapper shape needs its own oracle: ``exclude=`` cannot reach it.

    ``CreateMediaBuyResult`` (and its update sibling) flatten their typed
    ``response`` via a custom ``model_serializer(mode="wrap")`` that calls
    ``self.response.model_dump()`` directly, without forwarding an externally
    supplied ``exclude=`` into that nested call — so a fix that only excludes
    a top-level ``context`` field would silently fail to protect this shape.
    """
    raw = _nested_context(3000)
    success = CreateMediaBuySuccess(media_buy_id="mb-1", packages=[], context=ContextObject.model_validate(raw))
    result = CreateMediaBuyResult(response=success, status="completed")

    assert dump_adcp_response(result)["context"] == raw
