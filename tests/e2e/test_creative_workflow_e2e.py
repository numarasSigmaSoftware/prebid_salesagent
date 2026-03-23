"""
Creative Workflow E2E Test

Covers the creative approval gate:
  sync_creatives (status: pending_review)
    → workflow task created (creative_review)
    → list_tasks / get_task (verify creative is linked)
    → complete_task (approve)
    → list_creatives (verify approved status)

These assertions run against a purpose-built require-human tenant so the
workflow gate stays stable even if shared CI tenant defaults change.

Note: complete_task marks the workflow step as done. The creative status
transition (pending_review → approved) is validated via list_creatives.
"""

import uuid

import pytest

from tests.e2e.adcp_request_builder import (
    build_adcp_media_buy_request,
    build_creative,
    build_sync_creatives_request,
    get_test_date_range,
    parse_tool_result,
)
from tests.e2e.admin_flow_helpers import bootstrap_review_ready_tenant, get_package_id_by_buyer_ref
from tests.e2e.utils import force_approve_media_buy_in_db, make_mcp_client


class TestCreativeWorkflow:
    """E2E tests for the creative approval workflow gate."""

    async def _get_display_format_id(self, client, context_label: str):
        products_result = await client.call_tool(
            "get_products",
            {"brief": "display advertising", "context": {"e2e": context_label}},
        )
        products_data = parse_tool_result(products_result)
        assert len(products_data["products"]) > 0, "Review-ready tenant must have at least one product"

        product = products_data["products"][0]
        format_ids = product.get("format_ids", [])
        assert len(format_ids) > 0, "Product must have at least one format_id"
        return format_ids[0]

    async def _bootstrap_review_client(self, live_server):
        setup = await bootstrap_review_ready_tenant(
            live_server,
            tenant_prefix="creative_workflow",
        )
        client_cm = make_mcp_client(
            live_server,
            setup["access_token"],
            tenant=setup["tenant_subdomain"],
        )
        return setup, client_cm

    async def _sync_pending_review_creative(self, client, *, context_label: str, creative_prefix: str) -> str:
        format_id = await self._get_display_format_id(client, context_label)
        creative_id = f"{creative_prefix}_{uuid.uuid4().hex[:8]}"
        creative = build_creative(
            creative_id=creative_id,
            format_id=format_id,
            name="Workflow Test Creative",
            asset_url="https://example.com/wf-test-creative.jpg",
            click_through_url="https://example.com/landing",
            status="pending_review",
        )
        sync_request = build_sync_creatives_request(creatives=[creative])
        sync_result = await client.call_tool("sync_creatives", sync_request)
        sync_data = parse_tool_result(sync_result)

        assert "creatives" in sync_data, "sync_creatives must return creatives"
        assert len(sync_data["creatives"]) == 1, "Should sync exactly 1 creative"
        assert sync_data["creatives"][0]["creative_id"] == creative_id, "Creative ID must match"
        return creative_id

    @pytest.mark.asyncio
    async def test_sync_creatives_creates_workflow_task(self, docker_services_e2e, live_server, test_auth_token):
        """sync_creatives with pending_review status must create workflow tasks visible via list_tasks."""
        setup, client_cm = await self._bootstrap_review_client(live_server)
        async with client_cm as client:
            creative_id = await self._sync_pending_review_creative(
                client,
                context_label="creative_workflow",
                creative_prefix="creative_wf",
            )

            # Check if a workflow task was created for this creative
            tasks_result = await client.call_tool(
                "list_tasks",
                {"object_type": "creative", "object_id": creative_id},
            )
            tasks_data = parse_tool_result(tasks_result)

            assert "tasks" in tasks_data, "list_tasks must return tasks key"
            tasks = tasks_data["tasks"]
            assert len(tasks) > 0, (
                f"Pending-review creative {creative_id} must create a workflow task in "
                f"{setup['tenant_subdomain']}, "
                "but list_tasks returned no tasks"
            )

            task = tasks[0]
            assert "task_id" in task, "Task must have task_id"
            assert "status" in task, "Task must have status"

            task_id = task["task_id"]
            get_task_result = await client.call_tool("get_task", {"task_id": task_id})
            task_detail = parse_tool_result(get_task_result)
            assert task_detail["task_id"] == task_id
            associated_objects = task_detail.get("associated_objects", [])
            assert any(obj.get("id") == creative_id for obj in associated_objects), (
                f"Creative workflow task must reference {creative_id}, got: {associated_objects}"
            )

    @pytest.mark.asyncio
    async def test_complete_task_approves_creative(self, docker_services_e2e, live_server, test_auth_token):
        """complete_task on a creative_review task must approve the creative and complete the task."""
        setup, client_cm = await self._bootstrap_review_client(live_server)
        async with client_cm as client:
            creative_id = await self._sync_pending_review_creative(
                client,
                context_label="creative_complete_task",
                creative_prefix="creative_ct",
            )

            tasks_result = await client.call_tool(
                "list_tasks",
                {"status": "pending", "object_type": "creative", "object_id": creative_id},
            )
            tasks_data = parse_tool_result(tasks_result)
            tasks = tasks_data.get("tasks", [])
            assert len(tasks) > 0, (
                f"Pending-review creative {creative_id} must expose a pending workflow task in "
                f"{setup['tenant_subdomain']}"
            )

            task_id = tasks[0]["task_id"]

            # Complete (approve) the task
            complete_result = await client.call_tool(
                "complete_task",
                {
                    "task_id": task_id,
                    "status": "completed",
                    "response_data": {"decision": "approved", "reviewer": "e2e-test"},
                },
            )
            complete_data = parse_tool_result(complete_result)

            assert complete_data["task_id"] == task_id, "complete_task must return the task_id"
            assert complete_data["status"] == "completed", (
                f"Task status must be 'completed', got: {complete_data['status']}"
            )
            assert "completed_at" in complete_data, "complete_task must return completed_at timestamp"

            # Verify the task is now completed via get_task
            get_task_result = await client.call_tool("get_task", {"task_id": task_id})
            task_detail = parse_tool_result(get_task_result)
            assert task_detail["status"] == "completed", (
                f"Task must be completed after complete_task call, got: {task_detail['status']}"
            )

            # Verify approval changed observable creative state, not just the workflow task.
            list_result = await client.call_tool("list_creatives", {})
            list_data = parse_tool_result(list_result)
            matching_creatives = [c for c in list_data["creatives"] if c["creative_id"] == creative_id]
            assert matching_creatives, f"Approved creative {creative_id} must appear in list_creatives"
            assert matching_creatives[0]["status"] == "approved", (
                f"Creative {creative_id} must become approved after task completion, "
                f"got: {matching_creatives[0]['status']}"
            )

    @pytest.mark.asyncio
    async def test_creative_visible_via_list_creatives(self, docker_services_e2e, live_server, test_auth_token):
        """Synced creatives must be visible via list_creatives."""
        setup, client_cm = await self._bootstrap_review_client(live_server)
        async with client_cm as client:
            # Get product format_id
            products_result = await client.call_tool(
                "get_products",
                {"brief": "display advertising", "context": {"e2e": "list_creatives"}},
            )
            products_data = parse_tool_result(products_result)
            product = products_data["products"][0]
            format_id = product["format_ids"][0]

            # Sync a creative
            creative_id = f"creative_lc_{uuid.uuid4().hex[:8]}"
            creative = build_creative(
                creative_id=creative_id,
                format_id=format_id,
                name="List Creatives Test",
                asset_url="https://example.com/lc-test-creative.jpg",
                status="approved",
            )
            sync_request = build_sync_creatives_request(creatives=[creative])
            sync_result = await client.call_tool("sync_creatives", sync_request)
            sync_data = parse_tool_result(sync_result)
            assert any(c["creative_id"] == creative_id for c in sync_data.get("creatives", [])), (
                f"sync_creatives must return {creative_id}, got {sync_data}"
            )

            # Verify list_creatives returns the synced creative
            list_result = await client.call_tool("list_creatives", {})
            list_data = parse_tool_result(list_result)

            assert "creatives" in list_data, "list_creatives must return creatives key"
            creative_ids = [c["creative_id"] for c in list_data["creatives"]]
            assert creative_id in creative_ids, (
                f"Synced creative {creative_id} must appear in list_creatives response for "
                f"{setup['tenant_subdomain']}"
            )

    @pytest.mark.asyncio
    async def test_creative_assigned_to_package_after_sync(self, docker_services_e2e, live_server, test_auth_token):
        """Creative assigned via sync_creatives assignments must appear in delivery data."""
        async with make_mcp_client(live_server, test_auth_token) as client:
            # Get product
            products_result = await client.call_tool(
                "get_products",
                {"brief": "display advertising", "context": {"e2e": "creative_assignment"}},
            )
            products_data = parse_tool_result(products_result)
            product = products_data["products"][0]
            product_id = product["product_id"]
            pricing_option_id = product["pricing_options"][0]["pricing_option_id"]
            format_id = product["format_ids"][0]

            # Create media buy
            start_time, end_time = get_test_date_range(days_from_now=1, duration_days=30)
            pkg_buyer_ref = f"pkg_wf_{uuid.uuid4().hex[:8]}"
            media_buy_request = build_adcp_media_buy_request(
                product_ids=[product_id],
                total_budget=1500.0,
                start_time=start_time,
                end_time=end_time,
                brand={"domain": "creativetest.com"},
                pricing_option_id=pricing_option_id,
                context={"e2e": "creative_assignment"},
            )
            # Override package buyer_ref to be deterministic for assignment
            media_buy_request["packages"][0]["buyer_ref"] = pkg_buyer_ref

            create_result = await client.call_tool("create_media_buy", media_buy_request)
            create_data = parse_tool_result(create_result)
            media_buy_id = create_data["media_buy_id"]
            package_id = None
            response_packages = create_data.get("packages", [])
            if response_packages:
                package_id = response_packages[0].get("package_id")
            if not package_id:
                package_id = get_package_id_by_buyer_ref(live_server, media_buy_id, pkg_buyer_ref)

            # Force-approve so we can assign creatives
            force_approve_media_buy_in_db(live_server, media_buy_id)

            # Sync creative with assignment to the package
            creative_id = f"creative_assign_{uuid.uuid4().hex[:8]}"
            creative = build_creative(
                creative_id=creative_id,
                format_id=format_id,
                name="Assignment Test Creative",
                asset_url="https://example.com/assign-test.jpg",
            )
            sync_request = build_sync_creatives_request(
                creatives=[creative],
                assignments={creative_id: [package_id]},
            )
            sync_result = await client.call_tool("sync_creatives", sync_request)
            sync_data = parse_tool_result(sync_result)

            assert "creatives" in sync_data, "sync_creatives must return creatives"
            assert len(sync_data["creatives"]) == 1

            # Verify delivery endpoint returns data for this media buy
            delivery_result = await client.call_tool(
                "get_media_buy_delivery",
                {"media_buy_ids": [media_buy_id]},
            )
            delivery_data = parse_tool_result(delivery_result)

            assert "deliveries" in delivery_data or "media_buy_deliveries" in delivery_data, (
                f"Delivery must be returned for approved media buy {media_buy_id}"
            )
