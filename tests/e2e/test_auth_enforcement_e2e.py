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

import uuid

import pytest
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport

from tests.e2e.adcp_request_builder import (
    build_adcp_media_buy_request,
    build_creative,
    build_sync_creatives_request,
    get_test_date_range,
    parse_tool_result,
)
from tests.e2e.admin_flow_helpers import bootstrap_review_ready_tenant
from tests.e2e.utils import make_mcp_client

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


def _is_not_found_or_denied(exc: Exception) -> bool:
    """Return True if the exception message indicates not-found or cross-tenant denial."""
    text = str(exc).lower()
    return any(
        kw in text
        for kw in ("not found", "does not exist", "no task", "404", "401", "403", "unauthorized", "forbidden")
    )


class TestTenantIsolation:
    """Verify that tenant-scoped objects are not accessible from a different tenant's token.

    Each test bootstraps two independent tenants (A and B), creates data under tenant A,
    then asserts that tenant B's token cannot see or mutate tenant A's data.
    """

    @pytest.mark.asyncio
    async def test_task_not_accessible_from_wrong_tenant(self, docker_services_e2e, live_server, test_auth_token):
        """get_task with tenant B's token must not return tenant A's workflow step."""
        setup_a = await bootstrap_review_ready_tenant(live_server, tenant_prefix="iso_task_a")
        setup_b = await bootstrap_review_ready_tenant(live_server, tenant_prefix="iso_task_b")

        # Create a media buy in tenant A → produces a workflow task
        async with make_mcp_client(
            live_server, setup_a["access_token"], tenant=setup_a["tenant_subdomain"]
        ) as client_a:
            products = parse_tool_result(
                await client_a.call_tool(
                    "get_products", {"brief": "display advertising", "context": {"e2e": "iso_task_a"}}
                )
            )
            product_a = products["products"][0]
            start_time, end_time = get_test_date_range(days_from_now=1, duration_days=14)
            create_data = parse_tool_result(
                await client_a.call_tool(
                    "create_media_buy",
                    build_adcp_media_buy_request(
                        product_ids=[product_a["product_id"]],
                        total_budget=500.0,
                        start_time=start_time,
                        end_time=end_time,
                        brand={"domain": "iso-tenant-a.example.com"},
                        pricing_option_id=product_a["pricing_options"][0]["pricing_option_id"],
                        context={"e2e": "iso_task_a"},
                    ),
                )
            )
            media_buy_id_a = create_data["media_buy_id"]

            tasks_a = parse_tool_result(
                await client_a.call_tool("list_tasks", {"object_type": "media_buy", "object_id": media_buy_id_a})
            )
            assert tasks_a["tasks"], f"Tenant A must have at least one workflow task for {media_buy_id_a}"
            task_id_a = tasks_a["tasks"][0]["task_id"]

        # Tenant B must not see tenant A's task in list_tasks
        async with make_mcp_client(
            live_server, setup_b["access_token"], tenant=setup_b["tenant_subdomain"]
        ) as client_b:
            tasks_b = parse_tool_result(await client_b.call_tool("list_tasks", {}))
            ids_visible_to_b = {t["task_id"] for t in tasks_b.get("tasks", [])}
            assert task_id_a not in ids_visible_to_b, (
                f"Tenant A's task {task_id_a} leaked into tenant B's list_tasks result"
            )

            # get_task with tenant A's task_id must fail for tenant B
            try:
                result = await client_b.call_tool("get_task", {"task_id": task_id_a})
                # If no exception, the structured response must indicate not-found
                data = result.structured_content if hasattr(result, "structured_content") else {}
                assert _looks_like_auth_denial(data) or "not found" in _stringify_auth_payload(data).lower(), (
                    f"Tenant B was able to read tenant A's task {task_id_a}: {data}"
                )
            except Exception as exc:
                assert not _is_non_auth_transport_error(exc), (
                    f"get_task failed with a transport error instead of a tenant-isolation error: {exc!r}"
                )

    @pytest.mark.asyncio
    async def test_creatives_scoped_to_tenant(self, docker_services_e2e, live_server, test_auth_token):
        """list_creatives via tenant B must not expose creatives synced under tenant A."""
        setup_a = await bootstrap_review_ready_tenant(live_server, tenant_prefix="iso_cr_a")
        setup_b = await bootstrap_review_ready_tenant(live_server, tenant_prefix="iso_cr_b")

        # Sync an approved creative under tenant A
        async with make_mcp_client(
            live_server, setup_a["access_token"], tenant=setup_a["tenant_subdomain"]
        ) as client_a:
            products = parse_tool_result(
                await client_a.call_tool(
                    "get_products", {"brief": "display advertising", "context": {"e2e": "iso_cr_a"}}
                )
            )
            format_id = products["products"][0]["format_ids"][0]
            creative_id_a = f"cr_iso_a_{uuid.uuid4().hex[:8]}"
            await client_a.call_tool(
                "sync_creatives",
                build_sync_creatives_request(
                    creatives=[
                        build_creative(
                            creative_id=creative_id_a,
                            format_id=format_id,
                            name="Tenant A Isolation Creative",
                            asset_url="https://example.com/iso-a.jpg",
                            status="approved",
                        )
                    ]
                ),
            )

        # Tenant B's list_creatives must not include tenant A's creative
        async with make_mcp_client(
            live_server, setup_b["access_token"], tenant=setup_b["tenant_subdomain"]
        ) as client_b:
            list_b = parse_tool_result(await client_b.call_tool("list_creatives", {}))
            creative_ids_b = {c["creative_id"] for c in list_b.get("creatives", [])}
            assert creative_id_a not in creative_ids_b, (
                f"Tenant A's creative {creative_id_a} leaked into tenant B's list_creatives result"
            )

    @pytest.mark.asyncio
    async def test_complete_task_blocked_for_wrong_tenant(self, docker_services_e2e, live_server, test_auth_token):
        """complete_task on a task belonging to tenant A must fail for tenant B's token."""
        setup_a = await bootstrap_review_ready_tenant(live_server, tenant_prefix="iso_ct_a")
        setup_b = await bootstrap_review_ready_tenant(live_server, tenant_prefix="iso_ct_b")

        # Create a pending workflow task under tenant A
        async with make_mcp_client(
            live_server, setup_a["access_token"], tenant=setup_a["tenant_subdomain"]
        ) as client_a:
            products = parse_tool_result(
                await client_a.call_tool(
                    "get_products", {"brief": "display advertising", "context": {"e2e": "iso_ct_a"}}
                )
            )
            product_a = products["products"][0]
            start_time, end_time = get_test_date_range(days_from_now=1, duration_days=14)
            create_data = parse_tool_result(
                await client_a.call_tool(
                    "create_media_buy",
                    build_adcp_media_buy_request(
                        product_ids=[product_a["product_id"]],
                        total_budget=500.0,
                        start_time=start_time,
                        end_time=end_time,
                        brand={"domain": "iso-ct-a.example.com"},
                        pricing_option_id=product_a["pricing_options"][0]["pricing_option_id"],
                        context={"e2e": "iso_ct_a"},
                    ),
                )
            )
            tasks_a = parse_tool_result(
                await client_a.call_tool(
                    "list_tasks",
                    {"object_type": "media_buy", "object_id": create_data["media_buy_id"]},
                )
            )
            assert tasks_a["tasks"], "Tenant A must have a pending approval task"
            task_id_a = tasks_a["tasks"][0]["task_id"]

        # Tenant B must not be able to complete tenant A's task
        async with make_mcp_client(
            live_server, setup_b["access_token"], tenant=setup_b["tenant_subdomain"]
        ) as client_b:
            try:
                result = await client_b.call_tool(
                    "complete_task",
                    {
                        "task_id": task_id_a,
                        "status": "completed",
                        "response_data": {"decision": "approved"},
                    },
                )
                data = result.structured_content if hasattr(result, "structured_content") else {}
                assert _looks_like_auth_denial(data) or "not found" in _stringify_auth_payload(data).lower(), (
                    f"Tenant B was able to complete tenant A's task {task_id_a}: {data}"
                )
            except Exception as exc:
                assert not _is_non_auth_transport_error(exc), (
                    f"complete_task failed with a transport error instead of tenant isolation: {exc!r}"
                )
                # Expected: not found or auth error — the task doesn't exist in tenant B's scope
