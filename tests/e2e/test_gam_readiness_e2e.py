"""Gated GAM sell-readiness coverage through real admin routes."""

from __future__ import annotations

import json
import uuid

import pytest
import requests

from tests.e2e.adcp_request_builder import parse_tool_result
from tests.e2e.admin_flow_helpers import (
    bootstrap_tenant_via_container,
    create_admin_session,
    create_authorized_property,
    create_principal,
    create_product,
    get_db_connection,
    get_seeded_format_and_product,
    wait_until,
)
from tests.e2e.conftest import GAM_TEST_ADVERTISER_ID, GAM_TEST_NETWORK_CODE
from tests.e2e.utils import make_mcp_client


def _get_first_synced_ad_unit_id(live_server, tenant_id: str) -> str | None:
    with get_db_connection(live_server) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT inventory_id
            FROM gam_inventory
            WHERE tenant_id = %s AND inventory_type = 'ad_unit'
            ORDER BY inventory_id
            LIMIT 1
            """,
            (tenant_id,),
        )
        row = cursor.fetchone()
    return row[0] if row else None


@pytest.mark.requires_gam
@pytest.mark.asyncio
async def test_gam_readiness_e2e(docker_services_e2e, live_server, gam_service_account_json):
    suffix = uuid.uuid4().hex[:8]
    tenant_id = f"gam_ready_{suffix}"
    subdomain = f"gam-ready-{suffix}"
    bootstrap_tenant_via_container(tenant_id=tenant_id, subdomain=subdomain, name=f"GAM Ready {suffix}")

    admin_session = create_admin_session(live_server, tenant_id)

    configure_response = admin_session.post(
        f"{live_server['admin']}/tenant/{tenant_id}/gam/configure",
        json={
            "auth_method": "service_account",
            "service_account_json": gam_service_account_json,
            "network_code": GAM_TEST_NETWORK_CODE,
            "network_currency": "USD",
        },
        timeout=20,
    )
    assert configure_response.status_code == 200, (
        f"GAM configure failed: {configure_response.status_code} {configure_response.text[:800]}"
    )
    configure_payload = configure_response.json()
    assert configure_payload.get("success") is True, configure_payload

    sync_response = admin_session.post(
        f"{live_server['admin']}/api/tenant/{tenant_id}/inventory/sync",
        json={
            "types": ["ad_units"],
            "custom_targeting_limit": 5,
            "audience_segment_limit": 5,
        },
        timeout=20,
    )
    assert sync_response.status_code == 202, f"Inventory sync failed: {sync_response.status_code} {sync_response.text}"
    sync_id = sync_response.json()["sync_id"]

    sync_status = wait_until(
        lambda: _poll_gam_sync(admin_session, live_server, tenant_id, sync_id),
        timeout_s=120,
        interval_s=2,
        description=f"GAM sync {sync_id} to complete",
    )
    assert sync_status["status"] == "completed", sync_status

    create_authorized_property(
        admin_session,
        live_server,
        tenant_id,
        name=f"GAM Property {suffix}",
        publisher_domain=f"gam-{suffix}.example.com",
        tags=["all_inventory"],
    )
    principal = create_principal(
        admin_session,
        live_server,
        tenant_id,
        name=f"GAM Principal {suffix}",
        enable_mock=False,
        gam_advertiser_id=GAM_TEST_ADVERTISER_ID,
    )

    ad_unit_id = wait_until(
        lambda: _get_first_synced_ad_unit_id(live_server, tenant_id),
        timeout_s=30,
        interval_s=1,
        description=f"synced GAM ad units for {tenant_id}",
    )

    format_ref, _ = await get_seeded_format_and_product(live_server, "ci-test-token")
    product_id = f"prod_gam_{suffix}"
    create_product(
        admin_session,
        live_server,
        tenant_id,
        product_id=product_id,
        name=f"GAM Sellable Product {suffix}",
        tag_scope=f"gam-{suffix}.example.com:all_inventory",
        formats_json=json.dumps([format_ref]),
        extra_form_data={
            "allowed_principal_ids": [principal["principal_id"]],
            "targeted_ad_unit_ids": ad_unit_id,
        },
    )

    async with make_mcp_client(live_server, principal["access_token"], tenant=subdomain) as client:
        result = await client.call_tool(
            "get_products",
            {"brief": "display advertising", "context": {"e2e": "gam_readiness"}},
        )
        payload = parse_tool_result(result)
        product_ids = {product["product_id"] for product in payload["products"]}
        assert product_id in product_ids, f"GAM-backed product {product_id} not discoverable via MCP: {product_ids}"


def _poll_gam_sync(session: requests.Session, live_server, tenant_id: str, sync_id: str) -> dict | None:
    response = session.get(f"{live_server['admin']}/tenant/{tenant_id}/gam/sync-status/{sync_id}", timeout=15)
    if response.status_code != 200:
        return None
    payload = response.json()
    if payload.get("status") in {"completed", "failed"}:
        return payload
    return None
