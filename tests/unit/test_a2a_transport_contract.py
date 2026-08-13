"""A2A Transport Contract Tests — Phase 0 regression gate for handler migration.

These tests verify the HTTP boundary shape for all A2A skills:
- Route existence (not 404)
- Auth contract (discovery vs auth-required)
- JSON-RPC protocol correctness
- Response field presence (shape, not values)

They use TestClient (in-process ASGI) with mocked _impl functions.
No Docker required. This is the regression gate between every Phase 2 step.

beads: salesagent-b61l.17
"""

import json
import uuid
from contextlib import nullcontext
from unittest.mock import ANY, patch

import pytest
from starlette.testclient import TestClient

from src.a2a_server.adcp_a2a_server import DISCOVERY_SKILLS as _PROD_DISCOVERY_SKILLS
from src.a2a_server.adcp_a2a_server import AdCPRequestHandler
from src.app import app
from src.core.auth_policy import AUTH_OPTIONAL_SKILLS
from src.core.exceptions import AdCPAuthenticationError
from src.core.mcp_auth_middleware import AUTH_OPTIONAL_TOOLS
from tests.helpers import assert_envelope_shape
from tests.helpers.pinned_schema import pinned_error_code_suggestion
from tests.utils.a2a_helpers import make_test_a2a_identity

_TEST_IDENTITY = make_test_a2a_identity()
_AUTH_MISSING_SUGGESTION = pinned_error_code_suggestion("AUTH_MISSING")
_AUTH_INVALID_SUGGESTION = pinned_error_code_suggestion("AUTH_INVALID")

# Task-management dispatch is selected by METHOD NAME, not by the version header: the app
# builds its A2A routes with ``enable_v0_3_compat=True`` (src/app.py), so the v0.3 names
# reach ``a2a/compat/v0_3/jsonrpc_adapter.py``. That adapter's ``handle_request`` has no
# ``except A2AError`` arm — only ``except Exception -> CoreInternalError(message=str(e))``,
# which takes no ``data``. So a raised ``InternalError(message=..., data=envelope)`` is
# REBUILT without its envelope on that path.
#
# Both halves are graded because the v1.0 rows alone cannot see the gap, and the installed
# adcp client emits the v0.3 names. Observed at this head, auth rejection on each:
#     GetTask      v1.0 -> code -32600, data = two-layer envelope
#     tasks/get    v0.3 -> code -32603, data = null
#     CancelTask   v1.0 -> code -32600, data = two-layer envelope
#     tasks/cancel v0.3 -> code -32603, data = null
# Tracked as #1670. Raising the typed error is still correct — it is what will surface the
# envelope once the compat adapter maps A2AError — so the fix is upstream, not here.
_TASK_METHOD_DISPATCH = [
    pytest.param("GetTask", "1.0", True, id="GetTask-v1.0"),
    pytest.param("CancelTask", "1.0", True, id="CancelTask-v1.0"),
    pytest.param("tasks/get", "0.3", False, id="tasks-get-v0.3-envelope-dropped"),
    pytest.param("tasks/cancel", "0.3", False, id="tasks-cancel-v0.3-envelope-dropped"),
]

# ---------------------------------------------------------------------------
# Explicit per-skill contract. Every registered skill MUST have an entry here
# (asserted), and each entry is INDEPENDENTLY authored — NOT derived from the
# registry — so the checks are not tautological: a new dispatch entry can't pass
# without a human classifying it, and a skill routed to the wrong handler is
# caught by the per-skill wire assertion.
#   classification: "implemented" (must NOT return UNSUPPORTED_FEATURE) or
#                   "unsupported" (must return a UNSUPPORTED_FEATURE failed Task)
#   advertised:     whether the agent card advertises it (asserted for exact equality)
#   discovery:      whether the skill is auth-OPTIONAL (no principal required); HAND-AUTHORED
#                   here and asserted for exact equality against production's DISCOVERY_SKILLS,
#                   so a skill wrongly flipped into/out of the discovery set in production
#                   reddens the build instead of silently re-partitioning the test.
#   params:         request params that reach the skill's terminal branch
# ---------------------------------------------------------------------------
SKILL_METADATA: dict[str, dict] = {
    "get_adcp_capabilities": {"classification": "implemented", "advertised": True, "discovery": True, "params": {}},
    "get_products": {"classification": "implemented", "advertised": True, "discovery": True, "params": {}},
    "create_media_buy": {"classification": "implemented", "advertised": True, "discovery": False, "params": {}},
    "list_creative_formats": {"classification": "implemented", "advertised": True, "discovery": True, "params": {}},
    "list_accounts": {"classification": "implemented", "advertised": True, "discovery": False, "params": {}},
    "sync_accounts": {"classification": "implemented", "advertised": True, "discovery": False, "params": {}},
    "list_authorized_properties": {
        "classification": "implemented",
        "advertised": True,
        "discovery": True,
        "params": {},
    },
    "update_media_buy": {"classification": "implemented", "advertised": True, "discovery": False, "params": {}},
    "get_media_buys": {"classification": "implemented", "advertised": True, "discovery": False, "params": {}},
    "get_media_buy_delivery": {"classification": "implemented", "advertised": True, "discovery": False, "params": {}},
    "update_performance_index": {
        "classification": "implemented",
        "advertised": True,
        "discovery": False,
        "params": {},
    },
    "sync_creatives": {"classification": "implemented", "advertised": True, "discovery": False, "params": {}},
    "list_creatives": {"classification": "implemented", "advertised": True, "discovery": False, "params": {}},
    # Unsupported + NOT advertised: handlers raise UNSUPPORTED_FEATURE, so they
    # are hidden from the agent card but stay reachable-but-unsupported by name.
    "approve_creative": {"classification": "unsupported", "advertised": False, "discovery": False, "params": {}},
    "get_media_buy_status": {"classification": "unsupported", "advertised": False, "discovery": False, "params": {}},
    "optimize_media_buy": {"classification": "unsupported", "advertised": False, "discovery": False, "params": {}},
    "create_creative": {
        "classification": "unsupported",
        "advertised": False,
        "discovery": False,
        "params": {"format_id": "display_300x250", "content_uri": "https://ex/c.jpg", "name": "c"},
    },
    "assign_creative": {
        "classification": "unsupported",
        "advertised": False,
        "discovery": False,
        "params": {"media_buy_id": "mb-1", "package_id": "pkg-1", "creative_id": "cr-1"},
    },
}

