"""
Media Buy Lifecycle E2E Test

Covers the full state machine for a media buy using the mock adapter:
  create_media_buy (pending_approval)
    → workflow task created
    → list_tasks / get_task (verify task exists)
    → force-approve in DB (bypasses human-approval gate, same as test_adcp_full_lifecycle.py)
    → update_media_buy (pause: active=False)
    → update_media_buy (resume: active=True)
    → get_media_buy_delivery (verify status after resume)

GAM adapter tests (test_gam_lifecycle.py) cover pause/resume with real GAM credentials.
This test covers the same lifecycle against the mock adapter so CI always catches regressions.
"""

import uuid

import pytest

from tests.e2e.adcp_request_builder import (
    build_adcp_media_buy_request,
    build_update_media_buy_request,
    get_test_date_range,
    parse_tool_result,
)
from tests.e2e.admin_flow_helpers import bootstrap_review_ready_tenant
from tests.e2e.utils import force_approve_media_buy_in_db, make_mcp_client


class TestMediaBuyLifecycle:
    """E2E test for the full media buy state machine (mock adapter, no real GAM)."""

    @pytest.mark.asyncio
    async def test_pending_approval_creates_workflow_task(self, docker_services_e2e, live_server, test_auth_token):
        """create_media_buy must create a workflow task visible via list_tasks / get_task."""
        setup = await bootstrap_review_ready_tenant(
            live_server,
            tenant_prefix="media_buy_lifecycle",
        )
        async with make_mcp_client(live_server, setup["access_token"], tenant=setup["tenant_subdomain"]) as client:
            # Get product so we can reference a real product_id and pricing_option_id
            products_result = await client.call_tool(
                "get_products",
                {"brief": "display advertising", "context": {"e2e": "lifecycle_task_check"}},
            )
            products_data = parse_tool_result(products_result)
            assert len(products_data["products"]) > 0, "CI tenant must have at least one product"

            product = products_data["products"][0]
            product_id = product["product_id"]
            pricing_option_id = product["pricing_options"][0]["pricing_option_id"]

            start_time, end_time = get_test_date_range(days_from_now=1, duration_days=30)
            buyer_ref = f"lifecycle_task_{uuid.uuid4().hex[:8]}"

            media_buy_request = build_adcp_media_buy_request(
                product_ids=[product_id],
                total_budget=1000.0,
                start_time=start_time,
                end_time=end_time,
                brand={"domain": "testbrand.com"},
                pricing_option_id=pricing_option_id,
                buyer_ref=buyer_ref,
                context={"e2e": "lifecycle_task_check"},
            )

            create_result = await client.call_tool("create_media_buy", media_buy_request)
            create_data = parse_tool_result(create_result)

            media_buy_id = create_data.get("media_buy_id")
            assert media_buy_id, "create_media_buy must return media_buy_id"

            # Verify a workflow task was created for this media buy
            tasks_result = await client.call_tool(
                "list_tasks",
                {"object_type": "media_buy", "object_id": media_buy_id},
            )
            tasks_data = parse_tool_result(tasks_result)

            assert "tasks" in tasks_data, "list_tasks must return tasks key"
            tasks = tasks_data["tasks"]
            assert len(tasks) > 0, (
                f"create_media_buy must create at least one workflow task for {media_buy_id}, "
                f"but list_tasks returned 0 tasks"
            )

            # Verify task structure
            task = tasks[0]
            assert "task_id" in task, "Task must have task_id"
            assert "status" in task, "Task must have status"
            assert task["status"] in ("pending", "in_progress", "requires_approval"), (
                f"Approval task should be pending, got {task['status']}"
            )

            # Verify get_task returns the same task with full details
            task_id = task["task_id"]
            get_task_result = await client.call_tool("get_task", {"task_id": task_id})
            task_detail = parse_tool_result(get_task_result)

            assert task_detail["task_id"] == task_id, "get_task must return the same task_id"
            assert "associated_objects" in task_detail, "Task detail must list associated_objects"

            # Verify the task is linked to our media buy
            associated_ids = [obj["id"] for obj in task_detail["associated_objects"]]
            assert media_buy_id in associated_ids, (
                f"Workflow task must reference {media_buy_id}, found: {associated_ids}"
            )

    @pytest.mark.asyncio
    async def test_pause_and_resume_lifecycle(self, docker_services_e2e, live_server, test_auth_token):
        """Force-approve a media buy, then pause and resume it via update_media_buy."""
        setup = await bootstrap_review_ready_tenant(
            live_server,
            tenant_prefix="media_buy_pause_resume",
        )
        async with make_mcp_client(live_server, setup["access_token"], tenant=setup["tenant_subdomain"]) as client:
            # Get product
            products_result = await client.call_tool(
                "get_products",
                {"brief": "display advertising", "context": {"e2e": "lifecycle_pause_resume"}},
            )
            products_data = parse_tool_result(products_result)
            product = products_data["products"][0]
            product_id = product["product_id"]
            pricing_option_id = product["pricing_options"][0]["pricing_option_id"]

            start_time, end_time = get_test_date_range(days_from_now=1, duration_days=30)

            # Create media buy
            media_buy_request = build_adcp_media_buy_request(
                product_ids=[product_id],
                total_budget=2000.0,
                start_time=start_time,
                end_time=end_time,
                brand={"domain": "pausetest.com"},
                pricing_option_id=pricing_option_id,
                context={"e2e": "lifecycle_pause_resume"},
            )

            create_result = await client.call_tool("create_media_buy", media_buy_request)
            create_data = parse_tool_result(create_result)
            media_buy_id = create_data["media_buy_id"]

            # Force-approve so we can test pause/resume (same approach as test_adcp_full_lifecycle.py)
            force_approve_media_buy_in_db(live_server, media_buy_id)

            # Pause the media buy (paused=True)
            pause_request = build_update_media_buy_request(
                media_buy_id=media_buy_id,
                paused=True,
            )
            pause_result = await client.call_tool("update_media_buy", pause_request)
            pause_data = parse_tool_result(pause_result)

            # update_media_buy returns UpdateMediaBuySuccess or UpdateMediaBuyError
            # The response should not be an error
            assert "errors" not in pause_data or len(pause_data.get("errors", [])) == 0, (
                f"Pause failed: {pause_data.get('errors')}"
            )

            # Resume the media buy (paused=False)
            resume_request = build_update_media_buy_request(
                media_buy_id=media_buy_id,
                paused=False,
            )
            resume_result = await client.call_tool("update_media_buy", resume_request)
            resume_data = parse_tool_result(resume_result)

            assert "errors" not in resume_data or len(resume_data.get("errors", [])) == 0, (
                f"Resume failed: {resume_data.get('errors')}"
            )

            # Verify delivery endpoint returns data for this media buy (structure check)
            delivery_result = await client.call_tool(
                "get_media_buy_delivery",
                {"media_buy_ids": [media_buy_id]},
            )
            delivery_data = parse_tool_result(delivery_result)

            assert "deliveries" in delivery_data or "media_buy_deliveries" in delivery_data, (
                f"get_media_buy_delivery must return deliveries, got: {list(delivery_data.keys())}"
            )
