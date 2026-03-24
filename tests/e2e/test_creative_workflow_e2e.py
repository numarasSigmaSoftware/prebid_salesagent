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

import asyncio
import uuid

import pytest

from tests.e2e.adcp_request_builder import (
    build_adcp_media_buy_request,
    build_creative,
    build_sync_creatives_request,
    get_test_date_range,
    parse_tool_result,
)
from tests.e2e.admin_flow_helpers import (
    bootstrap_review_ready_tenant,
    get_creative_status,
    get_media_buy_status,
    get_package_id_by_buyer_ref,
    set_media_buy_status,
)
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

            tasks = []
            for _ in range(15):
                tasks_result = await client.call_tool(
                    "list_tasks",
                    {"object_type": "creative", "object_id": creative_id},
                )
                tasks_data = parse_tool_result(tasks_result)
                tasks = tasks_data.get("tasks", [])
                if tasks:
                    break
                await asyncio.sleep(1)
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
                f"Synced creative {creative_id} must appear in list_creatives response for {setup['tenant_subdomain']}"
            )

    async def _sync_and_assign_creative(
        self,
        client,
        *,
        context_label: str,
        creative_prefix: str,
        package_id: str,
        format_id: str,
        status: str = "pending_review",
    ) -> str:
        """Sync a creative and assign it to a package. Returns creative_id."""
        creative_id = f"{creative_prefix}_{uuid.uuid4().hex[:8]}"
        creative = build_creative(
            creative_id=creative_id,
            format_id=format_id,
            name=f"Workflow Test Creative ({context_label})",
            asset_url="https://example.com/wf-test-creative.jpg",
            click_through_url="https://example.com/landing",
            status=status,
        )
        sync_result = await client.call_tool(
            "sync_creatives",
            build_sync_creatives_request(
                creatives=[creative],
                assignments={creative_id: [package_id]},
            ),
        )
        sync_data = parse_tool_result(sync_result)
        assert any(c["creative_id"] == creative_id for c in sync_data.get("creatives", [])), (
            f"sync_creatives must return {creative_id}"
        )
        return creative_id

    async def _poll_task_for_creative(
        self,
        client,
        creative_id: str,
        *,
        max_retries: int = 15,
    ) -> str:
        """Poll list_tasks until a task appears for the creative. Returns task_id."""
        for _ in range(max_retries):
            tasks_result = await client.call_tool(
                "list_tasks",
                {"object_type": "creative", "object_id": creative_id},
            )
            tasks_data = parse_tool_result(tasks_result)
            tasks = tasks_data.get("tasks", [])
            if tasks:
                return tasks[0]["task_id"]
            await asyncio.sleep(1)
        raise AssertionError(f"No workflow task appeared for creative {creative_id} after {max_retries} attempts")

    @pytest.mark.asyncio
    async def test_creative_assigned_to_package_after_sync(self, docker_services_e2e, live_server, test_auth_token):
        """Creative assigned via sync_creatives assignments must appear in delivery data."""
        setup, client_cm = await self._bootstrap_review_client(live_server)
        async with client_cm as client:
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
                status="approved",
            )
            sync_result = await client.call_tool(
                "sync_creatives",
                build_sync_creatives_request(creatives=[creative]),
            )
            sync_data = parse_tool_result(sync_result)

            assert "creatives" in sync_data, "sync_creatives must return creatives"
            assert len(sync_data["creatives"]) == 1

            assignment_result = await client.call_tool(
                "sync_creatives",
                build_sync_creatives_request(
                    creatives=[creative],
                    assignments={creative_id: [package_id]},
                ),
            )
            assignment_data = parse_tool_result(assignment_result)
            assert "creatives" in assignment_data, "Assignment sync must return creatives"

            # Verify delivery endpoint returns data for this media buy
            delivery_result = await client.call_tool(
                "get_media_buy_delivery",
                {"media_buy_ids": [media_buy_id]},
            )
            delivery_data = parse_tool_result(delivery_result)

            assert "deliveries" in delivery_data or "media_buy_deliveries" in delivery_data, (
                f"Delivery must be returned for approved media buy {media_buy_id}"
            )

    @pytest.mark.asyncio
    async def test_complete_task_rejects_creative(self, docker_services_e2e, live_server, test_auth_token):
        """complete_task with decision=rejected must reject the creative and complete the task."""
        setup, client_cm = await self._bootstrap_review_client(live_server)
        async with client_cm as client:
            creative_id = await self._sync_pending_review_creative(
                client,
                context_label="creative_reject",
                creative_prefix="creative_rej",
            )

            task_id = await self._poll_task_for_creative(client, creative_id)

            # Complete with rejection decision
            complete_result = await client.call_tool(
                "complete_task",
                {
                    "task_id": task_id,
                    "status": "completed",
                    "response_data": {"decision": "rejected", "reviewer": "e2e-test", "reason": "test rejection"},
                },
            )
            complete_data = parse_tool_result(complete_result)

            assert complete_data["task_id"] == task_id, "complete_task must return the task_id"
            assert complete_data["status"] == "completed", (
                f"Task status must be 'completed', got: {complete_data['status']}"
            )

            # Task must be completed
            get_task_result = await client.call_tool("get_task", {"task_id": task_id})
            task_detail = parse_tool_result(get_task_result)
            assert task_detail["status"] == "completed", (
                f"Task must be completed after rejection, got: {task_detail['status']}"
            )

            # Creative status must be rejected
            db_status = get_creative_status(live_server, creative_id, setup["tenant_id"])
            assert db_status == "rejected", (
                f"Creative {creative_id} must be 'rejected' after rejection task, got: {db_status!r}"
            )

            list_result = await client.call_tool("list_creatives", {})
            list_data = parse_tool_result(list_result)
            matching = [c for c in list_data["creatives"] if c["creative_id"] == creative_id]
            assert matching, f"Rejected creative {creative_id} must still appear in list_creatives"
            assert matching[0]["status"] == "rejected", (
                f"Creative {creative_id} must show 'rejected' in list_creatives, got: {matching[0]['status']}"
            )

    @pytest.mark.asyncio
    async def test_sequential_creative_approvals_block_media_buy_until_last(
        self, docker_services_e2e, live_server, test_auth_token
    ):
        """Media buy must stay in pending_creatives until the last creative is approved."""
        setup, client_cm = await self._bootstrap_review_client(live_server)
        async with client_cm as client:
            # Get product info for media buy and creative syncing
            products_result = await client.call_tool(
                "get_products",
                {"brief": "display advertising", "context": {"e2e": "multi_creative_gate"}},
            )
            products_data = parse_tool_result(products_result)
            product = products_data["products"][0]
            product_id = product["product_id"]
            pricing_option_id = product["pricing_options"][0]["pricing_option_id"]
            format_id = product["format_ids"][0]

            # Create a media buy
            start_time, end_time = get_test_date_range(days_from_now=1, duration_days=30)
            pkg_buyer_ref = f"pkg_mc_{uuid.uuid4().hex[:8]}"
            media_buy_request = build_adcp_media_buy_request(
                product_ids=[product_id],
                total_budget=2000.0,
                start_time=start_time,
                end_time=end_time,
                brand={"domain": "multitest.com"},
                pricing_option_id=pricing_option_id,
                context={"e2e": "multi_creative_gate"},
            )
            media_buy_request["packages"][0]["buyer_ref"] = pkg_buyer_ref

            create_result = await client.call_tool("create_media_buy", media_buy_request)
            create_data = parse_tool_result(create_result)
            media_buy_id = create_data["media_buy_id"]

            # Get package_id
            package_id = None
            response_packages = create_data.get("packages", [])
            if response_packages:
                package_id = response_packages[0].get("package_id")
            if not package_id:
                package_id = get_package_id_by_buyer_ref(live_server, media_buy_id, pkg_buyer_ref)

            # Force-approve the media buy, then set to pending_creatives so the
            # creative approval gate can activate it
            force_approve_media_buy_in_db(live_server, media_buy_id)
            set_media_buy_status(live_server, media_buy_id, "pending_creatives")

            # Sync two pending_review creatives, each assigned to the package
            creative_id_1 = await self._sync_and_assign_creative(
                client,
                context_label="multi_creative_1",
                creative_prefix="mc1",
                package_id=package_id,
                format_id=format_id,
            )
            creative_id_2 = await self._sync_and_assign_creative(
                client,
                context_label="multi_creative_2",
                creative_prefix="mc2",
                package_id=package_id,
                format_id=format_id,
            )

            task_id_1 = await self._poll_task_for_creative(client, creative_id_1)
            task_id_2 = await self._poll_task_for_creative(client, creative_id_2)

            # Approve the first creative
            await client.call_tool(
                "complete_task",
                {
                    "task_id": task_id_1,
                    "status": "completed",
                    "response_data": {"decision": "approved", "reviewer": "e2e-test"},
                },
            )

            # Media buy must still be pending_creatives — second creative not yet reviewed
            mb_status_after_first = get_media_buy_status(live_server, media_buy_id)
            assert mb_status_after_first == "pending_creatives", (
                f"Media buy must stay 'pending_creatives' after first approval, got: {mb_status_after_first!r}"
            )

            # Approve the second creative
            await client.call_tool(
                "complete_task",
                {
                    "task_id": task_id_2,
                    "status": "completed",
                    "response_data": {"decision": "approved", "reviewer": "e2e-test"},
                },
            )

            # Media buy must now be active or scheduled — all creatives approved
            mb_status_final = get_media_buy_status(live_server, media_buy_id)
            assert mb_status_final in {"active", "scheduled"}, (
                f"Media buy must become 'active' or 'scheduled' after all creatives approved, got: {mb_status_final!r}"
            )
