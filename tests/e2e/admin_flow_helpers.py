"""Shared helpers for admin-driven E2E setup flows."""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from collections.abc import Callable
from typing import Any

import psycopg2
import requests


def _super_admin_email() -> str:
    return os.environ.get("TEST_SUPER_ADMIN_EMAIL", "test_super_admin@example.com")


def _super_admin_password() -> str:
    return os.environ.get("TEST_SUPER_ADMIN_PASSWORD", "test123")


def create_admin_session(live_server: dict[str, Any], tenant_id: str) -> requests.Session:
    """Authenticate a requests session via the test auth endpoint."""
    session = requests.Session()
    response = session.post(
        f"{live_server['admin']}/test/auth",
        data={
            "email": _super_admin_email(),
            "password": _super_admin_password(),
            "tenant_id": tenant_id,
        },
        allow_redirects=False,
        timeout=10,
    )
    if response.status_code == 404:
        with get_db_connection(live_server) as conn, conn.cursor() as cursor:
            cursor.execute("SELECT name, subdomain FROM tenants WHERE tenant_id = %s", (tenant_id,))
            row = cursor.fetchone()
        assert row, f"Tenant {tenant_id!r} not found while enabling test auth"
        bootstrap_tenant_via_container(
            tenant_id=tenant_id,
            subdomain=row[1],
            name=row[0],
            auth_setup_mode=True,
        )
        response = session.post(
            f"{live_server['admin']}/test/auth",
            data={
                "email": _super_admin_email(),
                "password": _super_admin_password(),
                "tenant_id": tenant_id,
            },
            allow_redirects=False,
            timeout=10,
        )
    assert response.status_code == 302, f"Admin test auth failed: {response.status_code} {response.text[:500]}"
    return session


def get_db_connection(live_server: dict[str, Any]):
    """Open a direct PostgreSQL connection for E2E assertions."""
    params = live_server["postgres_params"]
    return psycopg2.connect(
        host=params["host"],
        port=params["port"],
        user=params["user"],
        password=params["password"],
        dbname=params["dbname"],
    )


def get_tenant_id_by_subdomain(live_server: dict[str, Any], subdomain: str) -> str:
    with get_db_connection(live_server) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT tenant_id FROM tenants WHERE subdomain = %s", (subdomain,))
        row = cursor.fetchone()
    assert row, f"Tenant with subdomain {subdomain!r} not found"
    return row[0]


def create_property_tag(
    session: requests.Session,
    live_server: dict[str, Any],
    tenant_id: str,
    *,
    tag_id: str,
    name: str,
    description: str,
) -> None:
    response = session.post(
        f"{live_server['admin']}/tenant/{tenant_id}/property-tags/create",
        data={"tag_id": tag_id, "name": name, "description": description},
        allow_redirects=False,
        timeout=10,
    )
    assert response.status_code == 302, f"Property tag creation failed: {response.status_code} {response.text[:500]}"


def create_authorized_property(
    session: requests.Session,
    live_server: dict[str, Any],
    tenant_id: str,
    *,
    name: str,
    publisher_domain: str,
    tags: list[str],
) -> None:
    form_data: dict[str, Any] = {
        "property_type": "website",
        "name": name,
        "publisher_domain": publisher_domain,
        "identifier_type_0": "domain",
        "identifier_value_0": publisher_domain,
        "tags": tags,
    }
    response = session.post(
        f"{live_server['admin']}/tenant/{tenant_id}/authorized-properties/create",
        data=form_data,
        allow_redirects=False,
        timeout=10,
    )
    assert response.status_code == 302, f"Property creation failed: {response.status_code} {response.text[:500]}"


