"""Sell-readiness and real approval E2E coverage."""

from __future__ import annotations

import uuid

import pytest

from tests.e2e.adcp_request_builder import (
    build_adcp_media_buy_request,
    get_test_date_range,
    parse_tool_result,
)
from tests.e2e.admin_flow_helpers import (
    approve_workflow_step,
    bootstrap_review_ready_tenant,
    bootstrap_tenant_via_container,
    create_admin_session,
    create_principal,
    get_media_buy_status,
    get_tenant_id_by_subdomain,
    provision_sellable_product,
    resolve_media_buy_workflow_step,
    wait_until,
)
from tests.e2e.utils import make_mcp_client


class TestSellReadiness:
    """Always-on readiness coverage using real admin HTTP flows."""

    @pytest.mark.asyncio
    async def test_sell_readiness_mock_e2e(self, docker_services_e2e, live_server):
        ci_tenant_id = get_tenant_id_by_subdomain(live_server, "ci-test")
        bootstrap_tenant_via_container(
            tenant_id=ci_tenant_id,
            subdomain="ci-test",
            name="CI Test Tenant",
            auth_setup_mode=True,
        )
        suffix = uuid.uuid4().hex[:8]

        # New principal should not see the future product before it exists.
        admin_session = create_admin_session(live_server, ci_tenant_id)
        principal = create_principal(
            admin_session,
            live_server,
            ci_tenant_id,
            name=f"Readiness Precheck Principal {suffix}",
            enable_mock=True,
        )
        future_product_id = f"prod_precreate_{suffix}"
        async with make_mcp_client(live_server, principal["access_token"], tenant="ci-test") as client:
            before_result = await client.call_tool(
                "get_products",
                {"brief": "display advertising", "context": {"e2e": "sell_readiness_before"}},
            )
            before_payload = parse_tool_result(before_result)
            assert future_product_id not in {p["product_id"] for p in before_payload["products"]}

        # Provision a fully sellable product through real admin routes.
        provisioned = await provision_sellable_product(
            live_server,
            ci_tenant_id,
            product_suffix=f"readiness_{suffix}",
        )
        restricted_product_id = provisioned["product_id"]

        # The legacy CI principal must not see the principal-restricted product.
        async with make_mcp_client(live_server, "ci-test-token", tenant="ci-test") as client:
            seeded_result = await client.call_tool(
                "get_products",
                {"brief": "display advertising", "context": {"e2e": "sell_readiness_seeded_principal"}},
            )
            seeded_payload = parse_tool_result(seeded_result)
            assert restricted_product_id not in {p["product_id"] for p in seeded_payload["products"]}

        # The generated principal token must see the newly created sellable offer.
        async with make_mcp_client(live_server, provisioned["access_token"], tenant="ci-test") as client:
            after_result = await client.call_tool(
                "get_products",
                {"brief": "display advertising", "context": {"e2e": "sell_readiness_after"}},
            )
            after_payload = parse_tool_result(after_result)
            product_ids = {p["product_id"] for p in after_payload["products"]}
            assert restricted_product_id in product_ids, (
                f"Expected product {restricted_product_id} in discovery results, got {sorted(product_ids)}"
            )


class TestMediaBuyApproval:
    """Real approval-loop coverage without DB force-approve shortcuts."""

    @pytest.mark.asyncio
    async def test_media_buy_real_approval_e2e(self, docker_services_e2e, live_server):
        suffix = uuid.uuid4().hex[:8]
        setup = await bootstrap_review_ready_tenant(
            live_server,
            tenant_prefix=f"approval_{suffix}",
        )

        async with make_mcp_client(live_server, setup["access_token"], tenant=setup["tenant_subdomain"]) as client:
            products_result = await client.call_tool(
                "get_products",
                {"brief": "display advertising", "context": {"e2e": "real_approval_discovery"}},
            )
            products_payload = parse_tool_result(products_result)
            matching_products = [p for p in products_payload["products"] if p["product_id"] == setup["product_id"]]
            assert matching_products, f"Provisioned product {setup['product_id']} not found in MCP discovery"
            product = matching_products[0]

            start_time, end_time = get_test_date_range(days_from_now=1, duration_days=14)
            create_request = build_adcp_media_buy_request(
                product_ids=[setup["product_id"]],
                total_budget=1500.0,
                start_time=start_time,
                end_time=end_time,
                brand={"domain": f"approval-{suffix}.example.com"},
                pricing_option_id=product["pricing_options"][0]["pricing_option_id"],
                buyer_ref=f"real_approval_{suffix}",
                context={"e2e": "real_approval_create"},
            )

            create_result = await client.call_tool("create_media_buy", create_request)
            create_payload = parse_tool_result(create_result)
            media_buy_id = create_payload.get("media_buy_id")
            assert media_buy_id, f"create_media_buy must return media_buy_id, got {create_payload}"

            tasks_result = await client.call_tool(
                "list_tasks",
                {"object_type": "media_buy", "object_id": media_buy_id},
            )
            tasks_payload = parse_tool_result(tasks_result)
            assert tasks_payload["tasks"], f"Expected workflow task for media buy {media_buy_id}"

            step_id, workflow_id = resolve_media_buy_workflow_step(tasks_payload["tasks"], live_server, media_buy_id)

            admin_session = create_admin_session(live_server, setup["tenant_id"])
            approve_workflow_step(
                admin_session,
                live_server,
                setup["tenant_id"],
                workflow_id=workflow_id,
                step_id=step_id,
            )

            approved_task = await wait_until_async(
                lambda: _fetch_task_status(client, step_id),
                description=f"workflow step {step_id} to be approved",
            )
            assert approved_task["status"] == "approved", approved_task

            final_status = wait_until(
                lambda: get_media_buy_status(live_server, media_buy_id),
                timeout_s=30,
                interval_s=1,
                description=f"media buy {media_buy_id} to leave pending approval",
            )
            assert final_status in {"scheduled", "pending_creatives", "active", "approved"}, final_status

            delivery_result = await client.call_tool(
                "get_media_buy_delivery",
                {"media_buy_ids": [media_buy_id]},
            )
            delivery_payload = parse_tool_result(delivery_result)
            assert "deliveries" in delivery_payload or "media_buy_deliveries" in delivery_payload


async def _fetch_task_status(client, step_id: str) -> dict | None:
    result = await client.call_tool("get_task", {"task_id": step_id})
    payload = parse_tool_result(result)
    if payload.get("status") == "approved":
        return payload
    return None


async def wait_until_async(
    predicate,
    *,
    timeout_s: float = 30.0,
    interval_s: float = 1.0,
    description: str,
):
    """Async polling helper local to this module."""
    import asyncio
    import time

    deadline = time.time() + timeout_s
    last_value = None
    while time.time() < deadline:
        last_value = await predicate()
        if last_value:
            return last_value
        await asyncio.sleep(interval_s)
    raise AssertionError(f"Timed out waiting for {description}; last value={last_value!r}")