ALL_SKILLS = sorted(AdCPRequestHandler()._skill_handler_map().keys())
IMPLEMENTED_SKILLS = sorted(s for s, m in SKILL_METADATA.items() if m["classification"] == "implemented")
UNSUPPORTED_SKILLS = sorted(s for s, m in SKILL_METADATA.items() if m["classification"] == "unsupported")
ADVERTISED_SKILLS = {s for s, m in SKILL_METADATA.items() if m["advertised"]}

# Discovery (no-auth) skills come from the HAND-AUTHORED oracle above (not the production
# frozenset), so the auth-boundary tests can't move in lockstep with a production mis-classification.
# ``test_discovery_metadata_matches_production`` pins this set equal to production's DISCOVERY_SKILLS.
DISCOVERY_SKILLS = sorted(s for s, m in SKILL_METADATA.items() if m["discovery"])

AUTH_REQUIRED_SKILLS = [s for s in ALL_SKILLS if s not in DISCOVERY_SKILLS]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_jsonrpc(skill: str, params: dict | None = None, request_id: str | None = None) -> dict:
    """Build a JSON-RPC 2.0 SendMessage request with explicit skill invocation."""
    return {
        "jsonrpc": "2.0",
        "id": request_id or str(uuid.uuid4()),
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": str(uuid.uuid4()),
                "role": "ROLE_USER",
                "parts": [{"data": {"skill": skill, "parameters": params or {}}}],
            }
        },
    }


def _extract_jsonrpc_result(response) -> dict:
    """Extract the result from a JSON-RPC success response."""
    body = response.json()
    assert "result" in body, f"Expected JSON-RPC result, got: {json.dumps(body, indent=2)[:500]}"
    return body["result"]


def _extract_jsonrpc_error(response) -> dict:
    """Extract the error from a JSON-RPC error response."""
    body = response.json()
    assert "error" in body, f"Expected JSON-RPC error, got: {json.dumps(body, indent=2)[:500]}"
    return body["error"]


def _extract_artifact_data(result: dict) -> dict:
    """Extract data from the first artifact's DataPart.

    a2a-sdk 1.0 protobuf format: result is {"task": {...}} or {"message": {...}}.
    Parts use oneof: {"data": {...}} or {"text": "..."} (no "kind" field).
    """
    # Unwrap task envelope if present
    task = result.get("task", result)
    artifacts = task.get("artifacts", [])
    if not artifacts:
        return {}
    for part in artifacts[0].get("parts", []):
        if "data" in part:
            return part["data"]
    return {}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    """TestClient for the unified FastAPI app."""
    c = TestClient(app, raise_server_exceptions=False)
    yield c
    c.close()


@pytest.fixture
def auth_headers():
    """Headers with a valid Bearer token."""
    return {"Authorization": "Bearer test-transport-token", "Content-Type": "application/json", "A2A-Version": "1.0"}


@pytest.fixture
def no_auth_headers():
    """Headers without authentication."""
    return {"Content-Type": "application/json", "A2A-Version": "1.0"}


# ---------------------------------------------------------------------------
# Route Existence
# ---------------------------------------------------------------------------


class TestA2ARouteExistence:
    """Verify A2A routes exist (not 404)."""

    def test_a2a_endpoint_exists(self, client):
        """POST /a2a should not return 404."""
        payload = _build_jsonrpc("get_products", {"brief": "test"})
        response = client.post("/a2a", json=payload)
        assert response.status_code != 404, "A2A endpoint /a2a should exist"

    def test_agent_card_endpoint_exists(self, client):
        """GET /.well-known/agent-card.json should return 200."""
        response = client.get("/.well-known/agent-card.json")
        assert response.status_code == 200

    def test_agent_card_has_required_fields(self, client):
        """Agent card must have name, supportedInterfaces, skills, capabilities."""
        response = client.get("/.well-known/agent-card.json")
        card = response.json()
        for field in ["name", "supportedInterfaces", "skills", "capabilities"]:
            assert field in card, f"Agent card missing '{field}'"
        assert card["name"] == "Prebid Sales Agent"
        # a2a-sdk 1.0: URL is inside supportedInterfaces, not top-level
        assert len(card["supportedInterfaces"]) > 0
        assert "url" in card["supportedInterfaces"][0]


# ---------------------------------------------------------------------------
# Auth Contract
# ---------------------------------------------------------------------------


