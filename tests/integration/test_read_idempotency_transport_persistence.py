"""Durable read replay through every production-exposed wire boundary."""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from src.core.database.repositories.uow import IdempotencyUoW
from src.core.schemas import GetProductsResponse, ListCreativesResponse
from src.core.schemas._base import SalesAgentBaseModel
from tests.harness._base import IntegrationEnv
from tests.harness.account_list import AccountListEnv
from tests.harness.capabilities import CapabilitiesEnv
from tests.harness.creative_formats import CreativeFormatsEnv
from tests.harness.creative_list import CreativeListEnv
from tests.harness.delivery_poll import DeliveryPollEnv
from tests.harness.media_buy_list import MediaBuyListEnv
from tests.harness.product import ProductEnv
from tests.harness.transport import Transport

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


class _TaskListResponse(SalesAgentBaseModel):
    tasks: list[dict[str, Any]]
    total: int
    offset: int
    limit: int
    has_more: bool
    replayed: bool = False


class _TaskListEnv(IntegrationEnv):
    def call_mcp(self, **kwargs: Any) -> _TaskListResponse:
        return self._run_mcp_client("list_tasks", _TaskListResponse, **kwargs)


@dataclass(frozen=True)
class _ReadCase:
    tool_name: str
    env_type: type[IntegrationEnv]
    transports: tuple[Transport, ...]
    supports_context: bool = True


_MCP_A2A = (Transport.MCP, Transport.A2A)
_MCP_A2A_REST = (Transport.MCP, Transport.A2A, Transport.REST)
_READ_CASES = (
    _ReadCase("get_adcp_capabilities", CapabilitiesEnv, _MCP_A2A_REST, supports_context=False),
    _ReadCase("get_products", ProductEnv, _MCP_A2A_REST),
    _ReadCase("list_creative_formats", CreativeFormatsEnv, _MCP_A2A_REST, supports_context=False),
    _ReadCase("list_accounts", AccountListEnv, _MCP_A2A_REST),
    _ReadCase("get_media_buys", MediaBuyListEnv, _MCP_A2A),
    _ReadCase("get_media_buy_delivery", DeliveryPollEnv, _MCP_A2A_REST),
    _ReadCase("list_creatives", CreativeListEnv, _MCP_A2A_REST),
    _ReadCase("list_tasks", _TaskListEnv, (Transport.MCP,), supports_context=False),
)


def _case_ids(case: _ReadCase) -> str:
    return case.tool_name


def _stable_wire(wire: dict[str, Any]) -> dict[str, Any]:
    stable = copy.deepcopy(wire)
    stable.pop("replayed", None)
    stable.pop("context", None)
    return stable


@pytest.mark.parametrize("case", _READ_CASES, ids=_case_ids)
def test_each_read_replays_through_every_exposed_transport(integration_db, case: _ReadCase) -> None:
    for transport in case.transports:
        suffix = uuid.uuid4().hex[:10]
        key = f"{case.tool_name}-{transport.value}-{uuid.uuid4().hex}"
        with case.env_type(
            tenant_id=f"read_{transport.value}_{suffix}",
            principal_id=f"agent_{transport.value}_{suffix}",
        ) as env:
            env.setup_default_data()
            original: dict[str, Any] = {"idempotency_key": key}
            retry: dict[str, Any] = {"idempotency_key": key}
            if case.tool_name == "get_products":
                original["brief"] = "video inventory"
                retry["brief"] = "video inventory"
            if case.supports_context:
                original["context"] = {"correlation_id": "original"}
                retry["context"] = {"correlation_id": "retry"}

            first = env.call_via(transport, **original)
            replay = env.call_via(transport, **retry)
            omitted_args = {key: value for key, value in original.items() if key != "idempotency_key"}
            omitted = env.call_via(transport, **omitted_args)

        assert first.is_success, (case.tool_name, transport, first.error)
        assert replay.is_success, (case.tool_name, transport, replay.error)
        assert omitted.is_success, (case.tool_name, transport, omitted.error)
        assert type(replay.payload) is type(first.payload)
        assert first.wire_response is not None
        assert replay.wire_response is not None
        assert first.wire_response.get("replayed", False) is False
        assert replay.wire_response["replayed"] is True
        assert _stable_wire(replay.wire_response) == _stable_wire(first.wire_response)
        if case.supports_context:
            assert replay.wire_response["context"] == {"correlation_id": "retry"}


