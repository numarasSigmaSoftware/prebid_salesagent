"""Creative-sync seam and durable-notification orchestration contracts."""

from inspect import signature
from unittest.mock import MagicMock

from src.core.resolved_identity import ResolvedIdentity
from src.core.tools.creatives._sync import (
    _sync_creatives_core_kwargs,
    _sync_creatives_impl,
    _sync_creatives_work,
)
from src.services.idempotency_replay import ReservationResult
from tests.factories import PrincipalFactory

_CORE_KEYS = {
    "creatives",
    "assignments",
    "creative_ids",
    "delete_missing",
    "dry_run",
    "validation_mode",
    "push_notification_config",
    "context",
    "idempotency_key",
    "identity",
}


def _identity() -> ResolvedIdentity:
    return PrincipalFactory.make_identity(
        principal_id="creative-principal",
        tenant_id="creative-tenant",
        tenant={
            "tenant_id": "creative-tenant",
            "approval_mode": "require-human",
            "slack_webhook_url": "https://hooks.slack.test/services/example",
        },
    )


def test_sync_creatives_core_mapping_matches_both_call_seams() -> None:
    """The shared mapping cannot silently omit or invent an implementation argument."""
    values = _sync_creatives_core_kwargs([], None, None, False, False, "strict", None, None, None, None)

    assert set(values) == _CORE_KEYS
    assert set(signature(_sync_creatives_impl).parameters) == _CORE_KEYS | {"raw_wire_payload"}
    assert set(signature(_sync_creatives_work).parameters) == _CORE_KEYS | {
        "deferred_notifications",
        "deferred_observability",
    }


def test_notification_failure_after_durable_completion_preserves_success(mocker) -> None:
    """A failed Slack side effect must follow completion and never replace the result."""
    events: list[str] = []
    response = MagicMock(name="sync-success")
    notification = {
        "creatives_needing_approval": [{"creative_id": "creative-1"}],
        "approval_mode": "require-human",
    }
    mocker.patch(
        "src.core.tools.creatives._sync.reserve_idempotent",
        return_value=ReservationResult(attempt_id="attempt-1"),
    )

    def execute_work(**kwargs):
        kwargs["deferred_notifications"].append(notification)
        return response

    def fail_notification(**kwargs):
        events.append("notify")
        raise RuntimeError("Slack unavailable")

    mocker.patch("src.core.tools.creatives._sync._sync_creatives_work", side_effect=execute_work)
    mocker.patch(
        "src.core.tools.creatives._sync.complete_idempotent",
        side_effect=lambda *args, **kwargs: events.append("complete"),
    )
    mocker.patch(
        "src.core.tools.creatives._sync._send_creative_notifications",
        side_effect=fail_notification,
    )
    uow = MagicMock()
    uow.__enter__.return_value = uow
    mocker.patch("src.core.tools.creatives._sync.IdempotencyUoW", return_value=uow)
    mocker.patch("src.core.tools.creatives._sync.CreativeUoW", return_value=uow)

    result = _sync_creatives_impl(
        creatives=[],
        idempotency_key="creative-idempotency-key-1",
        identity=_identity(),
    )

    assert result is response
    assert events == ["complete", "notify"]
