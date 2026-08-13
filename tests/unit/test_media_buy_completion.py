"""Shared media-buy completion/rejection webhook helper (src/services/media_buy_completion.py).

Extracted from the operations approve/reject routes so the workflow and
creative-unblock routes can emit the same completion artifact (async buyers
otherwise never get the final revision/confirmed_at).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from adcp.types import GeneratedTaskStatus as AdcpTaskStatus

from src.core.schemas import CreateMediaBuyError, CreateMediaBuySuccess
from src.services import media_buy_completion as mod
from src.services.protocol_webhook_service import _to_wire_dict

_METADATA = {"task_type": "create_media_buy", "tenant_id": "t1", "principal_id": "p1", "media_buy_id": "mb_1"}


def _media_buy(*, media_buy_id="mb_1", confirmed_at=None, revision=2):
    mb = MagicMock()
    mb.media_buy_id = media_buy_id
    mb.confirmed_at = confirmed_at
    mb.revision = revision
    return mb


def _pkg(package_id):
    p = MagicMock()
    p.package_id = package_id
    return p


def test_rejectable_media_buy_statuses_membership():
    """Pin the reject-entry gate's exact membership.

    Both admin reject routes gate on this set, so a silent addition/removal/rename here
    changes which buys a reject may claim. ``pending_creatives`` is a wire status from
    the AdCP MediaBuyStatus enum; ``pending_approval`` is deliberately internal-only —
    it is NOT in that enum (a buy awaiting seller approval is not yet reportable to the
    buyer), so the set is asserted literally rather than derived from the enum.
    """
    from adcp.types import MediaBuyStatus

    assert set(mod.REJECTABLE_MEDIA_BUY_STATUSES) == {"pending_approval", "pending_creatives"}
    wire_statuses = {status.value for status in MediaBuyStatus}
    assert "pending_creatives" in wire_statuses
    assert "pending_approval" not in wire_statuses


def test_build_media_buy_result_completion():
    result = mod.build_media_buy_result(_media_buy(revision=3), [_pkg("p1"), _pkg("p2")])
    assert isinstance(result, CreateMediaBuySuccess)
    assert result.media_buy_id == "mb_1"
    assert result.revision == 3
    assert [p.package_id for p in result.packages] == ["p1", "p2"]


def test_build_media_buy_result_rejection_is_error_with_policy_violation():
    """AdCP 3.1.1 create-media-buy has no rejection arm, so a reject builds a
    CreateMediaBuyError carrying POLICY_VIOLATION (via the AdCPMediaBuyRejectedError
    cascade) with the reason in the error message — not a success + rejection_reason."""
    result = mod.build_media_buy_result(_media_buy(), [_pkg("p1")], rejection_reason="policy violation")
    assert isinstance(result, CreateMediaBuyError)
    assert result.errors[0].code == "POLICY_VIOLATION"
    assert "policy violation" in result.errors[0].message
    # Structured recovery guidance rides the artifact: recovery from the typed
    # rejection, suggestion = the pinned 3.1.1 enumMetadata hint for POLICY_VIOLATION.
    assert result.errors[0].recovery == "correctable"
    assert result.errors[0].suggestion == "review policy requirements in the error details"


@pytest.mark.parametrize("protocol", ["mcp", "a2a"])
def test_emit_media_buy_webhook_sends_payload_carrying_reason(protocol):
    """emit_media_buy_webhook sends exactly one notification whose serialized wire
    carries the rejection reason (in the CreateMediaBuyError message), for MCP and A2A."""
    result = mod.build_media_buy_result(_media_buy(), [_pkg("p1")], rejection_reason="brand-safety-xyz")
    step_data = {
        "step_id": "step_1",
        "context_id": "ctx_1",
        "tool_name": "create_media_buy",
        "request_data": {"protocol": protocol},
    }
    service = MagicMock()
    service.send_notification = AsyncMock()
    with patch.object(mod, "get_protocol_webhook_service", return_value=service):
        mod.emit_media_buy_webhook(step_data, MagicMock(), result, AdcpTaskStatus.rejected, _METADATA)

    assert service.send_notification.call_count == 1
    payload = service.send_notification.call_args.kwargs["payload"]
    # Assert on the TYPED error message field at its exact wire location per
    # protocol, not a blanket json.dumps substring (which would pass on the
    # reason appearing anywhere in the payload). MCP nests the result directly;
    # A2A wraps it in a failed-Task artifact DataPart.
    wire = _to_wire_dict(payload)
    if protocol == "a2a":
        errors = wire["artifacts"][0]["parts"][0]["data"]["errors"]
    else:
        errors = wire["result"]["errors"]
    assert errors[0]["message"] == "Rejected: brand-safety-xyz"


def test_emit_media_buy_webhook_swallows_delivery_errors():
    """A webhook delivery failure must be logged, never raised — the DB transition
    already committed and must not be undone by a delivery error."""
    result = mod.build_media_buy_result(_media_buy(), [_pkg("p1")])
    step_data = {"step_id": "s", "context_id": "c", "tool_name": "create_media_buy", "request_data": {}}
    service = MagicMock()
    service.send_notification = AsyncMock(side_effect=RuntimeError("network down"))
    with patch.object(mod, "get_protocol_webhook_service", return_value=service):
        mod.emit_media_buy_webhook(
            step_data, MagicMock(), result, AdcpTaskStatus.completed, _METADATA
        )  # must not raise


@pytest.mark.parametrize("delivered", [False, True])
async def test_emit_protocol_result_webhook_passes_through_the_delivery_verdict(delivered):
    """The emitter's ``-> bool`` is the service's own delivery verdict, passed through.

    ``send_notification`` can return False WITHOUT raising, so the swallow-errors
    sibling above — which drives the *raising* arm — never reaches this path. The
    ``await_count`` assert pins that a returned False came from the pass-through and
    not from the ``except`` arm's blanket ``return False``.
    """
    result = mod.build_media_buy_result(_media_buy(), [_pkg("p1")])
    step_data = {"step_id": "s", "context_id": "c", "tool_name": "create_media_buy", "request_data": {}}
    service = MagicMock()
    service.send_notification = AsyncMock(return_value=delivered)
    with patch.object(mod, "get_protocol_webhook_service", return_value=service):
        sent = await mod.emit_protocol_result_webhook_async(
            step_data, MagicMock(), result, AdcpTaskStatus.completed, _METADATA
        )

    assert service.send_notification.await_count == 1
    assert sent is delivered


_URL = "https://buyer.example/webhook"


def _step_data(*, url=_URL, protocol="mcp"):
    request_data = {"protocol": protocol}
    if url is not None:
        request_data["push_notification_config"] = {"url": url}
    return {"step_id": "s", "context_id": "c", "tool_name": "create_media_buy", "request_data": request_data}


def _config(url=_URL):
    from datetime import UTC, datetime

    c = MagicMock()
    c.url = url
    c.created_at = datetime.now(UTC)
    return c


def _session_returning(configs):
    """A mock session whose repository query yields ``configs`` (via .scalars().all())."""
    session = MagicMock()
    session.scalars.return_value.all.return_value = configs
    return session


def test_emit_media_buy_completion_emits_when_config_found():
    """When the step has a push URL and a matching active config exists, it emits once."""
    mb = _media_buy()
    mb.principal_id = "p1"
    with patch.object(mod, "emit_media_buy_webhook") as mock_emit:
        mod.emit_media_buy_completion(
            _session_returning([_config()]), "t1", mb, [_pkg("p1")], _step_data(), AdcpTaskStatus.completed
        )
    assert mock_emit.call_count == 1


@pytest.mark.parametrize(
    "media_buy, step_data, configs",
    [
        (None, _step_data(), [_config()]),  # no buy → no-op
        (_media_buy(), _step_data(url=None), [_config()]),  # no push url on step → no-op
        (_media_buy(), _step_data(), []),  # no active config for principal → no-op
        (_media_buy(), _step_data(), [_config(url="https://other.example/hook")]),  # url mismatch → no-op
    ],
)
def test_emit_media_buy_completion_is_noop_without_matching_config(media_buy, step_data, configs):
    with patch.object(mod, "emit_media_buy_webhook") as mock_emit:
        mod.emit_media_buy_completion(
            _session_returning(configs), "t1", media_buy, [_pkg("p1")], step_data, AdcpTaskStatus.completed
        )
    mock_emit.assert_not_called()