@dataclass(frozen=True)
class _AnonymousReadCase:
    tool_name: str
    env_type: type[IntegrationEnv]
    extra_kwargs: dict[str, Any]
    supports_context: bool = True
    env_kwargs: dict[str, Any] = field(default_factory=dict)


_ANONYMOUS_READ_CASES = (
    # get_adcp_capabilities is the spec's bootstrap carve-out (exempt from
    # security.mdx rules 1-9 entirely) — it proves nothing about the actual
    # skip-cache deviation _read_scope takes for the OTHER two public tools.
    _AnonymousReadCase("get_adcp_capabilities", CapabilitiesEnv, {}),
    # get_products' own business policy (brand_manifest_policy, independent of
    # transport-layer AUTH_OPTIONAL_TOOLS) defaults to "require_auth" and
    # rejects an anonymous caller regardless of idempotency handling. Opt the
    # tenant into "public" so this case actually reaches _read_scope's
    # skip-cache path rather than failing on an unrelated auth gate.
    _AnonymousReadCase(
        "get_products", ProductEnv, {"brief": "video inventory"}, env_kwargs={"brand_manifest_policy": "public"}
    ),
    # list_creative_formats' REST body (ListCreativeFormatsBody) has no
    # ``context`` field, mirroring _READ_CASES' supports_context=False above.
    _AnonymousReadCase("list_creative_formats", CreativeFormatsEnv, {}, supports_context=False),
)


def _anonymous_case_ids(case: _AnonymousReadCase) -> str:
    return case.tool_name


@pytest.mark.parametrize("transport", _MCP_A2A_REST)
@pytest.mark.parametrize("case", _ANONYMOUS_READ_CASES, ids=_anonymous_case_ids)
def test_anonymous_read_with_a_key_succeeds_and_persists_nothing(
    integration_db, case: _AnonymousReadCase, transport: Transport
) -> None:
    """An anonymous keyed read succeeds normally and leaves no durable row.

    Two things this pins together, because neither alone is the contract:

    - The read succeeds. AUTH_OPTIONAL_TOOLS (get_adcp_capabilities among them)
      is spec-designated public (authentication.mdx), and buyer SDKs send
      idempotency_key uniformly on every call (security.mdx) — so an anonymous
      SDK-originated discovery call carrying a key must not be rejected.
    - No row is ever written. AdCP 3.1.1 scopes durable cache entries per
      (authenticated_agent, account_id, idempotency_key); an anonymous caller
      has no authenticated_agent. Persisting one anyway used to write
      (tenant, NULL, NULL); because the lookup index is NULLS NOT DISTINCT that
      is ONE row-space shared by every anonymous caller of the tenant, so a
      client-chosen key could collide across callers and replay one caller's
      envelope to another. Asserting only success would leave that reachable
      again under a different implementation — the durable absence is what
      proves no scope exists to fuse.

    Covers all three AUTH_OPTIONAL_TOOLS that can carry an idempotency_key:
    get_adcp_capabilities is graded here only as the bootstrap exemption
    baseline; get_products and list_creative_formats are the two tools where
    _read_scope's skip-cache deviation from the spec's per-agent scoping
    requirement actually applies.
    """
    suffix = uuid.uuid4().hex[:10]
    key = f"anonymous-read-{transport.value}-{uuid.uuid4().hex}"
    with case.env_type(
        tenant_id=f"anonymous_read_{suffix}",
        principal_id=f"seed_agent_{suffix}",
        **case.env_kwargs,
    ) as env:
        env.setup_default_data()
        # Clear auth_token too, not just principal_id/account_id: MCP/A2A dispatch
        # takes a "real auth chain" path whenever the identity carries a truthy
        # auth_token (re-resolving the REAL principal from the DB via the token,
        # which would silently defeat the anonymous override for any tool that
        # gates on principal_id, e.g. get_products' brand_manifest_policy check).
        anonymous = env.identity_for(transport).model_copy(
            update={"principal_id": None, "account_id": None, "auth_token": None}
        )

        context_kwargs = {"context": {"correlation_id": "first"}} if case.supports_context else {}
        result = env.call_via(
            transport,
            identity=anonymous,
            idempotency_key=key,
            **context_kwargs,
            **case.extra_kwargs,
        )

        assert not result.is_error, f"an anonymous keyed read to a public tool must succeed: {result.error!r}"

        with IdempotencyUoW(anonymous.tenant_id) as uow:
            assert uow.idempotency_attempts is not None
            assert (
                uow.idempotency_attempts.find_by_key(
                    principal_id=None,
                    account_id=None,
                    idempotency_key=key,
                )
                is None
            ), "an anonymous read must not leave a tenant-wide shared row behind"