class TestA2AAuthContract:
    """Verify auth boundary: discovery vs auth-required skills."""

    @pytest.mark.parametrize("skill", DISCOVERY_SKILLS)
    def test_discovery_skills_accept_no_auth(self, client, no_auth_headers, skill):
        """Discovery skills should NOT return auth error without token."""
        payload = _build_jsonrpc(skill, {})
        response = client.post("/a2a", json=payload, headers=no_auth_headers)
        body = response.json()
        # Should not get an auth error
        if "error" in body:
            error_msg = body["error"].get("message", "").lower()
            # Check for explicit auth rejection (not just "authorized" in property names)
            auth_rejection_phrases = [
                "authentication token required",
                "missing authentication token",
                "bearer token required",
            ]
            for phrase in auth_rejection_phrases:
                assert phrase not in error_msg, (
                    f"Discovery skill '{skill}' rejected unauthenticated request: {body['error']}"
                )

    @pytest.mark.parametrize("skill", AUTH_REQUIRED_SKILLS)
    def test_auth_required_skills_reject_no_auth(self, client, no_auth_headers, skill):
        """Auth-required skills MUST reject requests without token."""
        payload = _build_jsonrpc(skill, {})
        response = client.post("/a2a", json=payload, headers=no_auth_headers)
        body = response.json()
        assert "error" in body, f"Auth-required skill '{skill}' should return error without token"
        error_msg = body["error"].get("message", "").lower()
        assert "auth" in error_msg or "token" in error_msg, (
            f"Error for '{skill}' should mention auth/token: {body['error']['message']}"
        )
        assert_envelope_shape(body["error"]["data"], "AUTH_MISSING", recovery="correctable")
        assert body["error"]["data"]["adcp_error"]["suggestion"] == _AUTH_MISSING_SUGGESTION

    @pytest.mark.parametrize(
        ("headers", "resolver_error", "expected_code", "expected_recovery", "expected_suggestion"),
        [
            (
                {"Content-Type": "application/json", "A2A-Version": "1.0"},
                None,
                "AUTH_MISSING",
                "correctable",
                _AUTH_MISSING_SUGGESTION,
            ),
            (
                {
                    "Authorization": "Basic malformed",
                    "Content-Type": "application/json",
                    "A2A-Version": "1.0",
                },
                None,
                "AUTH_INVALID",
                "terminal",
                _AUTH_INVALID_SUGGESTION,
            ),
            (
                {
                    "Authorization": "Bearer ",
                    "Content-Type": "application/json",
                    "A2A-Version": "1.0",
                },
                None,
                "AUTH_INVALID",
                "terminal",
                _AUTH_INVALID_SUGGESTION,
            ),
            (
                {
                    "Authorization": "Bearer invalid-task-token",
                    "Content-Type": "application/json",
                    "A2A-Version": "1.0",
                },
                "Authentication token is invalid or expired.",
                "AUTH_INVALID",
                "terminal",
                _AUTH_INVALID_SUGGESTION,
            ),
        ],
        ids=["missing-token", "malformed-authorization", "empty-bearer", "invalid-token"],
    )
    @pytest.mark.parametrize(("method", "version", "envelope_survives"), _TASK_METHOD_DISPATCH)
    def test_task_management_auth_errors_use_json_rpc_dispatcher(
        self,
        client,
        method,
        version,
        envelope_survives,
        headers,
        resolver_error,
        expected_code,
        expected_recovery,
        expected_suggestion,
    ):
        """The real dispatcher must serialize task auth failures as JSON-RPC errors, carrying the
        full two-layer auth envelope in ``error.data`` on the v1.0 names — and NOT on their v0.3
        aliases, which is graded here rather than assumed.

        Drives the ACTUAL SDK dispatcher via a real HTTP POST (not a direct handler call), so this
        catches regressions in dispatcher routing, request parsing, and response serialization that
        a unit test calling the handler method directly cannot see.

        The invalid-token case patches ``resolve_identity`` at its SOURCE module (not
        ``_resolve_a2a_identity`` itself) so the REAL ``_resolve_a2a_identity`` exception
        handling — and its own enveloping via ``_enveloped_auth_error`` — actually runs. An
        earlier version of this test patched ``_resolve_a2a_identity`` with a bare
        ``InvalidRequestError`` side effect, which bypassed that enveloping entirely and made
        the invalid-token case silently untested for ``error.data``.
        """
        headers = {**headers, "A2A-Version": version}
        payload = {"jsonrpc": "2.0", "id": "task-auth-request", "method": method, "params": {"id": "task_auth"}}
        resolver_patch = (
            patch(
                "src.core.resolved_identity.resolve_identity",
                side_effect=AdCPAuthenticationError(resolver_error),
            )
            if resolver_error
            else nullcontext()
        )

        with resolver_patch as mock_resolve:
            response = client.post("/a2a", json=payload, headers=headers)

        if resolver_error:
            assert mock_resolve is not None
            assert len(mock_resolve.call_args_list) == 1
            resolve_call = mock_resolve.call_args_list[0]
            assert resolve_call.kwargs["auth_token"] == "invalid-task-token"
            assert resolve_call.kwargs["require_valid_token"] is True
            assert resolve_call.kwargs["protocol"] == "a2a"
        body = response.json()
        assert body["jsonrpc"] == "2.0"
        assert body["id"] == "task-auth-request"
        assert "result" not in body
        # Holds on BOTH paths: the message is the sanitized text and no task id rides along,
        # so the compat flattening costs the machine-readable code, never disclosure.
        assert "auth" in body["error"]["message"].lower()
        assert "task_auth" not in json.dumps(body)

        if not envelope_survives:
            # v0.3 compat path (#1670): the adapter's bare `except Exception` rebuilds the error
            # as CoreInternalError(message=str(e)), so the code becomes -32603 and `data` is gone.
            # Asserted rather than skipped, so the day #1670 restores the mapping this row goes
            # red and forces `_internal_error_for`'s docstring to be corrected with it.
            assert body["error"]["code"] == -32603
            assert body["error"].get("data") is None, (
                f"{method} now carries error.data — the v0.3 compat adapter gained an A2AError "
                f"mapping. Update this table and _internal_error_for's docstring together (#1670)."
            )
            return

        assert body["error"]["code"] == -32600
        # The two-layer envelope must ALSO reach the real wire, not just a bare JSON-RPC message —
        # a buyer branches on error.data.adcp_error.code, and this is what previously required a
        # separate unit test calling the handler directly (which cannot prove the real dispatcher
        # actually places the envelope in the serialized HTTP response body).
        assert_envelope_shape(body["error"]["data"], expected_code, recovery=expected_recovery)
        adcp_error = body["error"]["data"]["adcp_error"]
        assert adcp_error["suggestion"] == expected_suggestion
        assert body["error"]["message"] == adcp_error["message"]

    @pytest.mark.parametrize("skill", ["create_media_buy", "list_accounts"])
    @pytest.mark.parametrize("authorization", ["Basic malformed", "Bearer "], ids=["basic", "empty-bearer"])
    def test_non_discovery_skill_rejects_presented_unusable_credentials(self, client, authorization, skill):
        """The message/send auth guard classifies malformed presented credentials as AUTH_INVALID."""
        response = client.post(
            "/a2a",
            json=_build_jsonrpc(skill),
            headers={
                "Authorization": authorization,
                "Content-Type": "application/json",
                "A2A-Version": "1.0",
            },
        )

        body = response.json()
        assert body["error"]["code"] == -32600
        assert_envelope_shape(body["error"]["data"], "AUTH_INVALID", recovery="terminal")
        assert body["error"]["data"]["adcp_error"]["suggestion"] == _AUTH_INVALID_SUGGESTION

    @patch("src.core.resolved_identity.resolve_identity", return_value=_TEST_IDENTITY)
    def test_x_adcp_auth_precedes_malformed_authorization(self, mock_resolve, client):
        """The A2A middleware resolves the x-adcp-auth credential when both headers are present."""
        response = client.post(
            "/a2a",
            json=_build_jsonrpc("create_media_buy"),
            headers={
                "x-adcp-auth": "valid-x-adcp-token",
                "Authorization": "Basic malformed",
                "Content-Type": "application/json",
                "A2A-Version": "1.0",
            },
        )

        assert response.json().get("result") is not None
        mock_resolve.assert_called_once_with(
            headers=ANY,
            auth_token="valid-x-adcp-token",
            require_valid_token=True,
            protocol="a2a",
            testing_context=ANY,
        )

    @patch("src.core.resolved_identity.resolve_identity")
    @pytest.mark.parametrize("skill", ["create_media_buy", "list_accounts"])
    def test_invalid_x_adcp_auth_does_not_fall_back_to_valid_bearer(self, mock_resolve, client, skill):
        """A rejected x-adcp-auth credential cannot be bypassed by a valid-looking Bearer token."""
        mock_resolve.side_effect = AdCPAuthenticationError("Authentication token is invalid or expired.")
        response = client.post(
            "/a2a",
            json=_build_jsonrpc(skill),
            headers={
                "x-adcp-auth": "rejected-x-adcp-token",
                "Authorization": "Bearer valid-looking-fallback",
                "Content-Type": "application/json",
                "A2A-Version": "1.0",
            },
        )

        body = response.json()
        assert body["error"]["code"] == -32600
        assert_envelope_shape(body["error"]["data"], "AUTH_INVALID", recovery="terminal")
        assert body["error"]["data"]["adcp_error"]["suggestion"] == _AUTH_INVALID_SUGGESTION
        calls = mock_resolve.call_args_list
        assert len(calls) == 1
        assert calls[0].kwargs["require_valid_token"] is True
        assert calls[0].kwargs["auth_token"] == "rejected-x-adcp-token"
        assert calls[0].kwargs["protocol"] == "a2a"


