"""AccountListEnv — integration test environment for _list_accounts_impl.

Patches: audit logger ONLY.
Real: get_db_session, AccountRepository, all query building (all hit real DB).

Requires: integration_db fixture (creates test PostgreSQL DB).

Usage::

    @pytest.mark.requires_db
    def test_something(self, integration_db):
        with AccountListEnv() as env:
            tenant, principal = env.setup_default_data()
            account = AccountFactory(tenant=tenant, account_id="acc_1")
            AgentAccountAccessFactory(
                tenant_id=tenant.tenant_id, principal=principal, account=account
            )

            response = env.call_impl()
            assert len(response.accounts) == 1

Available mocks via env.mock:
    "audit_logger" -- get_audit_logger (module-level import)

beads: salesagent-7do
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from src.core.schemas.account import ListAccountsResponse
from tests.harness._base import IntegrationEnv


class AccountListEnv(IntegrationEnv):
    """Integration test environment for _list_accounts_impl.

    Only mocks the audit logger. Everything else is real:
    - Real get_db_session -> real DB queries
    - Real AccountRepository -> real DB reads
    - Real query building, filtering, pagination
    """

    EXTERNAL_PATCHES = {
        "audit_logger": "src.core.tools.accounts.get_audit_logger",
    }

    def _configure_mocks(self) -> None:
        """Set up happy-path defaults for audit logger."""
        mock_logger = MagicMock()
        self.mock["audit_logger"].return_value = mock_logger

    def call_impl(self, **kwargs: Any) -> ListAccountsResponse:
        """Call _list_accounts_impl with real DB.

        Accepts all _list_accounts_impl kwargs. The 'identity' kwarg
        defaults to self.identity if not provided.
        """
        from src.core.tools.accounts import _list_accounts_impl

        self._commit_factory_data()
        kwargs.setdefault("identity", self.identity)
        return _list_accounts_impl(**kwargs)

    def call_a2a(self, **kwargs: Any) -> ListAccountsResponse:
        """Call list_accounts via real AdCPRequestHandler — full A2A pipeline."""
        return self._run_a2a_handler("list_accounts", ListAccountsResponse, **kwargs)

    def call_mcp(self, **kwargs: Any) -> ListAccountsResponse:
        """Call list_accounts via Client(mcp) — full pipeline dispatch."""
        return self._run_mcp_client("list_accounts", ListAccountsResponse, **kwargs)

    REST_ENDPOINT = "/api/v1/accounts"

    def build_rest_body(self, **kwargs: Any) -> dict[str, Any]:
        """Serialize the full list-accounts read envelope.

        This env is called in two shapes and must serve both. BDD steps build a
        typed ``ListAccountsRequest`` and pass it as ``req``; integration tests
        pass flat boundary fields (``idempotency_key`` for the read-idempotency
        persistence tests, ``status`` for the validation-parity tests).

        Handling only the flat shape sent an EMPTY body for every ``req=``
        caller, and the endpoint answers an empty body with unfiltered,
        unpaginated results — so the status-filter and pagination scenarios
        asserted against the full account list and failed on REST while passing
        on MCP and A2A, whose dispatchers unpack ``req`` themselves.
        """
        from pydantic import BaseModel as PydanticBaseModel

        req = kwargs.pop("req", None)
        body: dict[str, Any] = super().build_rest_body(req=req) if req is not None else {}
        for key in ("status", "sandbox", "pagination", "idempotency_key", "context"):
            value = kwargs.get(key)
            if value is None:
                continue
            # Explicit boundary fields win over the request's own, matching the
            # base's overlay order.
            body[key] = (
                value.model_dump(mode="json", exclude_none=True) if isinstance(value, PydanticBaseModel) else value
            )
        return body

    def parse_rest_response(self, data: dict[str, Any]) -> ListAccountsResponse:
        """Parse REST JSON into ListAccountsResponse."""
        return ListAccountsResponse(**data)
