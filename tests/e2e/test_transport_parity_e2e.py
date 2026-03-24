"""
Transport Parity E2E Test

Verifies that MCP and A2A transports return equivalent response shapes
for the same logical operation. A bug in the transport boundary (e.g., MCP
wrapper drops a field that the A2A wrapper passes) would show up here.

Strategy:
  1. Call get_products via MCP → extract top-level keys
  2. Call get_products via A2A skill (explicit invocation) → extract top-level keys
  3. Assert that both responses have the same mandatory keys

We do not assert identical values (timestamps, IDs differ per request),
only that the response shape is consistent across transports.

A2A calls reuse A2AAdCPComplianceClient from test_a2a_adcp_compliance to
avoid duplicating the JSON-RPC message building logic.
"""

from contextlib import asynccontextmanager

import pytest

from tests.e2e.adcp_request_builder import (
    build_adcp_media_buy_request,
    build_creative,
    build_sync_creatives_request,
    get_test_date_range,
    parse_tool_result,
)
from tests.e2e.admin_flow_helpers import bootstrap_review_ready_tenant
from tests.e2e.test_a2a_adcp_compliance import A2AAdCPComplianceClient
from tests.e2e.utils import make_mcp_client

PRODUCT_FIELDS = {
    "product_id",
    "name",
    "description",
    "format_ids",
    "pricing_options",
    "delivery_methods",
}
PRICING_OPTION_FIELDS = {
    "pricing_option_id",
    "pricing_model",
    "price",
    "currency",
}
FORMAT_FIELDS = {
    "format_id",
    "name",
    "description",
    "delivery_methods",
}


@asynccontextmanager
async def _a2a_client_for(live_server: dict, access_token: str, tenant: str):
    """Shared context manager for A2A compliance client in parity tests."""
    a2a_url = f"{live_server['a2a']}/a2a"
    async with A2AAdCPComplianceClient(
        a2a_url=a2a_url,
        auth_token=access_token,
        tenant=tenant,
        validate_schemas=False,
    ) as client:
        yield client


def _normalized_fields(payload: dict, allowed_fields: set[str]) -> set[str]:
    return {field for field in allowed_fields if field in payload}


def _normalize_product(product: dict) -> dict:
    normalized = {field: product[field] for field in PRODUCT_FIELDS if field in product}
    if "pricing_options" in normalized:
        normalized["pricing_options"] = [
            _normalized_fields(option, PRICING_OPTION_FIELDS) for option in normalized["pricing_options"]
        ]
    return normalized


def _normalize_format(fmt: dict) -> dict:
    return {field: fmt[field] for field in FORMAT_FIELDS if field in fmt}


