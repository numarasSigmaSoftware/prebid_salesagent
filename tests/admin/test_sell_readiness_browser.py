"""Browser-driven UI coverage for sell-readiness and approval flows."""

from __future__ import annotations

import asyncio
import re
import uuid

import pytest

from tests.admin.browser_flow_helpers import browser_page, install_dialog_recorder, login_as_tenant_admin
from tests.e2e.adcp_request_builder import build_adcp_media_buy_request, get_test_date_range, parse_tool_result
from tests.e2e.admin_flow_helpers import (
    bootstrap_tenant_via_container,
    create_admin_session,
    create_authorized_property,
    create_principal,
    create_property_tag,
    get_latest_workflow_step_for_media_buy,
    get_media_buy_status,
    get_seeded_format_and_product,
    get_tenant_id_by_subdomain,
    provision_sellable_product,
    wait_until,
)
from tests.e2e.conftest import GAM_TEST_NETWORK_CODE
from tests.e2e.utils import make_mcp_client

pytestmark = [pytest.mark.ui, pytest.mark.e2e, pytest.mark.requires_server]


def test_add_product_browser_flow(docker_services_e2e, live_server):
    """Create a sellable product through the actual browser form."""
    tenant_id = get_tenant_id_by_subdomain(live_server, "ci-test")
    suffix = uuid.uuid4().hex[:8]
    product_id = f"prod_browser_{suffix}"
    publisher_domain = f"browser-{suffix}.example.com"
    property_tag = f"browser_tag_{suffix}"

    admin_session = create_admin_session(live_server, tenant_id)
    create_property_tag(
        admin_session,
        live_server,
        tenant_id,
        tag_id=property_tag,
        name=f"Browser Tag {suffix}",
        description="Browser UI setup tag",
    )
    create_authorized_property(
        admin_session,
        live_server,
        tenant_id,
        name=f"Browser Property {suffix}",
        publisher_domain=publisher_domain,
        tags=[property_tag],
    )
    principal = create_principal(
        admin_session,
        live_server,
        tenant_id,
        name=f"Browser Principal {suffix}",
        enable_mock=True,
    )
    format_ref, _ = asyncio.run(get_seeded_format_and_product(live_server, "ci-test-token"))

    with browser_page(live_server["admin"]) as page:
        login_as_tenant_admin(page, live_server["admin"], tenant_id)
        page.goto(f"{live_server['admin']}/tenant/{tenant_id}/products/add", wait_until="networkidle")

        page.fill("#name", f"Browser Product {suffix}")
        page.fill("#product_id", product_id)
        page.fill("#description", "Created through the browser UI")
        page.fill("#delivery_measurement_provider", "publisher")
        page.select_option("select[name='pricing_model_0']", "cpm_fixed")
        page.fill("input[name='rate_0']", "12.50")
        page.select_option("#allowed_principal_ids", principal["principal_id"])
        page.check(f'input[name="selected_property_tags"][value="{publisher_domain}:{property_tag}"]')
        page.evaluate(
            """(formatRef) => {
                document.getElementById('formats-data').value = JSON.stringify([formatRef]);
            }""",
            format_ref,
        )

        page.get_by_role("button", name="Create Product").click()
        page.wait_for_url(re.compile(rf".*/tenant/{re.escape(tenant_id)}/products(?:\?.*)?$"), timeout=15000)
        page.get_by_text(product_id, exact=False).first.wait_for(state="visible", timeout=15000)

    async def _assert_discovery():
        async with make_mcp_client(live_server, principal["access_token"], tenant="ci-test") as client:
            result = await client.call_tool(
                "get_products",
                {"brief": "display advertising", "context": {"e2e": "browser_add_product"}},
            )
            payload = parse_tool_result(result)
            return product_id in {product["product_id"] for product in payload["products"]}

    assert asyncio.run(_assert_discovery()), f"Browser-created product {product_id} was not discoverable via MCP"


