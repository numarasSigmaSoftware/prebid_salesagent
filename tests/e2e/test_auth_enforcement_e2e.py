"""
Authentication Enforcement E2E Test

Verifies that all MCP tools reject requests with an invalid or missing token.
Tests a representative subset of tools to cover:
- Read-only tools (get_products, list_creatives, get_media_buy_delivery)
- Write tools (create_media_buy, sync_creatives, update_media_buy)
- Workflow tools (list_tasks, complete_task)

Each call uses a clearly invalid token and asserts that the server returns
an error (MCP error or a structured response indicating authentication failure).
"""

import pytest
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport

INVALID_TOKEN = "this-token-is-definitely-invalid-00000000"
AUTH_ERROR_KEYWORDS = (
    "401",
    "403",
    "auth",
    "unauthorized",
    "forbidden",
    "invalid token",
    "authentication",
    "authorization",
    "denied",
)


def _stringify_auth_payload(value) -> str:
    if isinstance(value, dict):
        return " ".join(_stringify_auth_payload(v) for v in value.values())
    if isinstance(value, list | tuple):
        return " ".join(_stringify_auth_payload(v) for v in value)
    return str(value)


def _looks_like_auth_denial(value) -> bool:
    text = _stringify_auth_payload(value).lower()
    return any(keyword in text for keyword in AUTH_ERROR_KEYWORDS)


def _is_non_auth_transport_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "timed out",
            "timeout",
            "connection refused",
            "connection error",
            "connection reset",
            "network",
            "dns",
            "name or service not known",
            "no route to host",
            "502",
            "503",
            "504",
            "internal server error",
        )
    )


async def _call_tool_expect_auth_error(mcp_url: str, tool_name: str, params: dict) -> None:
    """
    Call an MCP tool with an invalid token and assert that it is rejected for auth reasons.

    Fail closed: transport issues or ambiguous errors do not count as auth enforcement.
    """
    headers = {
        "x-adcp-auth": INVALID_TOKEN,
        "x-adcp-tenant": "ci-test",
    }
    transport = StreamableHttpTransport(url=mcp_url, headers=headers)

    try:
        async with Client(transport=transport) as client:
            result = await client.call_tool(tool_name, params)
            if hasattr(result, "structured_content") and result.structured_content:
                content = result.structured_content
                assert _looks_like_auth_denial(content), (
                    f"Tool '{tool_name}' returned structured content for an invalid token, "
                    f"but it did not look like an auth denial: {content}"
                )
                return
    except Exception as exc:
        assert not _is_non_auth_transport_error(exc), (
            f"Tool '{tool_name}' failed due to a transport/server issue instead of an auth rejection: {exc!r}"
        )
        assert _looks_like_auth_denial(exc), (
            f"Tool '{tool_name}' rejected the request, but the error did not clearly indicate auth denial: {exc!r}"
        )
        return

    pytest.fail(
        f"Tool '{tool_name}' must reject requests with an invalid token, "
        f"but the call appeared to succeed without an auth error"
    )


class TestAuthEnforcement:
    """Verify that MCP tools reject unauthenticated / invalid-token requests."""

    @pytest.mark.asyncio
    async def test_get_products_rejects_invalid_token(self, docker_services_e2e, live_server):
        """get_products must reject an invalid auth token."""
        await _call_tool_expect_auth_error(
            f"{live_server['mcp']}/mcp/",
            "get_products",
            {"brief": "display advertising"},
        )

    @pytest.mark.asyncio
    async def test_create_media_buy_rejects_invalid_token(self, docker_services_e2e, live_server):
        """create_media_buy must reject an invalid auth token."""
        await _call_tool_expect_auth_error(
            f"{live_server['mcp']}/mcp/",
            "create_media_buy",
            {
                "buyer_ref": "invalid_token_test",
                "brand": {"domain": "test.com"},
                "packages": [
                    {
                        "buyer_ref": "pkg_invalid",
                        "product_id": "some_product",
                        "budget": 1000.0,
                        "pricing_option_id": "default",
                    }
                ],
                "start_time": "2026-01-01T00:00:00Z",
                "end_time": "2026-02-01T00:00:00Z",
            },
        )

    @pytest.mark.asyncio
    async def test_sync_creatives_rejects_invalid_token(self, docker_services_e2e, live_server):
        """sync_creatives must reject an invalid auth token."""
        await _call_tool_expect_auth_error(
            f"{live_server['mcp']}/mcp/",
            "sync_creatives",
            {
                "creatives": [
                    {
                        "creative_id": "creative_invalid_auth",
                        "format_id": "some_format",
                        "name": "Test",
                        "content_uri": "https://example.com/test.jpg",
                        "assets": {"primary": {"asset_type": "image", "url": "https://example.com/test.jpg"}},
                        "status": "processing",
                    }
                ],
                "dry_run": False,
                "validation_mode": "strict",
                "delete_missing": False,
            },
        )

    @pytest.mark.asyncio
    async def test_update_media_buy_rejects_invalid_token(self, docker_services_e2e, live_server):
        """update_media_buy must reject an invalid auth token."""
        await _call_tool_expect_auth_error(
            f"{live_server['mcp']}/mcp/",
            "update_media_buy",
            {
                "media_buy_id": "mb_nonexistent",
                "active": False,
            },
        )

    @pytest.mark.asyncio
    async def test_list_tasks_rejects_invalid_token(self, docker_services_e2e, live_server):
        """list_tasks must reject an invalid auth token."""
        await _call_tool_expect_auth_error(
            f"{live_server['mcp']}/mcp/",
            "list_tasks",
            {},
        )

    @pytest.mark.asyncio
    async def test_get_media_buy_delivery_rejects_invalid_token(self, docker_services_e2e, live_server):
        """get_media_buy_delivery must reject an invalid auth token."""
        await _call_tool_expect_auth_error(
            f"{live_server['mcp']}/mcp/",
            "get_media_buy_delivery",
            {"media_buy_ids": ["mb_nonexistent"]},
        )

    @pytest.mark.asyncio
    async def test_list_creatives_rejects_invalid_token(self, docker_services_e2e, live_server):
        """list_creatives must reject an invalid auth token."""
        await _call_tool_expect_auth_error(
            f"{live_server['mcp']}/mcp/",
            "list_creatives",
            {},
        )

    @pytest.mark.asyncio
    async def test_update_performance_index_rejects_invalid_token(self, docker_services_e2e, live_server):
        """update_performance_index must reject an invalid auth token."""
        await _call_tool_expect_auth_error(
            f"{live_server['mcp']}/mcp/",
            "update_performance_index",
            {
                "media_buy_id": "mb_nonexistent",
                "performance_data": [
                    {
                        "product_id": "prod_test",
                        "performance_index": 0.8,
                    }
                ],
            },
        )

    @pytest.mark.asyncio
    async def test_complete_task_rejects_invalid_token(self, docker_services_e2e, live_server):
        """complete_task must reject an invalid auth token."""
        await _call_tool_expect_auth_error(
            f"{live_server['mcp']}/mcp/",
            "complete_task",
            {
                "task_id": "step_nonexistent",
                "status": "completed",
            },
        )
