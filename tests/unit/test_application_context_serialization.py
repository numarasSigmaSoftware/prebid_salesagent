"""AdCP application context is an opaque, lossless JSON object."""

import datetime
import json
import math
from typing import Any

import pytest
from adcp.types import ContextObject

from src.core.application_context import (
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


def test_cyclic_context_is_rejected_at_the_request_boundary_and_emittable_after() -> None:
    """Rejection is the request boundary's job; serialization must still emit.

    The two halves defend different things. ``validate_application_context`` is
    what a buyer's cyclic context hits, and it raises. ``serialize_application_context``
    runs on the response and error paths, where raising would shadow the real
    error and leave the boundary with no envelope — so it breaks the cycle and
    emits the rest.
    """
    raw: dict[str, Any] = {}
    raw["self"] = raw

    with pytest.raises(AdCPValidationError, match="acyclic"):
        validate_application_context(raw)

    serialized = serialize_application_context(raw)
    assert serialized == {"self": None}
    assert json.dumps(serialized) == '{"self": null}'


def test_cyclic_sequence_is_rejected_and_error_dump_cannot_hang() -> None:
    sequence: list[Any] = []
    sequence.append(sequence)
    raw = {"sequence": sequence}

    with pytest.raises(AdCPValidationError, match="acyclic"):
        validate_application_context(raw)

    assert serialize_application_context(raw) == {"sequence": [None]}
    # Emitted rather than dropped: the cycle-broken form is valid JSON, and the
    # response path has no reason to discard the keys that survived.
    assert dump_adcp_response(raw) == {"sequence": [None]}
    error = AdCPValidationError("invalid request", field="context", context=raw)
    envelope = build_two_layer_error_envelope(error)
    # The envelope echoes the cycle-broken context rather than omitting it: the
    # point of this path is that formatting an error cannot hang or raise, and a
    # context that survives as emittable JSON is still the buyer's context.
    assert envelope["context"] == {"sequence": [None]}
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


def test_application_context_accepts_deep_schema_valid_opaque_data() -> None:
    validate_application_context(_nested_context(5000))


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


# ---------------------------------------------------------------------------
# JSON-safety: the boundary must never hand an encoder a value it cannot emit.
#
# Every caller of this module runs inside an exception handler or a response
# builder, so a serialization failure here does not surface as itself — it
# SHADOWS the buyer's real error and the boundary emits no envelope at all.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_float_from_a_buyer_body_serializes_to_null(literal: str) -> None:
    """CPython's ``json.loads`` accepts these; RFC 8259 does not define them.

    A buyer can therefore put a real ``float('nan')`` into ``context`` on any
    transport. Echoing it verbatim is impossible — ``json.dumps`` and
    ``JSONResponse`` both raise on it — so it is echoed as JSON ``null``, which
    keeps the key present and the envelope emittable.
    """
    context = json.loads(f'{{"x": {literal}}}')
    assert not math.isfinite(context["x"]), "fixture must produce a real non-finite float"

    serialized = serialize_application_context(context)

    assert serialized == {"x": None}
    assert json.dumps(serialized) == '{"x": null}'


def test_non_finite_float_nested_in_containers_serializes_to_null() -> None:
    context = json.loads('{"a": [{"b": NaN}], "c": {"d": [Infinity]}}')

    assert serialize_application_context(context) == {"a": [{"b": None}], "c": {"d": [None]}}


def test_serialized_context_is_encodable_by_a_strict_json_encoder() -> None:
    """``allow_nan=False`` is what a spec-conformant peer's parser enforces."""
    serialized = serialize_application_context(json.loads('{"x": NaN, "y": [Infinity]}'))

    assert json.dumps(serialized, allow_nan=False) == '{"x": null, "y": [null]}'


def test_self_referential_context_terminates_and_breaks_the_cycle() -> None:
    """JSON cannot express a cycle, so a faithful copy of one is still unemittable."""
    context: dict[str, Any] = {"k": 1}
    context["self"] = context

    serialized = serialize_application_context(context)

    assert serialized == {"k": 1, "self": None}
    assert json.dumps(serialized) == '{"k": 1, "self": null}'


def test_a_repeated_reference_that_is_not_a_cycle_is_copied_out_in_full() -> None:
    """The cycle guard tracks the path to the node, not every node ever seen.

    Two keys pointing at one dict is a DAG, not a cycle: JSON expresses it fine
    by writing the subtree twice. A guard keyed on "have I seen this object?"
    would null the second one and silently drop buyer data.
    """
    shared = {"s": 1}

    assert serialize_application_context({"a": shared, "b": shared}) == {"a": {"s": 1}, "b": {"s": 1}}


def test_typed_context_extras_are_json_coerced_like_declared_fields() -> None:
    """``model_extra`` bypasses Pydantic's ``mode="json"`` — it is merged raw.

    ``ContextObject`` declares no fields, so EVERY buyer key is an extra and
    none of them see Pydantic's JSON coercion. A non-JSON object arriving that
    way has to be coerced here or it reaches the encoder intact.
    """
    context = ContextObject.model_validate({"when": datetime.datetime(2026, 8, 11, 12, 0, 0)})

    serialized = serialize_application_context(context)

    assert serialized == {"when": "2026-08-11T12:00:00"}
    assert json.dumps(serialized) == '{"when": "2026-08-11T12:00:00"}'


def test_mcp_wire_text_parses_under_a_strict_json_parser() -> None:
    """``AdCPToolError.__str__`` is ``json.dumps(envelope)`` with ``allow_nan=True``.

    So an uncoerced non-finite float does not raise on MCP — it produces wire
    text ending ``"context": {"x": NaN}``, which a spec-conformant peer's strict
    parser rejects. The envelope is emitted and still unreadable.
    """
    from src.core.exceptions import AdCPAuthenticationError, build_two_layer_error_envelope
    from src.core.tool_error_logging import AdCPToolError

    exc = AdCPAuthenticationError("nope", context=json.loads('{"x": NaN}'))
    # Exactly what the MCP boundary raises (tool_error_logging.py:322).
    tool_error = AdCPToolError(build_two_layer_error_envelope(exc), status_code=exc.status_code)

    def _reject(constant: str) -> None:
        raise AssertionError(f"non-JSON constant {constant!r} reached the MCP wire text")

    # parse_constant fires only for NaN / Infinity / -Infinity.
    parsed = json.loads(str(tool_error), parse_constant=_reject)

    assert parsed["context"] == {"x": None}


def test_two_layer_envelope_is_encodable_by_a_strict_json_encoder() -> None:
    """The shared envelope builder feeds REST's ``JSONResponse`` and A2A's DataPart."""
    from src.core.exceptions import AdCPAuthenticationError, build_two_layer_error_envelope

    exc = AdCPAuthenticationError("nope", context=json.loads('{"a": [{"b": Infinity}]}'))
    envelope = build_two_layer_error_envelope(exc)

    assert envelope["context"] == {"a": [{"b": None}]}
    assert json.dumps(envelope, allow_nan=False)