def test_workflow_approval_browser_flow(docker_services_e2e, live_server):
    """Approve a pending media buy from the browser review flow."""
    tenant_id = get_tenant_id_by_subdomain(live_server, "ci-test")
    suffix = uuid.uuid4().hex[:8]
    setup = asyncio.run(
        provision_sellable_product(
            live_server,
            tenant_id,
            product_suffix=f"browser_approval_{suffix}",
        )
    )

    async def _create_pending_media_buy() -> tuple[str, str]:
        async with make_mcp_client(live_server, setup["access_token"], tenant="ci-test") as client:
            products_result = await client.call_tool(
                "get_products",
                {"brief": "display advertising", "context": {"e2e": "browser_approval_discovery"}},
            )
            products_payload = parse_tool_result(products_result)
            product = next(
                product for product in products_payload["products"] if product["product_id"] == setup["product_id"]
            )

            start_time, end_time = get_test_date_range(days_from_now=1, duration_days=14)
            create_request = build_adcp_media_buy_request(
                product_ids=[setup["product_id"]],
                total_budget=1500.0,
                start_time=start_time,
                end_time=end_time,
                brand={"domain": f"browser-approval-{suffix}.example.com"},
                pricing_option_id=product["pricing_options"][0]["pricing_option_id"],
                buyer_ref=f"browser_approval_{suffix}",
                context={"e2e": "browser_approval_create"},
            )

            create_result = await client.call_tool("create_media_buy", create_request)
            create_payload = parse_tool_result(create_result)
            media_buy_id = create_payload["media_buy_id"]
            workflow_step = get_latest_workflow_step_for_media_buy(live_server, media_buy_id)
            return media_buy_id, workflow_step["workflow_id"]

    media_buy_id, workflow_id = asyncio.run(_create_pending_media_buy())

    with browser_page(live_server["admin"]) as page:
        dialogs = install_dialog_recorder(page)
        login_as_tenant_admin(page, live_server["admin"], tenant_id)
        page.goto(f"{live_server['admin']}/tenant/{tenant_id}/workflows", wait_until="networkidle")

        review_link = page.locator(f'tr#{media_buy_id} a:has-text("Review & Approve")').first
        review_link.wait_for(state="visible", timeout=15000)
        review_link.click()
        page.wait_for_url(re.compile(rf".*/tenant/{re.escape(tenant_id)}/media-buy/{re.escape(media_buy_id)}/approve$"))
        page.get_by_role("button", name=re.compile("Approve")).click()
        page.wait_for_load_state("networkidle")

    assert any("approve this media buy" in message.lower() for message in dialogs), dialogs

    final_status = wait_until(
        lambda: get_media_buy_status(live_server, media_buy_id),
        timeout_s=30,
        interval_s=1,
        description=f"media buy {media_buy_id} to be approved via browser",
    )
    assert final_status in {"scheduled", "pending_creatives", "active", "approved"}, final_status

    with browser_page(live_server["admin"]) as page:
        login_as_tenant_admin(page, live_server["admin"], tenant_id)
        page.goto(
            f"{live_server['admin']}/tenant/{tenant_id}/workflows/{workflow_id}/steps/"
            f"{get_latest_workflow_step_for_media_buy(live_server, media_buy_id)['step_id']}/review",
            wait_until="networkidle",
        )
        page.get_by_text("Approved", exact=False).first.wait_for(state="visible", timeout=15000)


@pytest.mark.requires_gam
def test_gam_sync_browser_flow(docker_services_e2e, live_server, gam_service_account_json):
    """Configure GAM and trigger inventory sync from the browser UI."""
    suffix = uuid.uuid4().hex[:8]
    tenant_id = f"browser_gam_{suffix}"
    subdomain = f"browser-gam-{suffix}"
    bootstrap_tenant_via_container(tenant_id=tenant_id, subdomain=subdomain, name=f"Browser GAM {suffix}")

    with browser_page(live_server["admin"]) as page:
        dialogs = install_dialog_recorder(page)
        login_as_tenant_admin(page, live_server["admin"], tenant_id)
        page.goto(f"{live_server['admin']}/tenant/{tenant_id}/settings", wait_until="networkidle")

        page.locator("#manual_service_account_json").fill(gam_service_account_json)
        page.locator("#manual_network_code").fill(GAM_TEST_NETWORK_CODE)
        with page.expect_response(re.compile(rf".*/tenant/{re.escape(tenant_id)}/gam/configure$")) as configure_info:
            page.get_by_role("button", name="Save Service Account Configuration").click()
        configure_payload = configure_info.value.json()
        assert configure_payload.get("success") is True, configure_payload
        page.wait_for_load_state("networkidle")

        page.goto(f"{live_server['admin']}/tenant/{tenant_id}/inventory", wait_until="networkidle")
        with page.expect_response(re.compile(rf".*/api/tenant/{re.escape(tenant_id)}/inventory/sync$")) as sync_info:
            page.get_by_role("button", name="Sync All").click()
        sync_payload = sync_info.value.json()
        sync_id = sync_payload["sync_id"]

        assert any("service account configuration saved" in message.lower() for message in dialogs), dialogs
        assert any("sync started" in message.lower() for message in dialogs), dialogs

        sync_status = wait_until(
            lambda: _poll_sync_status(live_server["admin"], tenant_id, sync_id),
            timeout_s=120,
            interval_s=2,
            description=f"GAM browser sync {sync_id} to complete",
        )
        assert sync_status["status"] == "completed", sync_status

        page.goto(f"{live_server['admin']}/tenant/{tenant_id}/products/add", wait_until="networkidle")
        assert not page.get_by_text("Inventory Not Synced", exact=False).count()
        page.locator("#targeted_ad_unit_ids").wait_for(state="attached", timeout=10000)


def _poll_sync_status(base_url: str, tenant_id: str, sync_id: str) -> dict | None:
    import requests

    response = requests.get(f"{base_url}/tenant/{tenant_id}/gam/sync-status/{sync_id}", timeout=15)
    if response.status_code != 200:
        return None
    payload = response.json()
    if payload.get("status") in {"completed", "failed"}:
        return payload
    return None