# ---------------------------------------------------------------------------
# JSON-RPC Protocol
# ---------------------------------------------------------------------------


class TestA2AJsonRpcProtocol:
    """Verify JSON-RPC protocol compliance."""

    def test_invalid_method_returns_error(self, client, auth_headers):
        """Unknown JSON-RPC method should return method-not-found error."""
        payload = {"jsonrpc": "2.0", "id": "test-1", "method": "nonexistent/method", "params": {}}
        response = client.post("/a2a", json=payload, headers=auth_headers)
        body = response.json()
        assert "error" in body, "Unknown method should return JSON-RPC error"

    @patch("src.core.resolved_identity.resolve_identity", return_value=_TEST_IDENTITY)
    def test_unknown_skill_returns_failed_task_not_transport_error(self, mock_resolve, client, auth_headers):
        """Unknown skill name returns a failed Task with UNSUPPORTED_FEATURE, not JSON-RPC.

        The JSON-RPC method (message/send) is valid; routing failed *inside*
        skill dispatch, which is an application-layer failure. Per AdCP
        3.1.1 transport-errors.mdx "Layer Separation" it belongs in the
        task body as a failed Task carrying a two-layer envelope — JSON-RPC
        MethodNotFoundError is reserved for unknown JSON-RPC *methods*
        (see test_invalid_method_returns_error). Identity is mocked so the
        request reaches skill dispatch.
        """
        buyer_controlled_skill = "Bearer_secret_hunter2"
        payload = _build_jsonrpc(buyer_controlled_skill, {})
        response = client.post("/a2a", json=payload, headers=auth_headers)
        body = response.json()

        assert "error" not in body, f"unknown skill must not be a JSON-RPC error: {body.get('error')}"
        assert "result" in body, f"expected a failed-Task result, got: {json.dumps(body)[:400]}"
        data = _extract_artifact_data(body["result"])
        assert_envelope_shape(data, "UNSUPPORTED_FEATURE", recovery="correctable")
        assert data["errors"][0]["message"] == (
            "The requested skill is not supported. Call discovery to list available skills."
        )
        assert buyer_controlled_skill not in json.dumps(body)

    def test_response_echoes_request_id(self, client, auth_headers):
        """JSON-RPC response must echo the request id."""
        payload = _build_jsonrpc("get_products", {"brief": "test"}, request_id="echo-test-42")
        response = client.post("/a2a", json=payload, headers=auth_headers)
        body = response.json()
        assert body.get("id") == "echo-test-42", "Response must echo request id"

    def test_response_has_jsonrpc_field(self, client, auth_headers):
        """Response must have jsonrpc: '2.0' field."""
        payload = _build_jsonrpc("get_products", {"brief": "test"})
        response = client.post("/a2a", json=payload, headers=auth_headers)
        body = response.json()
        assert body.get("jsonrpc") == "2.0", "Response must have jsonrpc: '2.0'"

    def test_numeric_request_id_handled(self, client, auth_headers):
        """Numeric JSON-RPC id should be handled (middleware converts to string)."""
        payload = {
            "jsonrpc": "2.0",
            "id": 42,
            "method": "SendMessage",
            "params": {"message": {"messageId": "msg-1", "role": "ROLE_USER", "parts": [{"text": "hello"}]}},
        }
        response = client.post("/a2a", json=payload, headers=auth_headers)
        # Should not crash with TypeError
        assert response.status_code != 500 or b"TypeError" not in response.content


# ---------------------------------------------------------------------------
# Response Shape — Key Skills
# ---------------------------------------------------------------------------