@pytest.mark.parametrize("transport", _MCP_A2A)
def test_malformed_keyed_read_is_rejected_before_reservation(integration_db, transport: Transport) -> None:
    """Strict request validation precedes the durable idempotency insert."""
    suffix = uuid.uuid4().hex[:10]
    key = f"malformed-read-{transport.value}-{uuid.uuid4().hex}"
    with ProductEnv(
        tenant_id=f"malformed_read_{suffix}",
        principal_id=f"malformed_agent_{suffix}",
    ) as env:
        env.setup_default_data()
        result = env.call_via(transport, idempotency_key=key, brief=3)
        identity = env.identity_for(transport)
        with IdempotencyUoW(identity.tenant_id) as uow:
            assert uow.idempotency_attempts is not None
            attempt = uow.idempotency_attempts.find_by_key(
                principal_id=identity.principal_id,
                account_id=identity.account_id,
                idempotency_key=key,
            )

    assert result.is_error
    result.assert_wire_error("VALIDATION_ERROR", recovery="correctable")
    assert attempt is None


class _CrossToolReadEnv(IntegrationEnv):
    REST_ENDPOINT = "/api/v1/products"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._skill = "get_products"

    def select_skill(self, skill: str) -> None:
        self._skill = skill
        self.REST_ENDPOINT = "/api/v1/products" if skill == "get_products" else "/api/v1/creatives"

    @property
    def _response_type(self) -> type[SalesAgentBaseModel]:
        return GetProductsResponse if self._skill == "get_products" else ListCreativesResponse

    def call_mcp(self, **kwargs: Any) -> SalesAgentBaseModel:
        return self._run_mcp_client(self._skill, self._response_type, **kwargs)

    def call_a2a(self, **kwargs: Any) -> SalesAgentBaseModel:
        return self._run_a2a_handler(self._skill, self._response_type, **kwargs)

    def build_rest_body(self, **kwargs: Any) -> dict[str, Any]:
        return {key: value for key, value in kwargs.items() if value is not None}

    def parse_rest_response(self, data: dict[str, Any]) -> SalesAgentBaseModel:
        return self._response_type(**data)


@pytest.mark.parametrize("transport", (Transport.MCP, Transport.A2A, Transport.REST))
def test_cross_tool_key_reuse_is_an_exact_wire_conflict(integration_db, transport: Transport) -> None:
    suffix = uuid.uuid4().hex[:10]
    key = f"cross-tool-{transport.value}-{uuid.uuid4().hex}"
    with _CrossToolReadEnv(
        tenant_id=f"cross_tool_{suffix}",
        principal_id=f"cross_agent_{suffix}",
    ) as env:
        env.setup_default_data()
        env.select_skill("get_products")
        first = env.call_via(transport, idempotency_key=key, brief="video inventory")
        env.select_skill("list_creatives")
        conflict = env.call_via(transport, idempotency_key=key)

    assert first.is_success, first.error
    assert conflict.is_error
    conflict.assert_wire_error("IDEMPOTENCY_CONFLICT", recovery="correctable")


def test_rest_read_hashes_pre_normalization_wire_types(integration_db) -> None:
    """Pydantic-equivalent JSON values remain distinct idempotency payloads."""
    suffix = uuid.uuid4().hex[:10]
    key = f"rest-wire-type-{uuid.uuid4().hex}"
    with CreativeFormatsEnv(
        tenant_id=f"rest_wire_{suffix}",
        principal_id=f"rest_wire_agent_{suffix}",
    ) as env:
        env.setup_default_data()
        first = env.call_via(Transport.REST, idempotency_key=key, min_width="300")
        conflict = env.call_via(Transport.REST, idempotency_key=key, min_width=300)

    assert first.is_success, first.error
    assert conflict.is_error
    conflict.assert_wire_error("IDEMPOTENCY_CONFLICT", recovery="correctable")