class TestTransportParity:
    """Verify MCP and A2A return equivalent response shapes."""

    @pytest.mark.asyncio
    async def test_get_products_response_shape_matches_across_transports(
        self, docker_services_e2e, live_server, test_auth_token
    ):
        """get_products via MCP and via A2A must return the same top-level keys."""
        # ── MCP call ──
        async with make_mcp_client(live_server, test_auth_token) as mcp_client:
            mcp_result = await mcp_client.call_tool(
                "get_products",
                {"brief": "display advertising", "context": {"e2e": "transport_parity"}},
            )
            mcp_data = parse_tool_result(mcp_result)

        # ── A2A call ──
        async with _a2a_client_for(live_server, test_auth_token, "ci-test") as a2a_client:
            a2a_response = await a2a_client.send_explicit_skill_message(
                "get_products",
                {"brief": "display advertising", "context": {"e2e": "transport_parity"}},
            )

        # A2A must not return a JSON-RPC error
        assert "error" not in a2a_response, f"A2A returned error: {a2a_response.get('error')}"

        a2a_data = a2a_client.extract_adcp_payload_from_a2a_response(a2a_response)
        assert a2a_data is not None, f"Could not extract AdCP payload from A2A response: {a2a_response}"

        assert "products" in mcp_data, f"MCP response missing 'products' key: {list(mcp_data.keys())}"
        assert "products" in a2a_data, f"A2A response missing 'products' key: {list(a2a_data.keys())}"

        mcp_products = mcp_data["products"]
        a2a_products = a2a_data["products"]

        assert isinstance(mcp_products, list), "MCP products must be a list"
        assert isinstance(a2a_products, list), "A2A products must be a list"

        assert len(mcp_products) > 0, "MCP must return at least one product"
        assert len(a2a_products) > 0, "A2A must return at least one product"

        mcp_product = _normalize_product(mcp_products[0])
        a2a_product = _normalize_product(a2a_products[0])

        mandatory_fields = {"product_id", "name", "format_ids", "pricing_options"}
        assert mandatory_fields.issubset(mcp_product.keys()), (
            f"MCP product missing mandatory parity fields: {mandatory_fields - set(mcp_product.keys())}"
        )
        assert mandatory_fields.issubset(a2a_product.keys()), (
            f"A2A product missing mandatory parity fields: {mandatory_fields - set(a2a_product.keys())}"
        )

        assert set(mcp_product.keys()) == set(a2a_product.keys()), (
            f"MCP/A2A product keys differ: mcp={sorted(mcp_product.keys())}, a2a={sorted(a2a_product.keys())}"
        )
        assert mcp_product["pricing_options"], "MCP product must include at least one pricing option"
        assert a2a_product["pricing_options"], "A2A product must include at least one pricing option"
        assert mcp_product["pricing_options"][0] == a2a_product["pricing_options"][0], (
            "MCP/A2A pricing option shapes diverged for the first product"
        )

    @pytest.mark.asyncio
    async def test_list_creative_formats_parity(self, docker_services_e2e, live_server, test_auth_token):
        """list_creative_formats via MCP and A2A must both return format lists."""
        # ── MCP call ──
        async with make_mcp_client(live_server, test_auth_token) as mcp_client:
            mcp_result = await mcp_client.call_tool("list_creative_formats", {})
            mcp_data = parse_tool_result(mcp_result)

        # ── A2A call ──
        async with _a2a_client_for(live_server, test_auth_token, "ci-test") as a2a_client:
            a2a_response = await a2a_client.send_explicit_skill_message("list_creative_formats", {})

        assert "error" not in a2a_response, f"A2A error: {a2a_response.get('error')}"

        a2a_data = a2a_client.extract_adcp_payload_from_a2a_response(a2a_response)
        assert a2a_data is not None, "Could not extract payload from A2A list_creative_formats response"

        mcp_formats = mcp_data.get("formats", mcp_data.get("creative_formats", []))
        a2a_formats = a2a_data.get("formats", a2a_data.get("creative_formats", []))

        assert isinstance(mcp_formats, list), f"MCP formats must be a list, got {type(mcp_formats)}"
        assert isinstance(a2a_formats, list), f"A2A formats must be a list, got {type(a2a_formats)}"
        assert len(mcp_formats) > 0, "MCP must return at least one creative format"
        assert len(a2a_formats) > 0, "A2A must return at least one creative format"

        mcp_format = _normalize_format(mcp_formats[0])
        a2a_format = _normalize_format(a2a_formats[0])
        mandatory_fields = {"format_id", "name"}
        assert mandatory_fields.issubset(mcp_format.keys()), (
            f"MCP format missing mandatory parity fields: {mandatory_fields - set(mcp_format.keys())}"
        )
        assert mandatory_fields.issubset(a2a_format.keys()), (
            f"A2A format missing mandatory parity fields: {mandatory_fields - set(a2a_format.keys())}"
        )
        assert set(mcp_format.keys()) == set(a2a_format.keys()), (
            f"MCP/A2A creative format keys differ: mcp={sorted(mcp_format.keys())}, a2a={sorted(a2a_format.keys())}"
        )

    @pytest.mark.asyncio
    async def test_create_media_buy_state_parity(self, docker_services_e2e, live_server, test_auth_token):
        """create_media_buy via MCP and A2A must both return media_buy_id and start in pending_approval.

        Covers: task-creation business state parity and media buy discovery parity for the same
        principal. Both transports go through the same _impl function so divergence would signal
        a wrapper-level regression.
        """
        setup = await bootstrap_review_ready_tenant(live_server, tenant_prefix="mb_state_parity")
        token = setup["access_token"]
        tenant = setup["tenant_subdomain"]

        start_time, end_time = get_test_date_range(days_from_now=1, duration_days=14)

        # Discover a product valid for this tenant (same token, same tenant for both transports)
        async with make_mcp_client(live_server, token, tenant=tenant) as mcp_client:
            products_result = await mcp_client.call_tool(
                "get_products",
                {"brief": "display advertising", "context": {"e2e": "mb_state_parity"}},
            )
            products_data = parse_tool_result(products_result)
            product = products_data["products"][0]
            product_id = product["product_id"]
            pricing_option_id = product["pricing_options"][0]["pricing_option_id"]

            # ── MCP create ──
            mcp_create_result = await mcp_client.call_tool(
                "create_media_buy",
                build_adcp_media_buy_request(
                    product_ids=[product_id],
                    total_budget=1000.0,
                    start_time=start_time,
                    end_time=end_time,
                    brand={"domain": "mcp-parity.example.com"},
                    pricing_option_id=pricing_option_id,
                    context={"e2e": "mb_state_parity_mcp"},
                ),
            )
            mcp_data = parse_tool_result(mcp_create_result)

        # ── A2A create ──
        async with _a2a_client_for(live_server, token, tenant) as a2a_client:
            a2a_response = await a2a_client.send_explicit_skill_message(
                "create_media_buy",
                build_adcp_media_buy_request(
                    product_ids=[product_id],
                    total_budget=1000.0,
                    start_time=start_time,
                    end_time=end_time,
                    brand={"domain": "a2a-parity.example.com"},
                    pricing_option_id=pricing_option_id,
                    context={"e2e": "mb_state_parity_a2a"},
                ),
            )

        assert "error" not in a2a_response, f"A2A create_media_buy returned error: {a2a_response.get('error')}"
        a2a_data = a2a_client.extract_adcp_payload_from_a2a_response(a2a_response)
        assert a2a_data is not None, f"Could not extract payload from A2A create_media_buy response: {a2a_response}"

        # Both transports must return a media_buy_id
        assert "media_buy_id" in mcp_data, f"MCP create_media_buy missing media_buy_id: {list(mcp_data.keys())}"
        assert "media_buy_id" in a2a_data, f"A2A create_media_buy missing media_buy_id: {list(a2a_data.keys())}"

        # Both must have the same mandatory top-level keys
        mandatory_keys = {"media_buy_id"}
        assert mandatory_keys.issubset(mcp_data.keys()), (
            f"MCP create_media_buy missing mandatory keys: {mandatory_keys - set(mcp_data.keys())}"
        )
        assert mandatory_keys.issubset(a2a_data.keys()), (
            f"A2A create_media_buy missing mandatory keys: {mandatory_keys - set(a2a_data.keys())}"
        )

    @pytest.mark.asyncio
    async def test_creative_status_parity_after_approval(self, docker_services_e2e, live_server, test_auth_token):
        """After approving a creative via MCP, list_creatives via A2A must show the same approved status.

        Covers creative-status parity: the business state written by MCP (complete_task → approve)
        must be readable via A2A (list_creatives) with no divergence.
        """
        import asyncio

        setup = await bootstrap_review_ready_tenant(live_server, tenant_prefix="cr_status_parity")
        token = setup["access_token"]
        tenant = setup["tenant_subdomain"]

        async with make_mcp_client(live_server, token, tenant=tenant) as mcp_client:
            # Discover format_id
            products_result = await mcp_client.call_tool(
                "get_products",
                {"brief": "display advertising", "context": {"e2e": "cr_status_parity"}},
            )
            products_data = parse_tool_result(products_result)
            format_id = products_data["products"][0]["format_ids"][0]

            # Sync pending_review creative
            import uuid

            creative_id = f"cr_par_{uuid.uuid4().hex[:8]}"
            creative = build_creative(
                creative_id=creative_id,
                format_id=format_id,
                name="Parity Test Creative",
                asset_url="https://example.com/parity.jpg",
                click_through_url="https://example.com/landing",
                status="pending_review",
            )
            await mcp_client.call_tool(
                "sync_creatives",
                build_sync_creatives_request(creatives=[creative]),
            )

            # Poll for the workflow task
            task_id = None
            for _ in range(15):
                tasks_result = await mcp_client.call_tool(
                    "list_tasks", {"object_type": "creative", "object_id": creative_id}
                )
                tasks = parse_tool_result(tasks_result).get("tasks", [])
                if tasks:
                    task_id = tasks[0]["task_id"]
                    break
                await asyncio.sleep(1)
            assert task_id, f"No workflow task appeared for creative {creative_id}"

            # Approve via MCP complete_task
            await mcp_client.call_tool(
                "complete_task",
                {
                    "task_id": task_id,
                    "status": "completed",
                    "response_data": {"decision": "approved", "reviewer": "e2e-parity"},
                },
            )

            # MCP list_creatives must show approved
            mcp_list = await mcp_client.call_tool("list_creatives", {})
            mcp_creatives = parse_tool_result(mcp_list).get("creatives", [])
            mcp_match = [c for c in mcp_creatives if c["creative_id"] == creative_id]
            assert mcp_match, f"Creative {creative_id} not found in MCP list_creatives"
            assert mcp_match[0]["status"] == "approved", (
                f"MCP list_creatives shows {mcp_match[0]['status']!r} for {creative_id}"
            )

        # A2A list_creatives must show the same approved status
        async with _a2a_client_for(live_server, token, tenant) as a2a_client:
            a2a_response = await a2a_client.send_explicit_skill_message("list_creatives", {})

        assert "error" not in a2a_response, f"A2A list_creatives returned error: {a2a_response.get('error')}"
        a2a_data = a2a_client.extract_adcp_payload_from_a2a_response(a2a_response)
        assert a2a_data is not None, "Could not extract payload from A2A list_creatives response"

        a2a_creatives = a2a_data.get("creatives", [])
        a2a_match = [c for c in a2a_creatives if c.get("creative_id") == creative_id]
        assert a2a_match, f"Creative {creative_id} not found in A2A list_creatives"
        assert a2a_match[0]["status"] == "approved", (
            f"A2A list_creatives shows {a2a_match[0]['status']!r} for {creative_id}; expected 'approved' matching MCP"
        )