class TestA2AResponseShape:
    """Verify response field shapes for representative skills.

    These tests mock _impl functions to return known responses,
    testing the full transport chain: middleware → dispatch → serialization.
    """

    @patch("src.core.resolved_identity.resolve_identity", return_value=_TEST_IDENTITY)
    @patch("src.core.tools.products._get_products_impl")
    def test_get_products_response_shape(self, mock_impl, mock_resolve, client, auth_headers):
        """get_products response must contain 'products' list."""
        from src.core.schemas import GetProductsResponse

        mock_impl.return_value = GetProductsResponse(products=[], message="test")

        payload = _build_jsonrpc("get_products", {"brief": "test"})
        response = client.post("/a2a", json=payload, headers=auth_headers)
        body = response.json()

        if "result" in body:
            result = body["result"]
            assert "task" in result, "SendMessage result must contain 'task'"
            data = _extract_artifact_data(result)
            assert "products" in data, "get_products response must have 'products' field"
            assert isinstance(data["products"], list)

    @patch("src.core.resolved_identity.resolve_identity", return_value=_TEST_IDENTITY)
    @patch("src.core.tools.media_buy_create._create_media_buy_impl")
    def test_create_media_buy_response_shape(self, mock_impl, mock_resolve, client, auth_headers):
        """create_media_buy response must have media_buy_id."""
        from adcp.types.aliases import CreateMediaBuySuccessResponse

        mock_impl.return_value = CreateMediaBuySuccessResponse(
            media_buy_id="mb-test-1",
            packages=[],
            # adcp 6.6 (spec 3.1.1) made these required on the success envelope
            status="completed",
            confirmed_at="2026-03-01T00:00:00Z",
            revision=1,
        )

        payload = _build_jsonrpc(
            "create_media_buy",
            {
                "brand": {"domain": "testbrand.com"},
                "packages": [{"product_id": "p1", "budget": 1000.0, "pricing_option_id": "cpm"}],
                "start_time": "2026-03-01T00:00:00Z",
                "end_time": "2026-03-31T00:00:00Z",
                "idempotency_key": "unit-test-key-a2a-shape-0001",
            },
        )
        response = client.post("/a2a", json=payload, headers=auth_headers)
        body = response.json()

        if "result" in body:
            data = _extract_artifact_data(body["result"])
            assert "media_buy_id" in data, "create_media_buy response must have 'media_buy_id'"

    def test_error_format_is_jsonrpc(self, client, auth_headers):
        """Error responses must use JSON-RPC error envelope, not {success: false}."""
        # Send a request that will fail (unknown skill)
        payload = _build_jsonrpc("nonexistent_skill", {})
        response = client.post("/a2a", json=payload, headers=auth_headers)
        body = response.json()

        # Must be JSON-RPC format
        assert "error" in body or "result" in body, "Response must be JSON-RPC format"
        if "error" in body:
            assert "code" in body["error"], "JSON-RPC error must have 'code'"
            assert "message" in body["error"], "JSON-RPC error must have 'message'"

    @patch("src.core.resolved_identity.resolve_identity", return_value=_TEST_IDENTITY)
    @patch("src.a2a_server.adcp_a2a_server.core_sync_creatives_tool")
    def test_sync_creatives_response_shape(self, mock_impl, mock_resolve, client, auth_headers):
        """sync_creatives response must contain 'creatives' or 'synced_creatives'."""
        from src.core.schemas import SyncCreativesResponse

        mock_impl.return_value = SyncCreativesResponse(creatives=[], failed_creatives=[])

        payload = _build_jsonrpc("sync_creatives", {"creatives": []})
        response = client.post("/a2a", json=payload, headers=auth_headers)
        body = response.json()

        if "result" in body:
            data = _extract_artifact_data(body["result"])
            assert "creatives" in data or "synced_creatives" in data, (
                "sync_creatives response must have 'creatives' field"
            )

    @patch("src.core.resolved_identity.resolve_identity", return_value=_TEST_IDENTITY)
    @patch("src.a2a_server.adcp_a2a_server.core_list_creatives_tool")
    def test_list_creatives_response_shape(self, mock_impl, mock_resolve, client, auth_headers):
        """list_creatives response must contain 'creatives' list."""
        from src.core.schemas import ListCreativesResponse

        # adcp 3.6.0: Pagination uses cursor-based pagination (has_more, total_count, cursor)
        mock_impl.return_value = ListCreativesResponse(
            creatives=[],
            pagination={"has_more": False, "total_count": 0},
            query_summary={"filters_applied": [], "returned": 0, "total_matching": 0},
        )

        payload = _build_jsonrpc("list_creatives", {})
        response = client.post("/a2a", json=payload, headers=auth_headers)
        body = response.json()

        if "result" in body:
            data = _extract_artifact_data(body["result"])
            assert "creatives" in data, "list_creatives response must have 'creatives' field"
            assert isinstance(data["creatives"], list)

    @patch("src.core.resolved_identity.resolve_identity", return_value=_TEST_IDENTITY)
    @patch("src.a2a_server.adcp_a2a_server.core_update_media_buy_tool")
    def test_update_media_buy_response_shape(self, mock_impl, mock_resolve, client, auth_headers):
        """update_media_buy response must have media_buy_id."""
        from adcp.types.aliases import UpdateMediaBuySuccessResponse

        mock_impl.return_value = UpdateMediaBuySuccessResponse(
            media_buy_id="mb-test-1",
            affected_packages=[],
            # adcp 6.6 (spec 3.1.1) made these required on the success envelope
            status="completed",
            revision=1,
        )

        payload = _build_jsonrpc("update_media_buy", {"media_buy_id": "mb-test-1", "paused": False})
        response = client.post("/a2a", json=payload, headers=auth_headers)
        body = response.json()

        if "result" in body:
            data = _extract_artifact_data(body["result"])
            assert "media_buy_id" in data, "update_media_buy response must have 'media_buy_id'"

    @patch("src.core.resolved_identity.resolve_identity", return_value=_TEST_IDENTITY)
    @patch("src.a2a_server.adcp_a2a_server.core_get_media_buy_delivery_tool")
    def test_get_media_buy_delivery_response_shape(self, mock_impl, mock_resolve, client, auth_headers):
        """get_media_buy_delivery response must have 'deliveries' or 'media_buys'."""
        from src.core.schemas import GetMediaBuyDeliveryResponse

        mock_impl.return_value = GetMediaBuyDeliveryResponse(
            media_buy_deliveries=[],
            aggregated_totals={"impressions": 0, "clicks": 0, "spend": 0.0, "media_buy_count": 0},
            currency="USD",
            reporting_period={"start": "2026-03-01T00:00:00Z", "end": "2026-03-31T00:00:00Z", "granularity": "daily"},
        )

        payload = _build_jsonrpc("get_media_buy_delivery", {"media_buy_ids": ["mb-1"]})
        response = client.post("/a2a", json=payload, headers=auth_headers)
        body = response.json()

        if "result" in body:
            data = _extract_artifact_data(body["result"])
            assert "media_buy_deliveries" in data or "deliveries" in data, (
                "get_media_buy_delivery response must have 'media_buy_deliveries' field"
            )

    @patch("src.core.resolved_identity.resolve_identity", return_value=_TEST_IDENTITY)
    @patch("src.a2a_server.adcp_a2a_server.core_update_performance_index_tool")
    def test_update_performance_index_response_shape(self, mock_impl, mock_resolve, client, auth_headers):
        """update_performance_index response must have acknowledgment fields."""
        from src.core.schemas import UpdatePerformanceIndexResponse

        mock_impl.return_value = UpdatePerformanceIndexResponse(
            status="updated",
            detail="Performance index updated for mb-test-1",
        )

        payload = _build_jsonrpc(
            "update_performance_index",
            {"media_buy_id": "mb-test-1", "performance_data": [{"product_id": "p1", "performance_index": 1.2}]},
        )
        response = client.post("/a2a", json=payload, headers=auth_headers)
        body = response.json()

        if "result" in body:
            data = _extract_artifact_data(body["result"])
            assert "media_buy_id" in data or "status" in data, (
                "update_performance_index response must have 'media_buy_id' or 'status'"
            )


