"""The admin media-buy detail route reads a sandbox buy through the simulator.

The route builds an adapter to fetch delivery metrics for a buy it is displaying. It
holds no buyer identity — an operator is looking at someone else's buy — so the mode
must come from the buy's own account. Getting that wrong reads a sandbox buy's delivery
from the tenant's real ad server, which is the same defect as dispatching one there.

This route had no oracle at all. Its ``sandbox=`` argument could be replaced with a
hard-wired ``False`` and every suite stayed green, because the metrics block is gated on
``status in ("active", "approved", "completed")`` while the only tests that loaded the
page created a ``pending_approval`` buy — so the block never executed anywhere.

Driven through the Flask client against a real database rather than by calling the view
function: the mode is derived inside the request, from a row, using the route's own
session, and that session's lifetime is itself load-bearing here (routing this through a
UoW would close it under the route and 500 the page).
"""

from unittest.mock import MagicMock

import pytest

from src.admin.app import create_app
from tests.factories import AccountFactory, MediaBuyFactory, PrincipalFactory, TenantFactory
from tests.helpers.sandbox_assertions import assert_all_live, assert_all_sandbox

app = create_app()

pytestmark = [pytest.mark.admin, pytest.mark.requires_db]

_TENANT = "mbdetail_sbx_tenant"


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SESSION_COOKIE_PATH"] = "/"
    with app.test_client() as c:
        yield c


def _auth(client, tenant_id):
    """The session shape @require_tenant_access actually reads.

    A partial session yields 403 and the metrics block never runs — which the
    vacuity anchor below reports rather than passing as "no adapter, no problem".
    """
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["user"] = {"email": "test@example.com", "is_super_admin": True}
        sess["email"] = "test@example.com"
        sess["tenant_id"] = tenant_id
        sess["test_user"] = "test@example.com"
        sess["test_user_role"] = "super_admin"
        sess["test_user_name"] = "Test User"
        sess["test_tenant_id"] = tenant_id


def _seed(factory_session, *, sandbox: bool) -> str:
    """An ACTIVE buy owned by an account in the given mode.

    ACTIVE is the point: the metrics block the sandbox decision lives in is gated on
    status, so a pending buy — which is all the existing page tests create — never
    reaches it.

    Factories only, no session.add()/get_db_session() in the body (CLAUDE.md §Test
    Fixtures, enforced by test_architecture_repository_pattern, which scans tests/admin).
    """
    from datetime import date

    suffix = "sbx" if sandbox else "live"
    tenant = TenantFactory(tenant_id=f"{_TENANT}_{suffix}", ad_server="mock")
    principal = PrincipalFactory(tenant=tenant, principal_id=f"p_detail_{suffix}")
    AccountFactory(tenant=tenant, account_id="acc_detail", sandbox=sandbox)
    buy = MediaBuyFactory(
        tenant=tenant,
        principal=principal,
        account_id="acc_detail",
        status="active",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
    )
    factory_session.commit()
    return tenant.tenant_id, buy.media_buy_id


def _sandbox_spy(monkeypatch) -> MagicMock:
    """Spy recording every get_adapter the route builds, for the shared assertions.

    Returns the MagicMock rather than a bare ``list[bool]`` so the assertions below run
    through ``assert_all_sandbox`` / ``assert_all_live``. The hand-rolled recorder this
    replaced dropped the helper's non-bool guard, so ``sandbox=None`` — falsy, but not an
    explicit live decision — satisfied ``assert not any(seen)`` as though the route had
    decided correctly. Latent only because ``account_is_sandbox`` coerces with ``bool()``;
    the helper exists so that stays true by construction rather than by luck.

    Patched at the SOURCE module: the route imports get_adapter inside the request
    handler, so it is never an attribute of the blueprint module and patching there
    silently no-ops (AttributeError at best, a spy that records nothing at worst).
    """
    import src.core.helpers.adapter_helpers as helpers

    spy = MagicMock(side_effect=helpers.get_adapter)
    monkeypatch.setattr(helpers, "get_adapter", spy)
    return spy


def test_sandbox_buy_detail_reads_through_the_simulator(client, factory_session, monkeypatch):
    tenant_id, media_buy_id = _seed(factory_session, sandbox=True)
    spy = _sandbox_spy(monkeypatch)
    _auth(client, tenant_id)

    resp = client.get(f"/tenant/{tenant_id}/media-buy/{media_buy_id}")

    # A 500 after the adapter was built leaves the mode assertion passing on a page the
    # operator never sees, so the status is asserted rather than discarded.
    assert resp.status_code == 200, f"the detail page did not render (status={resp.status_code})"
    assert_all_sandbox(spy, context="admin media-buy detail for a sandbox buy")


def test_live_buy_detail_reads_through_the_real_adapter(client, factory_session, monkeypatch):
    """Negative control — 'always sandbox' would silently show simulated delivery for real buys."""
    tenant_id, media_buy_id = _seed(factory_session, sandbox=False)
    spy = _sandbox_spy(monkeypatch)
    _auth(client, tenant_id)

    resp = client.get(f"/tenant/{tenant_id}/media-buy/{media_buy_id}")

    assert resp.status_code == 200, f"the detail page did not render (status={resp.status_code})"
    assert_all_live(spy, context="admin media-buy detail for a live buy")