def create_principal(
    session: requests.Session,
    live_server: dict[str, Any],
    tenant_id: str,
    *,
    name: str,
    enable_mock: bool = True,
    gam_advertiser_id: str | None = None,
) -> dict[str, str]:
    data: dict[str, Any] = {"name": name}
    if enable_mock:
        data["enable_mock"] = "on"
    if gam_advertiser_id:
        data["gam_advertiser_id"] = gam_advertiser_id

    response = session.post(
        f"{live_server['admin']}/tenant/{tenant_id}/principals/create",
        data=data,
        allow_redirects=False,
        timeout=10,
    )
    assert response.status_code == 302, f"Principal creation failed: {response.status_code} {response.text[:500]}"

    with get_db_connection(live_server) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT principal_id, access_token
            FROM principals
            WHERE tenant_id = %s AND name = %s
            ORDER BY created_at DESC NULLS LAST
            LIMIT 1
            """,
            (tenant_id, name),
        )
        row = cursor.fetchone()

    assert row, f"Principal {name!r} not found after creation"
    return {"principal_id": row[0], "access_token": row[1]}


def create_product(
    session: requests.Session,
    live_server: dict[str, Any],
    tenant_id: str,
    *,
    product_id: str,
    name: str,
    tag_scope: str,
    formats_json: str,
    extra_form_data: dict[str, Any] | None = None,
) -> None:
    data: dict[str, Any] = {
        "product_id": product_id,
        "name": name,
        "description": f"E2E product {name}",
        "formats": formats_json,
        "pricing_model_0": "cpm_fixed",
        "currency_0": "USD",
        "rate_0": "10.00",
        "property_mode": "tags",
        "selected_property_tags": tag_scope,
        "delivery_measurement_provider": "publisher",
    }
    if extra_form_data:
        data.update(extra_form_data)

    response = session.post(
        f"{live_server['admin']}/tenant/{tenant_id}/products/add",
        data=data,
        allow_redirects=False,
        timeout=15,
    )
    if response.status_code == 200:
        with get_db_connection(live_server) as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM products WHERE tenant_id = %s AND product_id = %s LIMIT 1",
                (tenant_id, product_id),
            )
            if cursor.fetchone():
                return
    assert response.status_code == 302, f"Product creation failed: {response.status_code} {response.text[:800]}"


async def get_seeded_format_and_product(live_server, auth_token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reuse live discovery endpoints to source a valid format reference."""
    from tests.e2e.adcp_request_builder import parse_tool_result
    from tests.e2e.utils import make_mcp_client

    async with make_mcp_client(live_server, auth_token) as client:
        formats_result = await client.call_tool("list_creative_formats", {})
        formats_payload = parse_tool_result(formats_result)
        assert formats_payload["formats"], "Expected default creative agent formats for E2E readiness tests"
        format_ref = next(
            (
                fmt["format_id"]
                for fmt in formats_payload["formats"]
                if isinstance(fmt.get("format_id"), dict) and "display" in fmt["format_id"].get("id", "").lower()
            ),
            formats_payload["formats"][0]["format_id"],
        )

        result = await client.call_tool(
            "get_products",
            {"brief": "display advertising", "context": {"e2e": "sell_readiness_seed_formats"}},
        )
        payload = parse_tool_result(result)
        assert payload["products"], "Expected seeded CI products for E2E readiness tests"
        product = payload["products"][0]
        return format_ref, product


def get_package_id_by_buyer_ref(live_server: dict[str, Any], media_buy_id: str, package_buyer_ref: str) -> str:
    """Resolve a package buyer_ref to the persisted package_id used by assignment APIs."""
    with get_db_connection(live_server) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT package_id
            FROM media_packages
            WHERE media_buy_id = %s
              AND package_config->>'buyer_ref' = %s
            ORDER BY package_id
            LIMIT 1
            """,
            (media_buy_id, package_buyer_ref),
        )
        row = cursor.fetchone()
    assert row, f"Package with buyer_ref {package_buyer_ref!r} not found for media buy {media_buy_id}"
    return row[0]


def get_latest_workflow_step_for_media_buy(live_server: dict[str, Any], media_buy_id: str) -> dict[str, str]:
    with get_db_connection(live_server) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT ws.step_id, ws.context_id
            FROM workflow_steps ws
            JOIN object_workflow_mappings owm ON owm.step_id = ws.step_id
            WHERE owm.object_type = 'media_buy' AND owm.object_id = %s
            ORDER BY ws.created_at DESC
            LIMIT 1
            """,
            (media_buy_id,),
        )
        row = cursor.fetchone()
    assert row, f"No workflow step found for media buy {media_buy_id}"
    return {"step_id": row[0], "workflow_id": row[1]}