# ---------------------------------------------------------------------------
# Stub Handlers (approve_creative, get_media_buy_status, optimize_media_buy)
# ---------------------------------------------------------------------------


class TestA2AStubHandlers:
    """Unimplemented-but-registered skills surface UNSUPPORTED_FEATURE in the task body.

    Table-driven wire assertions across the dispatch registry: a recognized-but-
    unimplemented skill is an application-layer failure, so it must return a
    failed Task carrying a two-layer ``UNSUPPORTED_FEATURE``/``correctable``
    envelope — NOT a JSON-RPC ``UnsupportedOperationError`` (-32004). Reserving
    JSON-RPC for transport faults is the AdCP 3.1.1 "Layer Separation"
    contract; direct calls to a recognized stub must get a structured,
    recoverable AdCP error rather than a transport exception even though the
    unsupported skill is intentionally omitted from the agent card.
    """

    @pytest.mark.parametrize("skill", UNSUPPORTED_SKILLS)
    @patch("src.core.resolved_identity.resolve_identity", return_value=_TEST_IDENTITY)
    def test_unsupported_skill_returns_failed_task_not_transport_error(self, mock_resolve, client, auth_headers, skill):
        """Each unsupported skill returns a failed Task with UNSUPPORTED_FEATURE, not JSON-RPC."""
        payload = _build_jsonrpc(skill, SKILL_METADATA[skill]["params"])
        response = client.post("/a2a", json=payload, headers=auth_headers)
        body = response.json()

        assert "error" not in body, f"'{skill}' must not be a JSON-RPC error: {body.get('error')}"
        assert "result" in body, f"'{skill}' expected a failed-Task result, got: {json.dumps(body)[:400]}"
        data = _extract_artifact_data(body["result"])
        assert data.get("adcp_error", {}).get("code") == "UNSUPPORTED_FEATURE", (
            f"'{skill}' must surface UNSUPPORTED_FEATURE in the task body: {data}"
        )
        assert data.get("adcp_error", {}).get("recovery") == "correctable", f"'{skill}': {data}"

    @pytest.mark.parametrize("skill", IMPLEMENTED_SKILLS)
    @patch("src.core.resolved_identity.resolve_identity", return_value=_TEST_IDENTITY)
    def test_implemented_skill_does_not_return_unsupported_feature(self, mock_resolve, client, auth_headers, skill):
        """A skill classified 'implemented' must NOT surface UNSUPPORTED_FEATURE.

        This is the non-tautological half: a skill mis-routed to an unsupported
        handler would return a UNSUPPORTED_FEATURE failed
        Task and fail here, even without a DB (empty params yield VALIDATION_ERROR or
        a JSON-RPC error, never UNSUPPORTED_FEATURE, for a genuinely-implemented skill).
        """
        payload = _build_jsonrpc(skill, {})
        response = client.post("/a2a", json=payload, headers=auth_headers)
        body = response.json()
        # An implemented skill with empty params yields a result Task (the skill ran and
        # returned VALIDATION_ERROR, never UNSUPPORTED_FEATURE). A JSON-RPC "error" body
        # here would mean the skill never reached its handler — assert the shape rather
        # than silently skipping, so a routing regression can't pass by changing the shape.
        assert "result" in body, f"'{skill}' produced a JSON-RPC error, not a result Task: {body}"
        data = _extract_artifact_data(body["result"])
        assert data.get("adcp_error", {}).get("code") != "UNSUPPORTED_FEATURE", (
            f"'{skill}' is classified implemented but routed to an unsupported handler: {data}"
        )


class TestA2ARegistryContract:
    """Explicit skill metadata ↔ production registry & agent card (non-tautological)."""

    def test_metadata_covers_exactly_the_production_registry(self):
        """SKILL_METADATA must be a bijection with the production registry.

        Both sides are independently authored (the metadata is NOT derived from the
        registry), so a skill added to ``_skill_handler_map`` without a metadata entry
        — or a stale metadata entry — fails here. Combined with the per-skill wire
        assertions (implemented ≠ UNSUPPORTED_FEATURE, unsupported = UNSUPPORTED_FEATURE),
        a mis-classified or mis-routed skill can no longer pass silently.
        """
        registry = set(AdCPRequestHandler()._skill_handler_map())
        assert set(SKILL_METADATA) == registry, (
            f"metadata↔registry drift — missing: {registry - set(SKILL_METADATA)}, "
            f"stale: {set(SKILL_METADATA) - registry}"
        )

    def test_discovery_metadata_matches_production(self):
        """The HAND-AUTHORED ``discovery`` flags must equal production's
        ``DISCOVERY_SKILLS`` frozenset. The auth-boundary tests derive their partitions from the
        oracle, so without this pin they would move in lockstep with production and a skill
        wrongly flipped into (or out of) the no-auth discovery set would still pass. Combined with
        the metadata↔registry bijection above (which forces every skill to carry a ``discovery``
        bool), a production classification change reddens here.
        """
        hand_authored = {s for s, m in SKILL_METADATA.items() if m["discovery"]}
        assert hand_authored == set(_PROD_DISCOVERY_SKILLS), (
            f"discovery classification drift — production-only: {set(_PROD_DISCOVERY_SKILLS) - hand_authored}, "
            f"oracle-only: {hand_authored - set(_PROD_DISCOVERY_SKILLS)}"
        )

    def test_transports_share_the_canonical_auth_optional_policy(self):
        """A2A and MCP must import, not copy, the transport-neutral auth policy."""
        assert _PROD_DISCOVERY_SKILLS is AUTH_OPTIONAL_SKILLS
        assert AUTH_OPTIONAL_TOOLS is AUTH_OPTIONAL_SKILLS


