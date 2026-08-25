"""Integration tests for account REST routes (list_accounts + sync_accounts).

Verifies REST transport parity with IMPL/A2A/MCP transports.
These routes don't exist yet — tests should FAIL until implemented.

beads: salesagent-4ud
"""

from __future__ import annotations

import pytest

from tests.factories.account import AccountFactory, AgentAccountAccessFactory
from tests.helpers import assert_envelope_field, assert_envelope_shape


@pytest.mark.requires_db
class TestListAccountsRestRoute:
    """REST /api/v1/accounts route should call list_accounts_raw."""

    def test_list_accounts_returns_accounts(self, integration_db):
        """GET accounts via REST returns same data as IMPL."""
        from tests.harness.account_list import AccountListEnv

        with AccountListEnv() as env:
            tenant, principal = env.setup_default_data()
            AccountFactory(tenant=tenant, status="active")
            AgentAccountAccessFactory(
                tenant_id=tenant.tenant_id,
                principal=principal,
                account=AccountFactory._meta.sqlalchemy_session.query(AccountFactory._meta.model).first(),
            )

            # IMPL baseline
            impl_response = env.call_impl()
            assert len(impl_response.accounts) >= 1

            # REST should return the same
            client = env.get_rest_client()
            rest_response = client.post("/api/v1/accounts", json={})
            assert rest_response.status_code == 200, (
                f"Expected 200, got {rest_response.status_code}: {rest_response.text}"
            )
            data = rest_response.json()
            assert "accounts" in data
            assert len(data["accounts"]) == len(impl_response.accounts)


@pytest.mark.requires_db
class TestSyncAccountsRestRoute:
    """REST /api/v1/accounts/sync route should call sync_accounts_raw."""

    def test_sync_accounts_creates_account(self, integration_db):
        """POST sync via REST creates account same as IMPL."""
        from tests.harness.account_sync import AccountSyncEnv

        with AccountSyncEnv() as env:
            env.setup_default_data()

            client = env.get_rest_client()
            rest_response = client.post(
                "/api/v1/accounts/sync",
                json={
                    "idempotency_key": "account-rest-route-0001",
                    "accounts": [
                        {
                            "brand": {"domain": "rest-test.com"},
                            "operator": "rest-test.com",
                            "billing": "operator",
                        }
                    ],
                },
            )
            assert rest_response.status_code == 200, (
                f"Expected 200, got {rest_response.status_code}: {rest_response.text}"
            )
            data = rest_response.json()
            assert "accounts" in data
            assert len(data["accounts"]) == 1
            assert data["accounts"][0]["brand"]["domain"] == "rest-test.com"

    def test_sync_accounts_rejects_an_unsafe_callback_url(self, integration_db):
        """REST applies the same registration-time callback policy as its siblings.

        `push_notification_config` rides into `SyncAccountsRequest` through
        `model_dump`, so the route has to run the callback funnel itself. Without
        it, REST accepts a link-local metadata address that `update_media_buy` and
        `sync_creatives` both reject — and the account is synced regardless.
        """
        from tests.harness.account_sync import AccountSyncEnv

        with AccountSyncEnv() as env:
            env.setup_default_data()

            rest_response = env.get_rest_client().post(
                "/api/v1/accounts/sync",
                json={
                    "idempotency_key": "account-rest-ssrf-0001",
                    "accounts": [
                        {
                            "brand": {"domain": "ssrf-test.com"},
                            "operator": "ssrf-test.com",
                            "billing": "operator",
                        }
                    ],
                    "push_notification_config": {"url": "https://169.254.169.254/latest/meta-data"},
                },
            )

            assert rest_response.status_code == 400, (
                f"expected the callback rejection, got {rest_response.status_code}: {rest_response.text}"
            )
            body = rest_response.json()
            assert_envelope_shape(body, "VALIDATION_ERROR", recovery="correctable", message_substr="SSRF")
            assert_envelope_field(body, "push_notification_config.url")
            assert "169.254.169.254" not in body["errors"][0]["message"], (
                "the metadata address leaked back to the buyer"
            )
