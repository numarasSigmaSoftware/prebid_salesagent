"""A2A error routing: application failures ride in failed Tasks, not JSON-RPC.

Compliance finding F7. Per AdCP 3.1.x transport rules (spec prose:
building/operating/transport-errors.mdx "Layer Separation" and the two-layer
error-handling model), application/task-execution failures must be RETURNED in
the task response body as a failed Task carrying the two-layer AdCP error
envelope artifact. JSON-RPC errors (``A2AError``) are reserved for genuine
transport faults — malformed requests, missing auth, and unknown JSON-RPC
*methods*. Unknown or unimplemented *skills* are application-layer failures
(the ``message/send`` method is valid; routing failed inside skill dispatch),
so they return a failed Task with ``UNSUPPORTED_FEATURE`` — see the dispatch-
registry wire assertions in ``test_a2a_transport_contract.py``.

Pre-fix bug: ``on_message_send``'s outer exception handler built the correct
failed Task with the ``processing_error`` envelope artifact, then threw it
away by raising ``InternalError`` (a JSON-RPC error) instead of returning the
Task. These tests pin the returned-failed-Task contract on the wire artifact.
"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from a2a.server.request_handlers.response_helpers import build_error_response
from a2a.types import (
    Artifact,
    CancelTaskRequest,
    GetTaskRequest,
    InternalError,
    InvalidParamsError,
    InvalidRequestError,
    Part,
    SendMessageConfiguration,
    SendMessageRequest,
    Task,
    TaskPushNotificationConfig,
    TaskState,
    TaskStatus,
)

from src.a2a_server.adcp_a2a_server import AdCPRequestHandler, _dict_to_value
from src.core.exceptions import (
    AUTH_REQUIRED_CANONICAL_SUGGESTION,
    VALIDATION_ERROR_SUGGESTION,
    AdCPAuthenticationError,
    AdCPAuthInvalidError,
    AdCPError,
    AdCPValidationError,
)
from src.core.tool_context import ToolContext
from tests.a2a_helpers import make_a2a_context
from tests.factories import PrincipalFactory
from tests.helpers import assert_envelope_shape
from tests.helpers.pinned_schema import pinned_error_code_suggestion
from tests.helpers.secret_scrub import SECRET_BEARING_MESSAGE, assert_no_secret_leak, serialize_wire_error
from tests.utils.a2a_helpers import (
    assert_failed_task_envelope,
    assert_failed_task_no_secret_leak,
    create_a2a_message_with_skill,
    extract_processing_error_envelope,
    make_nl_send_message_request,
    make_test_a2a_identity,
)

_TEST_IDENTITY = make_test_a2a_identity()


def _capture_a2a_auth_records():
    """Return a semantic recorder spy for pre-identity A2A auth failures."""
    records = []

    def _record(transport, operation, error, *, tenant_id=None, principal_id=None):
        records.append((transport, operation, error.error_code, tenant_id, principal_id))

    return records, _record


def _make_handler() -> tuple[AdCPRequestHandler, object]:
    """Handler + authenticated call context for driving on_message_send."""
    handler = AdCPRequestHandler()
    handler._get_auth_token = MagicMock(return_value="test-token")
    ctx = make_a2a_context(auth_token="test-token", headers={"host": "test.example.com"})
    return handler, ctx


@pytest.mark.asyncio
async def test_message_send_rejects_private_push_notification_url_before_storage():
    """A message-scoped callback cannot target seller-internal services."""
    handler, ctx = _make_handler()
    params = SendMessageRequest(
        message=create_a2a_message_with_skill("get_products", {"brief": "video"}),
        configuration=SendMessageConfiguration(
            task_push_notification_config=TaskPushNotificationConfig(
                id="pnc_private",
                url="http://169.254.169.254/latest/meta-data",
            )
        ),
    )

    with pytest.raises(InvalidParamsError, match="public HTTP"):
        await handler.on_message_send(params, context=ctx)

    assert handler._task_push_configs == {}
    assert handler.tasks == {}


@pytest.mark.asyncio
async def test_standalone_push_config_rejects_private_url_before_repository_write():
    """The durable A2A config endpoint rejects unsafe URLs before opening its UoW."""
    handler, ctx = _make_handler()
    handler._authenticated_tool_context = MagicMock(return_value=SimpleNamespace(tenant_id="tenant_1"))

    with (
        patch("src.a2a_server.adcp_a2a_server.PushNotificationConfigUoW") as uow,
        pytest.raises(InvalidParamsError, match="public HTTP"),
    ):
        await handler.on_create_task_push_notification_config(
            TaskPushNotificationConfig(
                id="pnc_private",
                url="http://10.0.0.5/internal",
            ),
            context=ctx,
        )

    uow.assert_not_called()


def _boundary_recording_spy():
    """Capture the stable boundary-record fields without asserting mock internals."""
    records: list[tuple[str, str, str, str | None, str]] = []

    def record(transport, operation, error, *, tenant_id, principal_id):
        records.append((transport, operation, error.wire_error_code, tenant_id, principal_id))

    return records, record


@pytest.mark.asyncio
async def test_untyped_crash_raises_sanitized_internal_error_without_leaking_secrets():
    """An untyped internal crash → sanitized JSON-RPC InternalError; the raw exception
    text (which may carry credentials/SQL/hostnames) never reaches the client.

    Per transport-errors.mdx an internal crash is a TRANSPORT-layer error, and the
    error-security requirements forbid exposing internals. So untyped exceptions are
    logged server-side and surfaced as a generic InternalError — only TYPED
    ``AdCPError`` (controlled messages) become failed Tasks. Uses a secret-shaped
    message to pin that it is not echoed back.
    """
    handler, ctx = _make_handler()
    params = make_nl_send_message_request("Show me available products in the catalog")
    secret = SECRET_BEARING_MESSAGE

    with patch("src.core.resolved_identity.resolve_identity", return_value=_TEST_IDENTITY):
        with patch("src.a2a_server.adcp_a2a_server.core_get_products_tool", side_effect=RuntimeError(secret)):
            with pytest.raises(InternalError) as exc_info:
                await handler.on_message_send(params, context=ctx)

    err = exc_info.value
    client_facing = f"{err.message} {json.dumps(err.data)}"
    assert_no_secret_leak(client_facing)
    # Still a structured error with a safe, generic wire code + message.
    assert err.data["adcp_error"]["code"] == "SERVICE_UNAVAILABLE"
    assert "internal error" in err.message.lower()


@pytest.mark.asyncio
async def test_untyped_crash_leaves_no_orphan_working_task():
    """An untyped crash routes to a sanitized InternalError AND leaves no retrievable task.

    Regression: the provisional Task is stored WORKING before dispatch; the untyped-error
    branch used to raise the sanitized InternalError without finalizing or removing it, so
    ``tasks/get`` still returned a task stuck in WORKING. The crash is a TRANSPORT-layer
    error (a JSON-RPC InternalError), not a Task-layer outcome, so the branch now drops the
    provisional task + push config before raising — nothing should remain retrievable.
    """
    handler, ctx = _make_handler()
    params = make_nl_send_message_request("Show me available products in the catalog")

    with patch("src.core.resolved_identity.resolve_identity", return_value=_TEST_IDENTITY):
        with patch("src.a2a_server.adcp_a2a_server.core_get_products_tool", side_effect=RuntimeError("boom")):
            with pytest.raises(InternalError):
                await handler.on_message_send(params, context=ctx)

    # The provisional WORKING task (and any push config) must be gone — no orphan.
    assert handler.tasks == {}, f"orphan task(s) left in WORKING after untyped crash: {list(handler.tasks)}"
    assert handler._task_push_configs == {}, "orphan push config left after untyped crash"


@pytest.mark.asyncio
async def test_explicit_skill_untyped_crash_scrubs_secret_from_failed_task():
    """An untyped crash inside an EXPLICIT-skill invocation returns a failed Task whose
    artifact is scrubbed of the raw exception — in BOTH the DataPart envelope and the
    human-readable TextPart.

    Regression: the explicit-skill path routed ``str(exc)`` onto the wire (via
    ``normalize_to_adcp_error`` → ``AdCPError(str(exc))``) while the outer/NL path was
    already sanitized. Both paths now share the single ``safe_adcp_error`` policy, so an
    untyped crash becomes a generic internal error regardless of which path caught it.
    Reproduces the failure by patching ``_handle_explicit_skill`` to raise a
    secret-shaped exception and inspect the returned failed Task.
    """
    handler, ctx = _make_handler()
    params = SendMessageRequest(message=create_a2a_message_with_skill("get_products", {"brief": "video"}))
    secret = SECRET_BEARING_MESSAGE

    with patch("src.core.resolved_identity.resolve_identity", return_value=_TEST_IDENTITY):
        with patch.object(handler, "_handle_explicit_skill", new_callable=AsyncMock, side_effect=RuntimeError(secret)):
            result = await handler.on_message_send(params, context=ctx)

    # The shared strict oracle pins the whole wire contract at once: FAILED state, artifact name,
    # exactly one DataPart + one TextPart, and BOTH envelope layers agreeing on code + recovery.
    envelope = assert_failed_task_envelope(result, code="SERVICE_UNAVAILABLE", recovery="transient")
    # No secret on ANY client-facing carrier of the failed Task (the surface definition lives once).
    assert_failed_task_no_secret_leak(result)
    # Sanitized to a generic internal error, not a str(exc)-derived message.
    assert "internal error" in envelope["errors"][0]["message"].lower()


@pytest.mark.asyncio
async def test_failed_explicit_skill_task_is_pollable_via_tasks_get():
    """The buyer must be able to poll tasks/get on a synchronously-returned FAILED explicit-skill
    Task — a failed Task is a Task-layer outcome, not a transport error, and the NL-failed path is
    already pollable. Regression: the failed-batch branch returned without `_remember_task`, so the
    task was an ownerless orphan and `on_get_task` served None to its own owner.

    Drives the REAL `on_get_task` (not the private owner map). No durable step exists in this
    in-memory context (no DB), so the durable lookup is stubbed to None and the assertion rests on
    the in-memory owned path — which is exactly what the fix populates. Before the fix this returns
    None (mutation-verified)."""
    handler, ctx = _make_handler()
    params = SendMessageRequest(message=create_a2a_message_with_skill("get_products", {"brief": "video"}))

    async def _boom(params, identity):
        raise ValueError("boom")

    with patch("src.core.resolved_identity.resolve_identity", return_value=_TEST_IDENTITY):
        with patch.object(handler, "_handle_get_products_skill", new_callable=AsyncMock, side_effect=_boom):
            result = await handler.on_message_send(params, context=ctx)

    assert isinstance(result, Task) and result.status.state == TaskState.TASK_STATE_FAILED

    # The owner polls tasks/get. Resolve to the same identity that created the task; no durable
    # step in a unit context, so on_get_task must serve the failed Task from the owned in-memory
    # path — which requires the failed branch to have remembered it under its owner.
    handler._resolve_a2a_identity = MagicMock(return_value=_TEST_IDENTITY)
    with patch.object(handler, "_durable_task_from_step", return_value=None):
        polled = await handler.on_get_task(GetTaskRequest(id=result.id), context=ctx)

    assert polled is not None, "owner cannot poll their failed explicit-skill task (ownerless orphan)"
    assert polled.id == result.id
    assert polled.status.state == TaskState.TASK_STATE_FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc_factory", "expected_code", "msg_keyword", "expected_suggestion"),
    [
        pytest.param(
            lambda s: ValueError(s), "VALIDATION_ERROR", "validate", VALIDATION_ERROR_SUGGESTION, id="ValueError"
        ),
        pytest.param(
            lambda s: PermissionError(s),
            "AUTH_REQUIRED",
            "credential",
            AUTH_REQUIRED_CANONICAL_SUGGESTION,
            id="PermissionError",
        ),
    ],
)
async def test_explicit_skill_raw_builtin_scrubs_secret_but_keeps_semantic_code(
    exc_factory, expected_code, msg_keyword, expected_suggestion
):
    """A raw ``ValueError``/``PermissionError`` raised INSIDE a skill returns a failed Task whose
    envelope keeps the SEMANTIC code the synchronous boundaries emit (VALIDATION_ERROR /
    AUTH_REQUIRED) but is scrubbed of the raw ``str(e)``.

    This patches the SKILL (``_handle_get_products_skill``), NOT ``_handle_explicit_skill``, so the
    exception flows through the real normalization seam at ``_handle_explicit_skill`` — the seam the
    prior secret test bypassed by patching ``_handle_explicit_skill`` itself. Before the
    provenance-vs-semantics split, that seam normalized the built-in to a *trusted*
    ``AdCPValidationError``/``AdCPAuthorizationError`` and its raw message survived to the wire.
    """
    handler, ctx = _make_handler()
    params = SendMessageRequest(message=create_a2a_message_with_skill("get_products", {"brief": "video"}))
    secret = SECRET_BEARING_MESSAGE

    with patch("src.core.resolved_identity.resolve_identity", return_value=_TEST_IDENTITY):
        with patch.object(
            handler, "_handle_get_products_skill", new_callable=AsyncMock, side_effect=exc_factory(secret)
        ):
            result = await handler.on_message_send(params, context=ctx)

    # SEMANTIC code preserved (matches the synchronous boundary) and both envelope layers agree;
    # a raw built-in normalizes to a CLIENT-CORRECTABLE code, never a transient/terminal one.
    envelope = assert_failed_task_envelope(result, code=expected_code, recovery="correctable")
    assert_failed_task_no_secret_leak(result)
    # Message scrubbed AND category-appropriate — a VALIDATION_ERROR / AUTH_REQUIRED must not
    # read "internal error".
    message = envelope["errors"][0]["message"].lower()
    assert msg_keyword in message
    assert "internal error" not in message
    # The suggestion reaches the WIRE, not just the error object. Chain of custody: this pins
    # wire == module constant, and test_sanitized_suggestions_match_pinned_spec_enum pins
    # module constant == the pinned spec fixture's enumMetadata.
    assert envelope["errors"][0]["suggestion"] == expected_suggestion


@pytest.mark.asyncio
async def test_explicit_skill_raw_error_scrubs_activity_and_audit_sinks():
    """A2A preserves raw provenance until tenant-visible observability is scrubbed."""
    handler = AdCPRequestHandler()
    feed_records: list[dict[str, object]] = []
    audit_records: list[dict[str, object]] = []
    audit_logger = SimpleNamespace(log_operation=lambda **kwargs: audit_records.append(kwargs))

    with (
        patch.object(
            handler,
            "_handle_get_products_skill",
            new_callable=AsyncMock,
            side_effect=ValueError(SECRET_BEARING_MESSAGE),
        ),
        patch(
            "src.services.activity_feed.activity_feed.log_error",
            side_effect=lambda **kwargs: feed_records.append(kwargs),
        ),
        patch("src.core.audit_logger.get_audit_logger", return_value=audit_logger),
        pytest.raises(AdCPError) as exc_info,
    ):
        await handler._handle_explicit_skill(
            "get_products",
            {"brief": "video"},
            _TEST_IDENTITY,
        )

    assert exc_info.value.wire_error_code == "VALIDATION_ERROR"
    assert len(feed_records) == 1
    assert len(audit_records) == 1
    assert_no_secret_leak(feed_records[0], context="A2A activity error payload")
    assert_no_secret_leak(audit_records[0], context="A2A audit error payload")


@pytest.mark.asyncio
async def test_unknown_skill_records_boundary_error_exactly_once():
    """An unknown skill emits exactly one boundary-observability record.

    Regression for the observability gap: the unknown-skill check used to raise
    BEFORE the logged try, so `record_boundary_error` never fired for it while the
    outer catch assumed the inner boundary had already logged. The check now lives
    inside the logged boundary, so an unknown skill records exactly once — not zero,
    not twice.
    """
    handler, ctx = _make_handler()
    params = SendMessageRequest(message=create_a2a_message_with_skill("nonexistent_skill", {}))
    records, record = _boundary_recording_spy()

    with patch("src.core.resolved_identity.resolve_identity", return_value=_TEST_IDENTITY):
        with patch("src.a2a_server.adcp_a2a_server.record_boundary_error", side_effect=record):
            result = await handler.on_message_send(params, context=ctx)

    assert_failed_task_envelope(
        result,
        code="UNSUPPORTED_FEATURE",
        recovery="correctable",
        artifact_name="error_result",
    )
    assert records == [
        ("a2a", "unsupported_skill", "UNSUPPORTED_FEATURE", _TEST_IDENTITY.tenant_id, _TEST_IDENTITY.principal_id)
    ]


@pytest.mark.parametrize(
    ("identity", "expected_tenant_id", "expected_principal_id"),
    [
        pytest.param(None, None, None, id="no-identity"),
        pytest.param(
            PrincipalFactory.make_identity(tenant_id="t_boundary", principal_id="p_boundary", protocol="a2a"),
            "t_boundary",
            "p_boundary",
            id="resolved-identity",
        ),
        pytest.param(
            ToolContext(
                context_id="ctx_boundary",
                tenant_id="t_tool",
                principal_id="p_tool",
                tool_name="set_push_notification_config",
                request_timestamp=datetime.now(UTC),
            ),
            "t_tool",
            "p_tool",
            id="tool-context",
        ),
    ],
)
def test_boundary_internal_error_uses_one_scope_policy_regardless_of_caller(
    identity, expected_tenant_id, expected_principal_id
):
    """``_boundary_internal_error`` — the single home shared by all seven A2A
    request handlers — logs the SAME identity-scope policy for resolved identities,
    tool contexts, and unresolved callers.

    Regression: ``on_message_send``'s boundary arm used to open-code
    ``(identity.tenant_id or "unknown") if identity else "unknown"`` while
    ``on_get_task``/``on_cancel_task`` open-coded
    ``getattr(identity, "tenant_id", None)`` / ``getattr(..., "principal_id", None) or
    "anonymous"`` — the SAME unresolved-identity event logged under two different
    sentinels (``"unknown"``/``"unknown"`` vs ``None``/``"anonymous"``), un-greppable
    across handlers. All seven now delegate to this one function, so there is exactly
    one scope and sentinel definition to test.
    """
    from src.a2a_server.adcp_a2a_server import _boundary_internal_error

    exc = RuntimeError("boom")
    with patch("src.a2a_server.adcp_a2a_server.record_boundary_error") as mock_record:
        result = _boundary_internal_error("some_op", "some operation", identity, exc)

    mock_record.assert_called_once_with(
        "a2a",
        "some_op",
        exc,
        tenant_id=expected_tenant_id,
        principal_id=expected_principal_id,
    )
    assert isinstance(result, InternalError)


def test_anonymous_discovery_boundary_error_is_tenant_unscoped():
    """A header-selected tenant cannot receive anonymous discovery failures."""
    from src.a2a_server.adcp_a2a_server import _record_a2a_boundary_error

    identity = PrincipalFactory.make_identity(
        tenant_id="header-selected-tenant",
        principal_id=None,
        protocol="a2a",
    )
    exc = ValueError("anonymous get_products validation failed")

    with patch("src.a2a_server.adcp_a2a_server.record_boundary_error") as mock_record:
        _record_a2a_boundary_error("get_products", identity, exc)

    mock_record.assert_called_once_with(
        "a2a",
        "get_products",
        exc,
        tenant_id=None,
        principal_id=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "op_key", "op_label"),
    [
        pytest.param(
            "on_get_task_push_notification_config",
            "get_push_notification_config",
            "get push notification config",
            id="get",
        ),
        pytest.param(
            "on_create_task_push_notification_config",
            "create_push_notification_config",
            "set push notification config",
            id="create",
        ),
        pytest.param(
            "on_list_task_push_notification_configs",
            "list_push_notification_configs",
            "list push notification configs",
            id="list",
        ),
        pytest.param(
            "on_delete_task_push_notification_config",
            "delete_push_notification_config",
            "delete push notification config",
            id="delete",
        ),
    ],
)
async def test_push_config_handlers_share_boundary_internal_error(handler_name, op_key, op_label):
    """Every push-config handler delegates its untyped-crash arm to the same scope,
    audit, and sanitization helper as message/send and task get/cancel.
    """
    handler = AdCPRequestHandler()
    exc = RuntimeError("boom")
    handler._authenticated_tool_context = MagicMock(side_effect=exc)
    translated = InternalError(message="sanitized")

    with patch(
        "src.a2a_server.adcp_a2a_server._boundary_internal_error",
        return_value=translated,
    ) as mock_boundary:
        with pytest.raises(InternalError) as exc_info:
            await getattr(handler, handler_name)(MagicMock(), MagicMock())

    assert exc_info.value is translated
    mock_boundary.assert_called_once_with(op_key, op_label, None, exc)


@pytest.mark.asyncio
async def test_typed_adcp_error_keeps_its_own_wire_code_on_failed_task():
    """A typed AdCPError escaping to the outer handler keeps its own wire code.

    The envelope must carry the AdCPError's code (here ``VALIDATION_ERROR``),
    not a blanket ``INTERNAL_ERROR`` — ``_build_error_envelope`` passes typed
    errors through ``safe_adcp_error`` unchanged (only untyped crashes are
    replaced with a generic error).
    """
    handler, ctx = _make_handler()
    params = make_nl_send_message_request("Show me available products in the catalog")

    with patch("src.core.resolved_identity.resolve_identity", return_value=_TEST_IDENTITY):
        with patch(
            "src.a2a_server.adcp_a2a_server.core_get_products_tool",
            side_effect=AdCPValidationError("brief must not be empty", _wire_safe_message=True),
        ):
            result = await handler.on_message_send(params, context=ctx)

    assert isinstance(result, Task), f"expected a returned Task, got {type(result).__name__}"
    assert result.status.state == TaskState.TASK_STATE_FAILED, (
        f"expected TASK_STATE_FAILED, got {result.status.state!r}"
    )
    assert_envelope_shape(
        extract_processing_error_envelope(result),
        "VALIDATION_ERROR",
        recovery="correctable",
        message_substr="brief must not be empty",
    )


@pytest.mark.parametrize(
    ("raised", "expected_code"),
    [
        (ValueError(SECRET_BEARING_MESSAGE), "VALIDATION_ERROR"),
        (PermissionError(SECRET_BEARING_MESSAGE), "AUTH_REQUIRED"),
    ],
    ids=["value_error", "permission_error"],
)
@pytest.mark.asyncio
async def test_nl_client_correctable_builtin_becomes_failed_task_not_internal_error(raised, expected_code):
    """A client-correctable built-in from an NL handler is an APPLICATION-layer failure.

    AdCP 3.1.1 ``transport-errors.mdx`` §"Layer Separation" puts AdCP errors in the task
    response, not the transport error channel, and classifies a malformed request as a
    Validation Error rather than a Protocol Error. Explicit-skill dispatch already honoured
    that; NL routing did not, because its handlers were invoked outside the sanitize seam —
    so a raw ``ValueError`` surfaced as a JSON-RPC ``InternalError``. The envelope inside it
    already said ``correctable``, which told the buyer not to auto-retry while the transport
    frame told them it was a server fault.

    Ungraded by the 3.1.1 conformance storyboards (``check: error_code`` is shape-agnostic
    and accepts ``error.data``), so this test is the contract.

    The recorder assertion is on the exception TYPE, not its wire code: the seam must hand
    ``record_boundary_error`` the ORIGINAL built-in so the privileged log keeps the raw text
    and traceback, while the wire gets the scrubbed copy.
    """
    handler, ctx = _make_handler()
    params = make_nl_send_message_request("Show me available products in the catalog")

    recorded: list[tuple[str, str]] = []

    def record(transport, operation, error, *, tenant_id, principal_id):
        recorded.append((operation, type(error).__name__))

    with patch("src.core.resolved_identity.resolve_identity", return_value=_TEST_IDENTITY):
        with patch("src.a2a_server.adcp_a2a_server.core_get_products_tool", side_effect=raised):
            with patch("src.a2a_server.adcp_a2a_server.record_boundary_error", side_effect=record):
                result = await handler.on_message_send(params, context=ctx)

    assert isinstance(result, Task), f"expected a failed Task, got {type(result).__name__}"
    assert result.status.state == TaskState.TASK_STATE_FAILED

    envelope = extract_processing_error_envelope(result)
    assert_envelope_shape(envelope, expected_code, recovery="correctable")
    assert_no_secret_leak(envelope, context=f"NL {type(raised).__name__} failed-Task envelope")

    # Recorded exactly once, with the ORIGINAL exception, under its own operation label.
    assert recorded == [("get_products", type(raised).__name__)]

    # The failed Task is retained: it is a task-layer outcome, so tasks/get can serve it.
    assert handler.tasks, "a failed Task must not be dropped like a transport-layer crash"


@pytest.mark.asyncio
async def test_nl_typed_failure_records_once_under_its_own_operation():
    """A typed NL-routing failure is recorded exactly once, labelled by the operation.

    The record moved from the outer task boundary into the shared dispatch seam, so it now
    carries the specific operation (``get_products``) instead of the generic
    ``message_processing`` — matching what explicit-skill dispatch has always logged, and
    making the two paths greppable together. "Once" remains the load-bearing half: the
    outer arm is pure framing now, so a re-added record there would show up as a second
    entry.
    """
    handler, ctx = _make_handler()
    params = make_nl_send_message_request("Show me available products in the catalog")
    records, record = _boundary_recording_spy()

    with patch("src.core.resolved_identity.resolve_identity", return_value=_TEST_IDENTITY):
        with patch(
            "src.a2a_server.adcp_a2a_server.core_get_products_tool",
            side_effect=AdCPValidationError("brief must not be empty"),
        ):
            with patch("src.a2a_server.adcp_a2a_server.record_boundary_error", side_effect=record):
                result = await handler.on_message_send(params, context=ctx)

    assert_failed_task_envelope(
        result,
        code="VALIDATION_ERROR",
        recovery="correctable",
        artifact_name="processing_error",
    )
    assert records == [
        ("a2a", "get_products", "VALIDATION_ERROR", _TEST_IDENTITY.tenant_id, _TEST_IDENTITY.principal_id)
    ]


@pytest.mark.asyncio
async def test_auth_extraction_failure_raises_sanitized_internal_error_no_nameerror():
    """A crash before identity resolution raises a sanitized InternalError, no NameError.

    Pins the ``identity = None`` hoist: auth-token extraction happens before identity
    resolution, so the untyped-crash handler must read ``identity`` (None) without a
    ``NameError`` while logging. The crash is untyped → sanitized JSON-RPC
    InternalError (not a failed Task), the raw text is not leaked, and no webhook.
    """
    handler = AdCPRequestHandler()
    handler._get_auth_token = MagicMock(side_effect=RuntimeError(SECRET_BEARING_MESSAGE))
    handler._send_protocol_webhook = AsyncMock()
    ctx = make_a2a_context(headers={"host": "test.example.com"})
    params = make_nl_send_message_request("Show me available products in the catalog")

    with pytest.raises(InternalError) as exc_info:
        await handler.on_message_send(params, context=ctx)

    err = exc_info.value
    client_facing = f"{err.message} {json.dumps(err.data)}"
    assert_no_secret_leak(client_facing, context="auth-extraction crash on the JSON-RPC wire")
    assert_envelope_shape(err.data, "SERVICE_UNAVAILABLE", recovery="transient")
    handler._send_protocol_webhook.assert_not_awaited()


@pytest.mark.asyncio
async def test_multi_skill_message_rejected_before_any_side_effect():
    """A message carrying >1 skill is rejected up front — no skill runs, no side effects.

    Aggregating divergent per-skill outcomes into one Task is incoherent when a skill
    has real side effects (a submitted create_media_buy persists a workflow while a
    sibling fails). So a multi-skill batch is rejected as a typed application failure
    (UNSUPPORTED_FEATURE) BEFORE dispatch — ``_handle_explicit_skill`` is never
    called, and the immediate terminal failure sends no webhook.
    """
    handler, ctx = _make_handler()
    handler._send_protocol_webhook = AsyncMock()
    handler._handle_explicit_skill = AsyncMock()  # must NOT be called
    message = create_a2a_message_with_skill("create_media_buy", {})
    message.parts.append(create_a2a_message_with_skill("approve_creative", {}).parts[0])
    params = SendMessageRequest(message=message)

    records, record = _boundary_recording_spy()
    with patch("src.core.resolved_identity.resolve_identity", return_value=_TEST_IDENTITY):
        with patch("src.a2a_server.adcp_a2a_server.record_boundary_error", side_effect=record):
            result = await handler.on_message_send(params, context=ctx)

    assert isinstance(result, Task)
    assert result.status.state == TaskState.TASK_STATE_FAILED
    envelope = extract_processing_error_envelope(result)
    assert envelope["adcp_error"]["code"] == "UNSUPPORTED_FEATURE", envelope
    assert envelope["adcp_error"]["recovery"] == "correctable", envelope
    assert "multiple skills" in envelope["errors"][0]["message"].lower()
    handler._handle_explicit_skill.assert_not_awaited()  # zero side effects
    handler._send_protocol_webhook.assert_not_awaited()
    assert records == [
        ("a2a", "message_processing", "UNSUPPORTED_FEATURE", _TEST_IDENTITY.tenant_id, _TEST_IDENTITY.principal_id)
    ]


@pytest.mark.asyncio
async def test_immediate_completed_task_sends_no_webhook():
    """An immediately-completed task returns synchronously and sends no webhook.

    a2a-guide.mdx "Webhook Trigger Rules": no push is sent
    when the initial response is already terminal — the buyer has the result in
    the response. Only non-terminal (submitted) initial responses notify.
    """
    handler, ctx = _make_handler()
    handler._send_protocol_webhook = AsyncMock()
    # A plain completed result (no "status": "submitted") → task completes immediately.
    handler._handle_explicit_skill = AsyncMock(return_value={"products": [{"id": "p1"}]})
    message = create_a2a_message_with_skill("get_products", {})
    params = SendMessageRequest(message=message)

    with patch("src.core.resolved_identity.resolve_identity", return_value=_TEST_IDENTITY):
        result = await handler.on_message_send(params, context=ctx)

    assert result.status.state == TaskState.TASK_STATE_COMPLETED
    handler._send_protocol_webhook.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_protocol_webhook_serializes_every_artifact_including_duplicate_names():
    """``_send_protocol_webhook`` puts EVERY artifact's data on the wire — including
    duplicate-named ones — never a single stale value or a flattened error string.

    Scope note: this is a focused unit test of the webhook serializer at the status
    production actually calls it with (``submitted``); the real async *failure*
    transition is emitted elsewhere (``src/core/context_manager.py``) and is out of
    this helper's scope. Two artifacts share the name ``error_result`` (repeated
    skill in one message) plus a distinct sibling — all three must survive
    serialization, so name-based overwriting is caught.
    """
    from google.protobuf import json_format

    handler = AdCPRequestHandler()

    env_a = AdCPRequestHandler._build_error_envelope(
        AdCPValidationError("first skill exploded", _wire_safe_message=True)
    )
    env_b = AdCPRequestHandler._build_error_envelope(
        AdCPValidationError("second skill exploded", _wire_safe_message=True)
    )
    task = Task(id="task_sub", context_id="ctx_sub", status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED))
    # Two artifacts with the SAME name (repeated skill) + a distinct sibling.
    task.artifacts.append(
        Artifact(artifact_id="skill_result_1", name="error_result", parts=[Part(data=_dict_to_value(env_a))])
    )
    task.artifacts.append(
        Artifact(artifact_id="skill_result_2", name="error_result", parts=[Part(data=_dict_to_value(env_b))])
    )
    task.artifacts.append(
        Artifact(
            artifact_id="skill_result_3",
            name="get_products_result",
            parts=[Part(data=_dict_to_value({"products": [{"id": "p-sibling"}]}))],
        )
    )
    handler._task_push_configs[task.id] = TaskPushNotificationConfig(id="pnc_1", url="https://buyer.example/webhook")

    captured: dict = {}

    async def _capture(*, push_notification_config, payload, metadata):
        captured["payload"] = payload

    service = MagicMock()
    service.send_notification = AsyncMock(side_effect=_capture)

    # create_a2a_webhook_payload is NOT mocked — we verify the real emitted wire payload.
    with patch("src.a2a_server.adcp_a2a_server.get_protocol_webhook_service", return_value=service):
        await handler._send_protocol_webhook(task, status="submitted")

    service.send_notification.assert_awaited_once()
    wire = json.dumps(json_format.MessageToDict(captured["payload"]))
    # BOTH same-named error envelopes AND the sibling survive (no name overwrite, no flatten).
    assert "first skill exploded" in wire, f"first same-named artifact dropped: {wire}"
    assert "second skill exploded" in wire, f"second same-named artifact overwritten by name collision: {wire}"
    assert "p-sibling" in wire, f"sibling artifact dropped: {wire}"


def test_read_failed_a2a_task_strict_asserts_on_artifactless_task():
    """Strict mode must trip the artifact-present pin on an artifact-less failed Task.

    Pins the branch order in ``_read_failed_a2a_task``: the
    ``expect_processing_error`` dispatch happens BEFORE the ``task.artifacts``
    guard, so the strict reader's "failed Task must carry the error envelope
    artifact" assertion is reachable. Reverting to guard-first silently
    downgrades strict mode to the loose fallback and this test goes red.
    """
    from a2a.types import TaskStatus

    from tests.harness._base import _read_failed_a2a_task

    bare_failed = Task(id="t-bare", status=TaskStatus(state=TaskState.TASK_STATE_FAILED))

    with pytest.raises(AssertionError, match="must carry the error envelope artifact"):
        _read_failed_a2a_task(bare_failed, fallback_message="x", expect_processing_error=True)


def test_read_failed_a2a_task_loose_falls_back_on_artifactless_task():
    """Loose mode keeps the harness fallback: (None, bare AdCPError) — no raise."""
    from a2a.types import TaskStatus

    from src.core.exceptions import AdCPError
    from tests.harness._base import _read_failed_a2a_task

    bare_failed = Task(id="t-bare", status=TaskStatus(state=TaskState.TASK_STATE_FAILED))

    envelope, error = _read_failed_a2a_task(bare_failed, fallback_message="x")

    assert envelope is None
    assert type(error) is AdCPError, f"expected bare AdCPError fallback, got {type(error).__name__}"
    assert "A2A task failed" in str(error)


@pytest.mark.parametrize(
    ("artifact", "expected_match"),
    [
        pytest.param(None, "must carry the error envelope artifact", id="no-artifact"),
        pytest.param(
            Artifact(artifact_id="e1", name="wrong_name", parts=[Part(text="boom"), Part(data=_dict_to_value({}))]),
            "expected 'error_result' artifact",
            id="wrong-artifact-name",
        ),
        pytest.param(
            Artifact(artifact_id="e1", name="error_result", parts=[Part(data=_dict_to_value({}))]),
            "must carry a human-readable TextPart",
            id="datapart-alone",
        ),
        pytest.param(
            Artifact(
                artifact_id="e1",
                name="error_result",
                parts=[Part(text="boom"), Part(data=_dict_to_value({})), Part(data=_dict_to_value({}))],
            ),
            "exactly one authoritative DataPart",
            id="two-dataparts",
        ),
    ],
)
def test_failed_task_artifact_reader_rejects_off_contract_shapes(artifact, expected_match):
    """Known-bad self-tests for the strict failed-Task reader every A2A error test depends on.

    The reader's pins are what make ``assert_failed_task_envelope`` stronger than a raw
    ``artifacts[0]`` read. Without these, DELETING one of its assertions reddens nothing
    test-side — the oracle could silently weaken while every call site stayed green.
    Mirrors the harness twin's self-tests one file over.
    """
    from tests.utils.a2a_helpers import _read_failed_task_artifact

    task = Task(id="t-shape", status=TaskStatus(state=TaskState.TASK_STATE_FAILED))
    if artifact is not None:
        task.artifacts.append(artifact)

    with pytest.raises(AssertionError, match=expected_match):
        _read_failed_task_artifact(task, "error_result")


@pytest.mark.asyncio
async def test_genuine_transport_fault_still_raises_json_rpc_error():
    """A transport-protocol fault must still surface as a JSON-RPC error.

    Missing authentication for a non-discovery skill is a transport-layer
    fault (the request cannot be routed at all), so ``on_message_send``
    re-raises the ``A2AError`` (here ``InvalidRequestError``) onto the
    JSON-RPC layer instead of returning a failed Task.
    """
    handler = AdCPRequestHandler()
    # No auth token at all — create_media_buy is a non-discovery skill.
    ctx = make_a2a_context(auth_token=None, headers={"host": "test.example.com"})
    message = create_a2a_message_with_skill("create_media_buy", {"product_ids": ["prod_1"]})
    params = SendMessageRequest(message=message)

    with pytest.raises(InvalidRequestError) as exc_info:
        await handler.on_message_send(params, context=ctx)

    assert "authentication" in str(exc_info.value).lower(), (
        f"transport fault should name the missing authentication; got: {exc_info.value}"
    )


@pytest.mark.parametrize(
    ("handler_method", "params"),
    [
        ("on_get_task", GetTaskRequest(id="task_auth")),
        ("on_cancel_task", CancelTaskRequest(id="task_auth")),
    ],
)
@pytest.mark.parametrize("auth_token", [None, "invalid-token"])
@pytest.mark.asyncio
async def test_task_management_auth_failures_stay_on_json_rpc_wire(handler_method, params, auth_token):
    """Missing/invalid task-management auth must remain a serialized transport error.

    ``_durable_lookup_identity`` previously swallowed every resolver exception and made
    tasks/get + tasks/cancel return ``None`` (indistinguishable from task-not-found).
    Drive both public handlers and serialize the real ``InvalidRequestError`` through
    the SDK dispatcher helper so the regression is pinned at the JSON-RPC wire altitude.
    """
    handler = AdCPRequestHandler()
    handler._get_auth_token = MagicMock(return_value=auth_token)
    if auth_token:
        handler._resolve_a2a_identity = MagicMock(
            side_effect=InvalidRequestError(message="Authentication token is invalid or expired.")
        )

    with pytest.raises(InvalidRequestError) as exc_info:
        await getattr(handler, handler_method)(params, context=None)

    wire = build_error_response("req-auth", exc_info.value)
    serialized = serialize_wire_error(wire)
    assert "Authentication" in serialized or "authentication" in serialized
    assert "task_auth" not in serialized, "auth failures must not be downgraded to task-not-found output"


def _auth_guarded_methods():
    """Every A2A method whose auth failure a buyer can branch on, with minimal params.

    Built as a function (not a module constant) so the request objects are fresh per test.

    Includes ``on_message_send`` — it is the method the helper docstring names first, and
    omitting it while claiming coverage is exactly the gap this list exists to close. Its
    params carry a NON-discovery skill so the request actually requires auth (a discovery
    skill is public and would resolve anonymously instead of rejecting).
    """
    from a2a.types import (
        DeleteTaskPushNotificationConfigRequest,
        GetTaskPushNotificationConfigRequest,
        ListTaskPushNotificationConfigsRequest,
    )

    return [
        (
            "on_message_send",
            SendMessageRequest(message=create_a2a_message_with_skill("create_media_buy", {"buyer_ref": "b1"})),
        ),
        ("on_get_task", GetTaskRequest(id="task_envelope")),
        ("on_cancel_task", CancelTaskRequest(id="task_envelope")),
        (
            "on_get_task_push_notification_config",
            GetTaskPushNotificationConfigRequest(id="pnc_1", task_id="task_envelope"),
        ),
        ("on_create_task_push_notification_config", TaskPushNotificationConfig(id="pnc_1", task_id="task_envelope")),
        ("on_list_task_push_notification_configs", ListTaskPushNotificationConfigsRequest(task_id="task_envelope")),
        (
            "on_delete_task_push_notification_config",
            DeleteTaskPushNotificationConfigRequest(id="pnc_1", task_id="task_envelope"),
        ),
    ]


@pytest.mark.parametrize(("handler_method", "params"), _auth_guarded_methods())
@pytest.mark.asyncio
async def test_every_auth_guarded_method_carries_the_auth_missing_envelope(handler_method, params):
    """EVERY A2A method's missing-credential failure carries AUTH_MISSING in ``data``.

    "Stays on the JSON-RPC wire" and "carries the envelope in ``data``" are orthogonal: the
    sibling test above pins only the former. Regression this closes: the envelope was attached
    at the message/send arm alone, so a buyer branching on
    ``error.data.adcp_error.code == "AUTH_MISSING"`` got it from message/send but NOT from
    tasks/get, tasks/cancel, or any push-notification-config method. All auth raises now share
    one enveloped source, so this parametrization reddens if any arm regresses to a bare error.
    """
    handler = AdCPRequestHandler()
    handler._get_auth_token = MagicMock(return_value=None)
    context = make_a2a_context(
        auth_token=None,
        headers={"x-adcp-tenant": "tenant-from-a2a-headers"},
    )
    records, recorder = _capture_a2a_auth_records()

    with (
        patch("src.core.resolved_identity.resolve_identity") as mock_resolve,
        patch("src.a2a_server.adcp_a2a_server.record_boundary_error", side_effect=recorder),
    ):
        with pytest.raises(InvalidRequestError) as exc_info:
            await getattr(handler, handler_method)(params, context=context)

    mock_resolve.assert_not_called()
    err = exc_info.value
    assert records == [("a2a", "authentication", "AUTH_MISSING", None, None)]
    assert err.data is not None, f"{handler_method} auth failure dropped the AdCP envelope from error.data"
    # Assert on the SERIALIZED JSON-RPC body the buyer actually receives, not the raised exception
    # object: ``build_error_response`` is the SDK dispatcher's own serializer, so a payload that
    # failed to survive it would never reach a buyer regardless of what the object held.
    body = build_error_response("req-auth", err)
    assert_envelope_shape(body["error"]["data"], "AUTH_MISSING", recovery="correctable")
    # Both wire layers must carry the SAME sanitized message (per _enveloped_invalid_request's
    # contract), and the suggestion must be the pinned-spec AUTH_MISSING hint — neither property was
    # pinned above, so a regression that desynced ``error.message`` from the envelope's message, or
    # dropped/replaced the suggestion, would have gone undetected.
    assert body["error"]["message"] == body["error"]["data"]["adcp_error"]["message"]
    assert body["error"]["data"]["adcp_error"]["suggestion"] == pinned_error_code_suggestion("AUTH_MISSING")


# The parametrization above drives only the MISSING-TOKEN arm. The remaining auth arms each
# need their own trigger, and without these a revert of any one of them to a bare
# InvalidRequestError reddens nothing.


@pytest.mark.asyncio
@pytest.mark.parametrize(("handler_method", "params"), _auth_guarded_methods())
async def test_auth_resolution_failure_arm_carries_the_auth_invalid_envelope(handler_method, params):
    """Every A2A auth boundary classifies a rejected presented token as AUTH_INVALID."""
    handler = AdCPRequestHandler()
    handler._get_auth_token = MagicMock(return_value="a-rejected-token")
    auth_error = AdCPAuthenticationError("Token rejected by the credential store.")

    context = make_a2a_context(
        auth_token="a-rejected-token",
        headers={
            "Authorization": "Bearer a-rejected-token",
            "x-adcp-tenant": "tenant-from-a2a-headers",
        },
    )
    records, recorder = _capture_a2a_auth_records()

    with (
        patch(
            "src.core.resolved_identity.resolve_identity",
            side_effect=auth_error,
        ) as mock_resolve,
        patch("src.a2a_server.adcp_a2a_server.record_boundary_error", side_effect=recorder),
    ):
        with pytest.raises(InvalidRequestError) as exc_info:
            await getattr(handler, handler_method)(params, context=context)

    assert len(mock_resolve.call_args_list) == 1
    resolve_call = mock_resolve.call_args_list[0]
    assert resolve_call.kwargs["auth_token"] == "a-rejected-token"
    assert resolve_call.kwargs["require_valid_token"] is True
    assert resolve_call.kwargs["protocol"] == "a2a"
    assert records == [("a2a", "authentication", "AUTH_INVALID", None, None)]
    assert_envelope_shape(exc_info.value.data, "AUTH_INVALID", recovery="terminal")


@pytest.mark.asyncio
@pytest.mark.parametrize(("handler_method", "params"), _auth_guarded_methods())
async def test_principal_less_identity_arm_carries_the_auth_invalid_envelope(handler_method, params):
    """Every A2A auth boundary classifies a principal-less token as AUTH_INVALID."""
    handler = AdCPRequestHandler()
    handler._get_auth_token = MagicMock(return_value="a-stale-token")
    principal_less = PrincipalFactory.make_identity(
        principal_id=None, tenant_id="test-tenant", tenant={"tenant_id": "test-tenant"}, protocol="a2a"
    )
    records, recorder = _capture_a2a_auth_records()

    with (
        patch("src.core.resolved_identity.resolve_identity", return_value=principal_less) as mock_resolve,
        patch("src.a2a_server.adcp_a2a_server.record_boundary_error", side_effect=recorder),
    ):
        with pytest.raises(InvalidRequestError) as exc_info:
            await getattr(handler, handler_method)(
                params,
                context=make_a2a_context(
                    auth_token="a-stale-token",
                    headers={"Authorization": "Bearer a-stale-token"},
                ),
            )

    assert len(mock_resolve.call_args_list) == 1
    assert mock_resolve.call_args_list[0].kwargs["require_valid_token"] is True
    assert records == [("a2a", "authentication", "AUTH_INVALID", None, None)]
    assert_envelope_shape(exc_info.value.data, "AUTH_INVALID", recovery="terminal")


@pytest.mark.asyncio
async def test_rejected_legacy_a2a_token_without_authorization_is_auth_missing():
    """A2A must not let the accepted legacy header redefine the v3.1.1 split."""
    handler = AdCPRequestHandler()
    context = make_a2a_context(
        auth_token="legacy-rejected-token",
        headers={
            "x-adcp-auth": "legacy-rejected-token",
            "x-adcp-tenant": "header-selected-tenant",
        },
    )

    with patch(
        "src.core.resolved_identity.resolve_identity",
        side_effect=AdCPAuthInvalidError("legacy token rejected"),
    ):
        with pytest.raises(InvalidRequestError) as exc_info:
            handler._resolve_a2a_identity(
                "legacy-rejected-token",
                require_valid_token=True,
                context=context,
            )

    assert_envelope_shape(exc_info.value.data, "AUTH_MISSING", recovery="correctable")


@pytest.mark.asyncio
async def test_explicit_skill_identity_guard_carries_the_envelope():
    """The standalone `_handle_explicit_skill` guard — a non-discovery skill with no identity.

    Not reachable through the missing-token parametrization: it fires after dispatch, on the
    identity object rather than the token.
    """
    handler = AdCPRequestHandler()
    records, recorder = _capture_a2a_auth_records()

    with patch("src.a2a_server.adcp_a2a_server.record_boundary_error", side_effect=recorder):
        with pytest.raises(InvalidRequestError) as exc_info:
            await handler._handle_explicit_skill("create_media_buy", {"buyer_ref": "b1"}, None)

    assert records == [("a2a", "authentication", "AUTH_MISSING", None, None)]
    assert_envelope_shape(exc_info.value.data, "AUTH_MISSING", recovery="correctable")


@pytest.mark.asyncio
async def test_tenantless_authenticated_principal_is_a_terminal_config_error_not_auth_required():
    """Valid credentials + no resolvable tenant is a SELLER-side failure, not a buyer auth problem.

    Emitting AUTH_REQUIRED here would tell a buyer to fix credentials that are already correct.
    The pinned 3.1.1 enum classifies CONFIGURATION_ERROR as terminal, and it is an internal wire
    code — so the message is scrubbed, which is what keeps the principal id off the wire.
    """
    handler = AdCPRequestHandler()
    handler._get_auth_token = MagicMock(return_value="a-valid-token")
    tenantless = PrincipalFactory.make_identity(principal_id="p-orphan", tenant_id=None, tenant=None, protocol="a2a")
    records, recorder = _capture_a2a_auth_records()

    with (
        patch("src.core.resolved_identity.resolve_identity", return_value=tenantless),
        patch("src.a2a_server.adcp_a2a_server.record_boundary_error", side_effect=recorder),
    ):
        with pytest.raises(InvalidRequestError) as exc_info:
            await handler.on_get_task(GetTaskRequest(id="task_envelope"), context=None)

    assert records == [("a2a", "authentication", "CONFIGURATION_ERROR", None, "p-orphan")]
    err = exc_info.value
    assert_envelope_shape(err.data, "CONFIGURATION_ERROR", recovery="terminal")
    assert "p-orphan" not in json.dumps(err.data, default=str), "principal id leaked to the wire"