def get_media_buy_status(live_server: dict[str, Any], media_buy_id: str) -> str | None:
    with get_db_connection(live_server) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT status FROM media_buys WHERE media_buy_id = %s", (media_buy_id,))
        row = cursor.fetchone()
    return row[0] if row else None


def approve_workflow_step(
    session: requests.Session,
    live_server: dict[str, Any],
    tenant_id: str,
    *,
    workflow_id: str,
    step_id: str,
) -> None:
    response = session.post(
        f"{live_server['admin']}/tenant/{tenant_id}/workflows/{workflow_id}/steps/{step_id}/approve",
        allow_redirects=False,
        timeout=15,
    )
    assert response.status_code == 200, f"Workflow approval failed: {response.status_code} {response.text[:800]}"
    payload = response.json()
    assert payload.get("success") is True, f"Unexpected approval payload: {payload}"


def wait_until(
    predicate: Callable[[], Any],
    *,
    timeout_s: float = 30.0,
    interval_s: float = 1.0,
    description: str,
) -> Any:
    """Poll until predicate returns a truthy value, then return it."""
    deadline = time.time() + timeout_s
    last_value = None
    while time.time() < deadline:
        last_value = predicate()
        if last_value:
            return last_value
        time.sleep(interval_s)
    raise AssertionError(f"Timed out waiting for {description}; last value={last_value!r}")