# ---------------------------------------------------------------------------
# All Skills Dispatch
# ---------------------------------------------------------------------------


class TestA2AAllSkillsDispatch:
    """Verify every registered skill (ALL_SKILLS, derived from the production registry) is reachable."""

    @pytest.mark.parametrize("skill", ALL_SKILLS)
    def test_skill_dispatches_not_404(self, client, auth_headers, skill):
        """Every registered skill must be dispatched (not 404 or method-not-found)."""
        payload = _build_jsonrpc(skill, {})
        response = client.post("/a2a", json=payload, headers=auth_headers)
        body = response.json()

        # Skill should be found (not method-not-found error)
        if "error" in body:
            error_msg = body["error"].get("message", "")
            assert "Unknown skill" not in error_msg, f"Skill '{skill}' not found in dispatch map: {error_msg}"

    @pytest.mark.parametrize("skill", ALL_SKILLS)
    def test_all_skills_return_valid_jsonrpc(self, client, auth_headers, skill):
        """Every skill must return valid JSON-RPC (result or error with code+message)."""
        payload = _build_jsonrpc(skill, {})
        response = client.post("/a2a", json=payload, headers=auth_headers)
        body = response.json()

        assert body.get("jsonrpc") == "2.0", f"Skill '{skill}' must return jsonrpc: '2.0'"
        assert "result" in body or "error" in body, f"Skill '{skill}' must return result or error"
        if "error" in body:
            assert "code" in body["error"], f"Error for '{skill}' must have 'code'"
            assert "message" in body["error"], f"Error for '{skill}' must have 'message'"


# ---------------------------------------------------------------------------
# Agent Card Contract
# ---------------------------------------------------------------------------


class TestAgentCardContract:
    """Verify agent card advertises all skills and has required structure."""

    def test_agent_card_advertises_exactly_the_metadata_advertised_set(self, client):
        """The agent card's advertised skills must EXACTLY equal ``ADVERTISED_SKILLS``.

        Exact set equality (not ``⊇``) catches both a dispatchable skill that stops
        being advertised and an arbitrary extra skill advertised by accident,
        because the expected set is the independently-authored
        metadata, not something derived from the card itself.
        """
        response = client.get("/.well-known/agent-card.json")
        card = response.json()
        advertised_skills = {s["name"] for s in card.get("skills", [])}

        assert advertised_skills == ADVERTISED_SKILLS, (
            f"agent card advertised set drifted from SKILL_METADATA — "
            f"unexpected: {sorted(advertised_skills - ADVERTISED_SKILLS)}, "
            f"missing: {sorted(ADVERTISED_SKILLS - advertised_skills)}"
        )

    def test_agent_card_url_no_trailing_slash(self, client):
        """Agent card URL must not have trailing slash (causes redirects)."""
        response = client.get("/.well-known/agent-card.json")
        card = response.json()
        url = card.get("url", "")
        assert not url.endswith("/"), f"Agent card URL has trailing slash: {url}"

    def test_agent_card_has_adcp_extension(self, client):
        """Agent card must include AdCP extension in capabilities."""
        response = client.get("/.well-known/agent-card.json")
        card = response.json()
        extensions = card.get("capabilities", {}).get("extensions", [])
        adcp_uris = [e.get("uri", "") for e in extensions]
        assert any("adcp-extension" in uri for uri in adcp_uris), "Agent card must have AdCP extension in capabilities"


class TestEnvelopeFreeProtocolRaises:
    """The envelope-free JSON-RPC raises stay enumerable, not just described.

    ``_enveloped_invalid_request``'s docstring enumerates every raise in the A2A module
    that deliberately ships WITHOUT the two-layer envelope, because each signals a
    transport-protocol condition with no AdCP wire code. An enumeration in prose drifts
    the moment someone adds a raise — an earlier version named 7 of the 12 and read as
    complete, which is what made a reviewer check by hand.
    """

    # Type -> count, mirroring the docstring. Update BOTH together, deliberately: adding a
    # protocol raise is a decision about whether the buyer gets an AdCP code, not a detail.
    _EXPECTED_ENVELOPE_FREE_RAISES = {
        "InvalidParamsError": 3,
        "TaskNotCancelableError": 3,
        "TaskNotFoundError": 4,
        "UnsupportedOperationError": 3,
    }

    @staticmethod
    def _raise_counts() -> dict[str, int]:
        """Count `raise <A2AProtocolError>(...)` per type in the A2A server module."""
        import ast
        from pathlib import Path

        src = Path(__file__).resolve().parents[2] / "src" / "a2a_server" / "adcp_a2a_server.py"
        counts: dict[str, int] = {}
        for node in ast.walk(ast.parse(src.read_text())):
            if not isinstance(node, ast.Raise) or node.exc is None:
                continue
            exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            name = exc.id if isinstance(exc, ast.Name) else (exc.attr if isinstance(exc, ast.Attribute) else None)
            if name in TestEnvelopeFreeProtocolRaises._EXPECTED_ENVELOPE_FREE_RAISES:
                counts[name] = counts.get(name, 0) + 1
        return counts

    def test_docstring_enumeration_matches_the_code(self):
        assert self._raise_counts() == self._EXPECTED_ENVELOPE_FREE_RAISES, (
            "The envelope-free protocol raises changed. Update BOTH this table and the "
            "enumeration in _enveloped_invalid_request's docstring — and decide whether the "
            "new raise should carry an AdCP envelope instead of being protocol-native."
        )

    def test_detector_actually_counts(self):
        """The count must come from the code, not from the expectation it is compared to."""
        counts = self._raise_counts()
        assert sum(counts.values()) == 13
        assert set(counts) == set(self._EXPECTED_ENVELOPE_FREE_RAISES), (
            "a declared type vanished from the module entirely — the equality above would "
            "still fail, but this states which half moved"
        )


