"""Unexpected-type behavior of the ``schema_helpers`` wire-object coercions.

``_coerce_wire_object`` backs five ``to_*`` helpers. Four of them degrade a
non-dict, non-model value to ``None``; ``to_account_reference`` alone rejects it
as a typed ``AdCPValidationError``.

The asymmetry is deliberate. The A2A skills read ``account`` straight off raw
``parameters`` with no model in front of them, so a silently-dropped account
skips identity enrichment, leaves ``identity.sandbox`` ``False``, and dispatches
a sandbox request to the LIVE adapter — a quiet failure on the axis account
isolation exists to defend. ``context``, by contrast, is opaque correlation data
the pinned schema says is "never parsed by AdCP agents", so hard-failing a
non-dict ``context`` would contradict the spec; the same reasoning covers the
other three, whose callers all sit behind a typed request model.
"""

import pytest
from adcp.types import (
    AccountReference,
    ContextObject,
    PropertyListReference,
    PushNotificationConfig,
    ReportingWebhook,
)

from src.core.exceptions import AdCPValidationError
from src.core.schema_helpers import (
    to_account_reference,
    to_context_object,
    to_property_list_reference,
    to_push_notification_config,
    to_reporting_webhook,
)

# Values that are neither ``None``, nor a dict, nor the target model.
_UNEXPECTED_TYPES = ["acc_123", 123, ["acc_123"], 4.5, True, ("acc_123",)]

# The four converters that must keep degrading to ``None``.
_DEGRADING_CONVERTERS = [
    (to_context_object, ContextObject),
    (to_reporting_webhook, ReportingWebhook),
    (to_push_notification_config, PushNotificationConfig),
    (to_property_list_reference, PropertyListReference),
]


@pytest.mark.parametrize("value", _UNEXPECTED_TYPES)
def test_to_account_reference_rejects_unexpected_type(value: object) -> None:
    """A non-dict account raises instead of coercing to ``None`` (fail-loud)."""
    with pytest.raises(AdCPValidationError) as excinfo:
        to_account_reference(value)  # type: ignore[arg-type]
    assert excinfo.value.suggestion, "typed rejection must carry a top-level suggestion"


def test_to_account_reference_rejection_matches_malformed_dict_shape() -> None:
    """Both ways of malforming an account produce the same buyer-facing contract.

    The unexpected-type rejection routes through the same
    ``adcp_validation_boundary`` as a malformed dict, so a buyer sees one
    consistent error shape for "bad account" regardless of which way it was bad:
    the same code, recovery, ``field`` and ``suggestion``, and the same
    ``Invalid account value:`` message prefix. The message *after* that prefix
    still carries the differing pydantic detail — that is not claimed identical.
    """
    with pytest.raises(AdCPValidationError) as from_bad_type:
        to_account_reference("acc_123")  # type: ignore[arg-type]
    with pytest.raises(AdCPValidationError) as from_bad_dict:
        to_account_reference({})

    assert from_bad_type.value.error_code == from_bad_dict.value.error_code
    assert from_bad_type.value.recovery == from_bad_dict.value.recovery
    assert from_bad_type.value.field == from_bad_dict.value.field == "account"
    assert from_bad_type.value.suggestion == from_bad_dict.value.suggestion
    assert str(from_bad_type.value).startswith("Invalid account value:")
    assert str(from_bad_dict.value).startswith("Invalid account value:")


@pytest.mark.parametrize("value", [*_UNEXPECTED_TYPES, {}, {"wrong_key": 1}])
def test_to_account_reference_rejection_names_the_request_field_not_the_model(value: object) -> None:
    """The buyer-visible field/suggestion name ``account``, never the pydantic model.

    ``AccountReference`` is a generated union whose members pydantic reports as
    ``AccountReference1``/``AccountReference2``. Neither name appears in any buyer
    request, so leaking one into ``field`` (a JSONPath-lite path into the REQUEST)
    or into ``suggestion`` tells the buyer to correct a field they never sent.
    """
    with pytest.raises(AdCPValidationError) as excinfo:
        to_account_reference(value)  # type: ignore[arg-type]

    assert excinfo.value.field == "account"
    assert "AccountReference" not in str(excinfo.value.field)
    assert "AccountReference" not in str(excinfo.value.suggestion)


def test_to_account_reference_still_accepts_valid_inputs() -> None:
    """Narrowing the unexpected-type case leaves the supported inputs untouched."""
    typed = to_account_reference({"account_id": "acc_123"})
    assert isinstance(typed, AccountReference)
    assert to_account_reference(typed) is typed
    assert to_account_reference(None) is None


@pytest.mark.parametrize(("converter", "model_cls"), _DEGRADING_CONVERTERS)
@pytest.mark.parametrize("value", _UNEXPECTED_TYPES)
def test_other_converters_still_degrade_to_none(converter, model_cls: type, value: object) -> None:
    """The four non-account converters keep their long-standing ``None`` fallback.

    This is the regression the account narrowing was scoped to avoid: the
    ``strict`` flag is opt-in, so flipping the shared fallback would show up here.
    """
    assert converter(value) is None


@pytest.mark.parametrize(
    ("converter", "model_cls"),
    # ``ContextObject`` is excluded: its schema sets ``extra="allow"`` because
    # context is opaque correlation data, so no dict is malformed for it. That
    # permissiveness is pinned separately below.
    [pair for pair in _DEGRADING_CONVERTERS if pair[1] is not ContextObject],
)
def test_other_converters_still_reject_malformed_dicts(converter, model_cls: type) -> None:
    """Degrading on a non-dict does not mean degrading on a malformed dict."""
    with pytest.raises(AdCPValidationError):
        converter({"definitely_not_a_field": object()})


def test_to_context_object_accepts_arbitrary_keys_by_design() -> None:
    """Context is opaque correlation data — arbitrary keys are valid, not malformed.

    This is why ``to_context_object`` must NOT adopt the account converter's
    strict rejection: the pinned schema describes context as never parsed by
    AdCP agents, merely preserved and returned.
    """
    result = to_context_object({"buyer_trace_id": "t-1", "anything": {"nested": True}})
    assert isinstance(result, ContextObject)
    assert result.model_dump()["anything"] == {"nested": True}