def unique_suffix(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


async def provision_sellable_product(
    live_server: dict[str, Any],
    tenant_id: str,
    *,
    product_suffix: str,
    seed_auth_token: str = "ci-test-token",
    enable_mock: bool = True,
    gam_advertiser_id: str | None = None,
) -> dict[str, str]:
    """Create the minimum publisher setup needed for a sellable product."""
    admin_session = create_admin_session(live_server, tenant_id)
    new_tag = unique_suffix(f"sell_ready_tag_{product_suffix}").lower()
    publisher_domain = f"{product_suffix}.e2e.example.com"
    principal_name = f"E2E Principal {product_suffix}"
    product_id = f"prod_{product_suffix}"
    product_name = f"E2E Sellable Product {product_suffix}"

    create_property_tag(
        admin_session,
        live_server,
        tenant_id,
        tag_id=new_tag,
        name=f"Sell Ready {product_suffix}",
        description="E2E readiness tag",
    )
    create_authorized_property(
        admin_session,
        live_server,
        tenant_id,
        name=f"E2E Property {product_suffix}",
        publisher_domain=publisher_domain,
        tags=[new_tag],
    )
    principal = create_principal(
        admin_session,
        live_server,
        tenant_id,
        name=principal_name,
        enable_mock=enable_mock,
        gam_advertiser_id=gam_advertiser_id,
    )

    format_ref, _ = await get_seeded_format_and_product(live_server, seed_auth_token)
    create_product(
        admin_session,
        live_server,
        tenant_id,
        product_id=product_id,
        name=product_name,
        tag_scope=f"{publisher_domain}:{new_tag}",
        formats_json=json.dumps([format_ref]),
        extra_form_data={
            "allowed_principal_ids": [principal["principal_id"]],
        },
    )

    return {
        "product_id": product_id,
        "product_name": product_name,
        "principal_id": principal["principal_id"],
        "access_token": principal["access_token"],
        "publisher_domain": publisher_domain,
        "property_tag": new_tag,
    }


def bootstrap_tenant_via_container(
    *,
    tenant_id: str,
    subdomain: str,
    name: str,
    auth_setup_mode: bool = True,
    human_review_required: bool = False,
    approval_mode: str = "auto-approve",
) -> None:
    """Create a minimal tenant and baseline records inside the app container."""
    bootstrap_script = f"""
from datetime import UTC, datetime

from sqlalchemy import select

from src.core.database.database_session import get_db_session
from src.core.database.models import CurrencyLimit, PropertyTag, Tenant, TenantAuthConfig

tenant_id = {tenant_id!r}
subdomain = {subdomain!r}
name = {name!r}
auth_setup_mode = {auth_setup_mode!r}
human_review_required = {human_review_required!r}
approval_mode = {approval_mode!r}

with get_db_session() as session:
    tenant = session.scalars(select(Tenant).filter_by(tenant_id=tenant_id)).first()
    if not tenant:
        tenant = Tenant(
            tenant_id=tenant_id,
            name=name,
            subdomain=subdomain,
            billing_plan="test",
            ad_server="mock",
            enable_axe_signals=True,
            is_active=True,
            authorized_emails=["ci-test@example.com"],
            auth_setup_mode=auth_setup_mode,
            auto_approve_format_ids=[],
            human_review_required=human_review_required,
            approval_mode=approval_mode,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(tenant)
    else:
        tenant.auth_setup_mode = auth_setup_mode
        tenant.human_review_required = human_review_required
        tenant.approval_mode = approval_mode

    tag = session.scalars(select(PropertyTag).filter_by(tenant_id=tenant_id, tag_id="all_inventory")).first()
    if not tag:
        session.add(
            PropertyTag(
                tag_id="all_inventory",
                tenant_id=tenant_id,
                name="All Inventory",
                description="Default E2E inventory tag",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )

    currency = session.scalars(select(CurrencyLimit).filter_by(tenant_id=tenant_id, currency_code="USD")).first()
    if not currency:
        session.add(
            CurrencyLimit(
                tenant_id=tenant_id,
                currency_code="USD",
                min_package_budget=1.0,
                max_daily_package_spend=10000.0,
            )
        )

    auth = session.scalars(select(TenantAuthConfig).filter_by(tenant_id=tenant_id)).first()
    if not auth:
        session.add(
            TenantAuthConfig(
                tenant_id=tenant_id,
                oidc_enabled=True,
                oidc_provider="google",
                oidc_discovery_url="https://accounts.google.com/.well-known/openid-configuration",
                oidc_client_id="gam-e2e-client-id",
            )
        )

    session.commit()
"""
    result = subprocess.run(
        [
            "docker-compose",
            "-f",
            "docker-compose.e2e.yml",
            "exec",
            "-T",
            "adcp-server",
            "python",
            "-c",
            bootstrap_script,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"Failed to bootstrap tenant {tenant_id}: rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
        )


async def bootstrap_review_ready_tenant(
    live_server: dict[str, Any],
    *,
    tenant_prefix: str,
    seed_auth_token: str = "ci-test-token",
) -> dict[str, str]:
    """Create a tenant explicitly configured for manual-review approval flows."""
    suffix = uuid.uuid4().hex[:8]
    tenant_id = f"{tenant_prefix}_{suffix}"
    subdomain = tenant_id.replace("_", "-")

    bootstrap_tenant_via_container(
        tenant_id=tenant_id,
        subdomain=subdomain,
        name=f"{tenant_prefix.replace('_', ' ').title()} {suffix}",
        auth_setup_mode=True,
        human_review_required=True,
        approval_mode="require-human",
    )

    provisioned = await provision_sellable_product(
        live_server,
        tenant_id,
        product_suffix=f"{tenant_prefix}_{suffix}",
        seed_auth_token=seed_auth_token,
    )
    provisioned["tenant_id"] = tenant_id
    provisioned["tenant_subdomain"] = subdomain
    return provisioned