class TestAsyncTaskSkillsThreadTheOuterTaskId:
    """Every skill that can return a NON-terminal task must receive the outer task id.

    The buyer is handed a ``task_*`` id for these skills, and that id is only useful if it
    reaches the durable workflow step: ``tasks/get`` reconciles against it,
    ``tasks/cancel`` refuses without it, and ``resolve_webhook_task_id`` keys the
    completion webhook on it.

    This is a signature-and-set pin rather than a wire test on purpose — the defect it
    guards is a NEW async skill being added to the set (or to the push-notification
    injection, which reads the same set) while the task-id threading is forgotten. That is
    what happened to ``sync_creatives``: it received a push-notification config but no
    task id, so a submitted sync produced a task id with no durable counterpart, and
    cancel then reported CANCELED for a workflow that kept running.
    """

    def test_every_async_task_skill_handler_accepts_the_outer_task_id(self):
        import inspect

        from src.a2a_server.adcp_a2a_server import _ASYNC_TASK_SKILLS, AdCPRequestHandler

        handler_map = AdCPRequestHandler._skill_handler_map(AdCPRequestHandler)
        missing = []
        for skill in sorted(_ASYNC_TASK_SKILLS):
            assert skill in handler_map, f"{skill} is in _ASYNC_TASK_SKILLS but has no handler"
            params = inspect.signature(handler_map[skill]).parameters
            if "a2a_task_id" not in params:
                missing.append(skill)

        assert not missing, (
            f"{missing} can return a non-terminal task but their handlers do not accept "
            "a2a_task_id, so the buyer's task id is never persisted on the workflow step — "
            "tasks/cancel cannot resolve it and the completion webhook keys on step_id."
        )

    def test_the_push_notification_set_and_the_task_id_set_are_the_same_source(self):
        """One definition, not two literals that can drift.

        The push-notification injection and the task-id threading select the same skills
        for the same reason: a skill that can notify asynchronously is one that can be
        polled and canceled. They were two separate literals and only one listed
        ``sync_creatives``.
        """
        from src.a2a_server.adcp_a2a_server import _ASYNC_TASK_SKILLS, _TASK_ID_BEARING_SKILLS

        assert _TASK_ID_BEARING_SKILLS < _ASYNC_TASK_SKILLS, (
            "_TASK_ID_BEARING_SKILLS must be derived from _ASYNC_TASK_SKILLS, not maintained as an independent literal"
        )
        # create_media_buy is threaded on its own branch (it also needs the raw wire
        # payload), so it is the one member handled outside the keyword branch.
        assert _ASYNC_TASK_SKILLS - _TASK_ID_BEARING_SKILLS == {"create_media_buy"}

    def test_the_set_names_every_skill_that_can_return_a_non_terminal_task(self):
        """Membership pin: the three skills whose result can be ``submitted``.

        Derived from what each skill can RETURN, not from what the dispatch happens to
        thread — create_media_buy and update_media_buy can answer with a submitted
        result awaiting approval, and sync_creatives with creatives pending review.
        A fourth async skill added without joining this set gets neither a persisted
        task id nor a push-notification config, which is the drift this pins.
        """
        from src.a2a_server.adcp_a2a_server import _ASYNC_TASK_SKILLS

        assert _ASYNC_TASK_SKILLS == {"create_media_buy", "sync_creatives", "update_media_buy"}


class TestA2ASkillParameterForwarding:
    """The A2A skill handlers must forward the request-level parameters their
    siblings do.

    MCP (``update_media_buy`` / ``create_media_buy``) and REST
    (``PUT /media-buys/{id}`` / ``POST /media-buys``) both plumb
    ``idempotency_key``, ``reporting_webhook`` and ``ext`` through to the shared
    raw function. The A2A handlers silently dropped them, so the same buyer
    payload was non-idempotent, had no reporting webhook and lost its extension
    object on A2A alone — a divergence no schema check can see, because dropping a
    keyword argument is not a type error.

    Driven through the REAL handler with the core tool spied, so removing any
    forwarded keyword reddens these.
    """

    _WEBHOOK = {
        "url": "https://buyer.example/reporting",
        "reporting_frequency": "daily",
        "authentication": {
            "schemes": ["Bearer"],
            "credentials": "reporting-webhook-credential-value",
        },
    }
    _EXT = {"vendor_trace_id": "trace-42"}

    @pytest.mark.asyncio
    async def test_update_skill_forwards_idempotency_reporting_and_ext(self):
        handler = AdCPRequestHandler()
        identity = make_test_a2a_identity()

        with patch("src.a2a_server.adcp_a2a_server.core_update_media_buy_tool") as spy:
            spy.return_value = {"ok": True}
            await handler._handle_update_media_buy_skill(
                {
                    "media_buy_id": "mb_1",
                    "paused": True,
                    "reporting_webhook": self._WEBHOOK,
                    "ext": self._EXT,
                    "idempotency_key": "idempotency-key-update-1",
                },
                identity,
            )

        assert spy.call_count == 1, "the core update tool must be invoked exactly once"
        forwarded = spy.call_args.kwargs
        assert forwarded["idempotency_key"] == "idempotency-key-update-1"
        assert forwarded["reporting_webhook"] == self._WEBHOOK
        assert forwarded["ext"] == self._EXT

    @pytest.mark.asyncio
    async def test_create_skill_forwards_idempotency_reporting_and_ext(self):
        handler = AdCPRequestHandler()
        identity = make_test_a2a_identity()

        with patch("src.a2a_server.adcp_a2a_server.core_create_media_buy_tool") as spy:
            spy.return_value = {"ok": True}
            await handler._handle_create_media_buy_skill(
                {
                    "brand": {"domain": "forwarding.example"},
                    "packages": [{"product_id": "prod_1", "budget": 5000.0, "pricing_option_id": "cpm_usd_fixed"}],
                    "start_time": "2030-01-01T00:00:00Z",
                    "end_time": "2030-01-08T00:00:00Z",
                    "reporting_webhook": self._WEBHOOK,
                    "ext": self._EXT,
                    "idempotency_key": "idempotency-key-create-1",
                },
                identity,
            )

        assert spy.call_count == 1, "the core create tool must be invoked exactly once"
        forwarded = spy.call_args.kwargs
        assert forwarded["idempotency_key"] == "idempotency-key-create-1"
        assert forwarded["reporting_webhook"] == self._WEBHOOK
        assert forwarded["ext"] == self._EXT
