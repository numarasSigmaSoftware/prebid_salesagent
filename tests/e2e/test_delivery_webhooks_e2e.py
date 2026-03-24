"""End-to-end blueprint for delivery webhook flow.

This follows the reference E2E patterns and calls real MCP tools:

1. get_products
2. create_media_buy (with reporting_webhook and inline creatives)
3. get_media_buy_delivery for an explicit period
4. Wait for scheduled delivery_report webhook and inspect payload

All TODOs are left for you to fill in assertions and any spec-specific checks.
"""

import asyncio
import json
import socket
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from time import sleep
from typing import Any

import psycopg2
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
from tests.e2e.utils import force_approve_media_buy_in_db, make_mcp_client, wait_for_server_readiness


class DeliveryWebhookReceiver(BaseHTTPRequestHandler):
    """Simple webhook receiver to capture delivery_report notifications."""

    received_webhooks: list[Any] = []

    def do_POST(self):
        """Handle POST requests (webhook notifications)."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            payload = json.loads(body.decode("utf-8"))
            self.received_webhooks.append(payload)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "received"}')
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, format, *args):
        """Silence HTTP server logs during tests."""
        pass


@pytest.fixture
def delivery_webhook_server():
    """Start a local HTTP server to receive delivery_report webhooks."""

    # Find an available port
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("0.0.0.0", 0))
    port = s.getsockname()[1]
    s.close()

    # Start server on all interfaces so it's reachable from Docker container
    # (via host.docker.internal mapping)
    server = HTTPServer(("0.0.0.0", port), DeliveryWebhookReceiver)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # We still use localhost in the URL because the MCP server's
    # protocol_webhook_service explicitly looks for 'localhost' to rewrite
    # it to 'host.docker.internal'
    webhook_url = f"http://localhost:{port}/webhook"

    yield {
        "url": webhook_url,
        "server": server,
        "received": DeliveryWebhookReceiver.received_webhooks,
    }

    server.shutdown()
    server.server_close()
    DeliveryWebhookReceiver.received_webhooks.clear()


class TestDailyDeliveryWebhookFlow:
    """Blueprint E2E test for daily delivery webhooks."""

    def setup_adapter_config(self, live_server):
        """Configure adapter for auto-approval (needs active media buy for delivery scheduler)."""
        try:
            conn = psycopg2.connect(live_server["postgres"])
            cursor = conn.cursor()

            # Ensure ci-test tenant has mock manual approval disabled
            cursor.execute("SELECT tenant_id FROM tenants WHERE subdomain = 'ci-test'")
            tenant_row = cursor.fetchone()
            if tenant_row:
                tenant_id = tenant_row[0]
                cursor.execute(
                    """
                    INSERT INTO adapter_config (tenant_id, adapter_type, mock_manual_approval_required)
                    VALUES (%s, 'mock', false)
                    ON CONFLICT (tenant_id)
                    DO UPDATE SET mock_manual_approval_required = false, adapter_type = 'mock'
                """,
                    (tenant_id,),
                )
                conn.commit()
                print(f"Updated adapter config for tenant {tenant_id}: manual_approval=False")
            else:
                print("Warning: ci-test tenant not found for adapter config update")

            cursor.close()
            conn.close()
        except Exception as e:
            print(f"Failed to update adapter config: {e}")

    async def discover_product(self, client):
        """Phase 1: Product discovery (get_products)."""
        products_result = await client.call_tool(
            "get_products",
            {
                "brand": {"domain": "testbrand.com"},
                "brief": "display advertising",
                "context": {"e2e": "delivery_webhook_get_products"},
            },
        )
        products_data = parse_tool_result(products_result)

        assert "products" in products_data
        assert isinstance(products_data["products"], list)
        assert len(products_data["products"]) > 0

        # Verify context echo
        assert products_data.get("context", {}).get("e2e") == "delivery_webhook_get_products"

        # Pick first product
        product = products_data["products"][0]
        product_id = product["product_id"]
        pricing_option_id = product["pricing_options"][0]["pricing_option_id"]

        # Pick formats_ids
        format_ids = product["format_ids"]

        return product_id, pricing_option_id, format_ids

    async def build_inline_creative(self, format_id: dict[str, Any]) -> dict[str, Any]:
        """Phase 2: Build inline creative for testing (no external sync)."""
        creative = build_creative(
            creative_id="cr_" + uuid.uuid4().hex[:8],
            format_id=format_id,
            name="Delivery Test Creative",
            asset_url="https://via.placeholder.com/300x250.png",
        )
        return creative

    async def create_media_buy(self, client, product_id, pricing_option_id, delivery_webhook_server):
        """Phase 3: Create media buy with reporting_webhook."""
        _, end_time = get_test_date_range(days_from_now=0, duration_days=7)
        start_time = "asap"

        media_buy_request = build_adcp_media_buy_request(
            product_ids=[product_id],
            total_budget=2000.0,
            start_time=start_time,
            end_time=end_time,
            brand={"domain": "testbrand.com"},
            webhook_url=delivery_webhook_server["url"],
            reporting_frequency="daily",
            context={"e2e": "delivery_webhook_create_media_buy"},
            pricing_option_id=pricing_option_id,
        )

        create_result = await client.call_tool("create_media_buy", media_buy_request)
        create_data = parse_tool_result(create_result)

        assert "media_buy_id" in create_data

        # Verify context echo
        assert create_data.get("context", {}).get("e2e") == "delivery_webhook_create_media_buy"

        media_buy_id = create_data.get("media_buy_id")
        buyer_ref = create_data.get("buyer_ref")

        assert media_buy_id or buyer_ref  # Blueprint sanity check

        return media_buy_id, start_time, end_time

    def force_approve_media_buy(self, live_server, media_buy_id):
        """Force approve media buy in database to bypass approval workflow."""
        force_approve_media_buy_in_db(live_server, media_buy_id)

    @pytest.mark.asyncio
    async def test_daily_delivery_webhook_end_to_end(
        self,
        docker_services_e2e,
        live_server,
        test_auth_token,
        delivery_webhook_server,
    ):
        """
        End-to-end blueprint:

        1. Discover a product (get_products)
        2. Create media buy with reporting_webhook.frequency = "daily"
        3. Get delivery metrics explicitly via get_media_buy_delivery
        4. Wait for scheduled delivery_report webhook and inspect payload
        """
        self.setup_adapter_config(live_server)

        headers = {
            "x-adcp-auth": test_auth_token,
            "x-adcp-tenant": "ci-test",  # Explicit tenant selection for E2E tests
        }
        print("live_server")
        print(live_server)
        transport = StreamableHttpTransport(url=f"{live_server['mcp']}/mcp/", headers=headers)

        # Wait for server readiness
        wait_for_server_readiness(live_server["mcp"])

        async with Client(transport=transport) as client:
            # 1. Discover Product
            product_id, pricing_option_id, format_ids = await self.discover_product(client)

            # 2. Create Media Buy
            # Use approved creatives from init_database_ci.py
            media_buy_id, start_time, end_time = await self.create_media_buy(
                client, product_id, pricing_option_id, delivery_webhook_server
            )

            # 3. Force Approve Media Buy
            self.force_approve_media_buy(live_server, media_buy_id)

            # 4. Explicit Delivery Check
            start_date_str = start_time
            if start_time == "asap":
                from datetime import UTC, datetime

                start_date_str = datetime.now(UTC).date().isoformat()
            else:
                start_date_str = start_time.split("T")[0]

            delivery_period = {
                "start_date": start_date_str,
                "end_date": end_time.split("T")[0],
            }

            delivery_result = await client.call_tool(
                "get_media_buy_delivery",
                {
                    "media_buy_ids": [media_buy_id],
                    **delivery_period,
                    "context": {"e2e": "delivery_webhook_get_media_buy_delivery"},
                },
            )

            delivery_data = parse_tool_result(delivery_result)

            assert "media_buy_deliveries" in delivery_data
            assert len(delivery_data["media_buy_deliveries"]) > 0
            assert delivery_data["media_buy_deliveries"][0]["totals"]["impressions"] > 0
            assert delivery_data.get("context", {}).get("e2e") == "delivery_webhook_get_media_buy_delivery"

            # 5. Wait for Webhook
            # The scheduler runs inside the container.
            # We configured DELIVERY_WEBHOOK_INTERVAL=5 in conftest.py for E2E tests.
            # It should trigger in 5 seconds.

            received = delivery_webhook_server["received"]

            # Wait for webhook
            timeout_seconds = 30
            poll_interval = 1

            elapsed = 0
            while elapsed < timeout_seconds and not received:
                sleep(poll_interval)
                elapsed += poll_interval

            assert received, (
                "Expected at least one delivery report webhook. Check connectivity and DELIVERY_WEBHOOK_INTERVAL."
            )

            if received:
                webhook_payload = received[0]

                # Verify webhook payload structure (MCP webhook format)
                assert webhook_payload.get("status") == "completed", (
                    f"Expected status 'completed', got {webhook_payload.get('status')}"
                )
                assert webhook_payload.get("task_id") == media_buy_id, (
                    f"Expected task_id '{media_buy_id}', got {webhook_payload.get('task_id')}"
                )
                assert "timestamp" in webhook_payload, "Missing timestamp in webhook payload"

                result = webhook_payload.get("result") or {}

                # Verify delivery data
                media_buy_deliveries = result.get("media_buy_deliveries")
                assert media_buy_deliveries is not None, "Missing media_buy_deliveries in result"
                assert len(media_buy_deliveries) > 0, "Expected at least one media_buy_delivery"
                assert media_buy_deliveries[0]["media_buy_id"] == media_buy_id

                # Verify scheduling metadata
                assert result.get("notification_type") == "scheduled", (
                    f"Expected notification_type 'scheduled', got {result.get('notification_type')}"
                )
                assert "next_expected_at" in result, "Missing next_expected_at in result"


class TestUpdatePerformanceIndex:
    """E2E test for the update_performance_index tool."""

    @pytest.mark.asyncio
    async def test_update_performance_index_accepted(
        self,
        docker_services_e2e,
        live_server,
        test_auth_token,
    ):
        """
        update_performance_index must accept valid performance data for an existing media buy.

        Flow:
          get_products → create_media_buy → force_approve → update_performance_index
        """
        async with make_mcp_client(live_server, test_auth_token) as client:
            # Discover product
            products_result = await client.call_tool(
                "get_products",
                {"brief": "display advertising", "context": {"e2e": "perf_index"}},
            )
            products_data = parse_tool_result(products_result)
            assert len(products_data["products"]) > 0
            product = products_data["products"][0]
            product_id = product["product_id"]
            pricing_option_id = product["pricing_options"][0]["pricing_option_id"]

            # Create media buy
            _, end_time = get_test_date_range(days_from_now=0, duration_days=7)
            media_buy_request = build_adcp_media_buy_request(
                product_ids=[product_id],
                total_budget=1000.0,
                start_time="asap",
                end_time=end_time,
                brand={"domain": "perftest.com"},
                pricing_option_id=pricing_option_id,
                context={"e2e": "perf_index"},
            )
            create_result = await client.call_tool("create_media_buy", media_buy_request)
            create_data = parse_tool_result(create_result)
            media_buy_id = create_data["media_buy_id"]

            # Force-approve so the media buy is visible to performance tools
            force_approve_media_buy_in_db(live_server, media_buy_id)

            # Submit performance feedback
            perf_result = await client.call_tool(
                "update_performance_index",
                {
                    "media_buy_id": media_buy_id,
                    "performance_data": [
                        {
                            "product_id": product_id,
                            "performance_index": 0.85,
                        }
                    ],
                    "context": {"e2e": "perf_index"},
                },
            )
            perf_data = parse_tool_result(perf_result)

            # update_performance_index currently returns an acknowledgement-only contract.
            # Until there is a production read path for the submitted performance index,
            # assert the strongest stable tool response available.
            assert perf_data["status"] == "success", f"Expected success status, got: {perf_data}"
            assert perf_data["detail"] == "Performance index updated for 1 products", (
                f"Unexpected performance update detail: {perf_data}"
            )
            assert perf_data.get("context", {}).get("e2e") == "perf_index", (
                f"Expected context echo for perf_index update, got: {perf_data}"
            )


async def _sync_creative_with_webhook(
    client,
    *,
    format_id: str,
    creative_prefix: str,
    webhook_url: str,
    context_label: str,
) -> str:
    """Sync a pending_review creative with push_notification_config. Returns creative_id."""
    creative_id = f"{creative_prefix}_{uuid.uuid4().hex[:8]}"
    creative = build_creative(
        creative_id=creative_id,
        format_id=format_id,
        name=f"Webhook Test Creative ({context_label})",
        asset_url="https://example.com/wh-test.jpg",
        click_through_url="https://example.com/landing",
        status="pending_review",
    )
    result = await client.call_tool(
        "sync_creatives",
        build_sync_creatives_request(creatives=[creative], webhook_url=webhook_url),
    )
    sync_data = parse_tool_result(result)
    assert any(c["creative_id"] == creative_id for c in sync_data.get("creatives", [])), (
        f"sync_creatives must return {creative_id}"
    )
    return creative_id


async def _poll_creative_review_task(client, creative_id: str, *, retries: int = 15) -> str:
    """Poll list_tasks until a review task appears for the creative. Returns task_id."""
    for _ in range(retries):
        tasks_result = await client.call_tool("list_tasks", {"object_type": "creative", "object_id": creative_id})
        tasks = parse_tool_result(tasks_result).get("tasks", [])
        if tasks:
            return tasks[0]["task_id"]
        await asyncio.sleep(1)
    raise AssertionError(f"No workflow task appeared for creative {creative_id} after {retries}s")


async def _wait_for_webhook(received: list, *, timeout_s: int = 15) -> None:
    """Poll until at least one webhook arrives or timeout."""
    for _ in range(timeout_s * 2):  # 0.5s intervals
        if received:
            return
        await asyncio.sleep(0.5)
    raise AssertionError(f"No webhook received after {timeout_s}s")


class TestCreativeReviewWebhookFlow:
    """E2E tests for creative-review push notifications via push_notification_config."""

    @pytest.mark.asyncio
    async def test_creative_review_approval_fires_webhook(
        self, docker_services_e2e, live_server, test_auth_token, delivery_webhook_server
    ):
        """Approving a creative review task must fire the push_notification_config webhook.

        Asserts the webhook payload contains:
        - status == "completed"
        - result.creatives[0].creative_id matches the synced creative
        - result.creatives[0].status == "approved"

        Note: The `pending_count` guard in call_webhook_for_creative_status ensures the
        webhook fires only after all creatives on a step are reviewed. With the standard
        one-step-per-creative sync flow this is always satisfied on the first review.
        """
        setup = await bootstrap_review_ready_tenant(live_server, tenant_prefix="cr_wh_approve")
        async with make_mcp_client(live_server, setup["access_token"], tenant=setup["tenant_subdomain"]) as client:
            products_result = await client.call_tool(
                "get_products",
                {"brief": "display advertising", "context": {"e2e": "cr_wh_approve"}},
            )
            products_data = parse_tool_result(products_result)
            format_id = products_data["products"][0]["format_ids"][0]

            creative_id = await _sync_creative_with_webhook(
                client,
                format_id=format_id,
                creative_prefix="cr_wh_app",
                webhook_url=delivery_webhook_server["url"],
                context_label="cr_wh_approve",
            )
            task_id = await _poll_creative_review_task(client, creative_id)

            await client.call_tool(
                "complete_task",
                {
                    "task_id": task_id,
                    "status": "completed",
                    "response_data": {"decision": "approved", "reviewer": "e2e-test"},
                },
            )

            await _wait_for_webhook(delivery_webhook_server["received"])
            payload = delivery_webhook_server["received"][0]

            assert payload.get("status") == "completed", f"Expected status 'completed', got: {payload.get('status')}"
            result = payload.get("result") or {}
            creatives_in_result = result.get("creatives", [])
            assert creatives_in_result, f"Webhook result must include creatives, got: {result}"
            matching = [c for c in creatives_in_result if c.get("creative_id") == creative_id]
            assert matching, f"Creative {creative_id} must appear in webhook result, got: {creatives_in_result}"
            assert matching[0]["status"] == "approved", (
                f"Creative must show 'approved' in webhook, got: {matching[0]['status']}"
            )

    @pytest.mark.asyncio
    async def test_creative_review_rejection_fires_webhook(
        self, docker_services_e2e, live_server, test_auth_token, delivery_webhook_server
    ):
        """Rejecting a creative review task must fire the push_notification_config webhook.

        Asserts the webhook payload contains:
        - status == "completed"
        - result.creatives[0].creative_id matches the synced creative
        - result.creatives[0].status == "rejected"
        """
        setup = await bootstrap_review_ready_tenant(live_server, tenant_prefix="cr_wh_reject")
        async with make_mcp_client(live_server, setup["access_token"], tenant=setup["tenant_subdomain"]) as client:
            products_result = await client.call_tool(
                "get_products",
                {"brief": "display advertising", "context": {"e2e": "cr_wh_reject"}},
            )
            products_data = parse_tool_result(products_result)
            format_id = products_data["products"][0]["format_ids"][0]

            creative_id = await _sync_creative_with_webhook(
                client,
                format_id=format_id,
                creative_prefix="cr_wh_rej",
                webhook_url=delivery_webhook_server["url"],
                context_label="cr_wh_reject",
            )
            task_id = await _poll_creative_review_task(client, creative_id)

            await client.call_tool(
                "complete_task",
                {
                    "task_id": task_id,
                    "status": "completed",
                    "response_data": {
                        "decision": "rejected",
                        "reviewer": "e2e-test",
                        "reason": "policy violation",
                    },
                },
            )

            await _wait_for_webhook(delivery_webhook_server["received"])
            payload = delivery_webhook_server["received"][0]

            assert payload.get("status") == "completed", f"Expected status 'completed', got: {payload.get('status')}"
            result = payload.get("result") or {}
            creatives_in_result = result.get("creatives", [])
            matching = [c for c in creatives_in_result if c.get("creative_id") == creative_id]
            assert matching, f"Creative {creative_id} must appear in webhook result, got: {creatives_in_result}"
            assert matching[0]["status"] == "rejected", (
                f"Creative must show 'rejected' in webhook, got: {matching[0]['status']}"
            )
