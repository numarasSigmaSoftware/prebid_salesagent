"""Universal read idempotency uses the same durable reservation flow."""

from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.tools.tool import ToolResult

from src.core.exceptions import AdCPAuthenticationError, AdCPAuthRequiredError, AdCPIdempotencyConflictError
from src.services.idempotency_replay import (
    ReservationResult,
    _decode_read_response,
    _read_scope,
    execute_idempotent_read,
    raise_on_tool_conflict,
)
from tests.factories import PrincipalFactory


@pytest.mark.asyncio
async def test_anonymous_read_with_a_key_skips_idempotency_and_succeeds() -> None:
    """An unauthenticated caller has no scope to durably cache within, so the
    idempotency machinery is skipped rather than the read being refused.

    AdCP 3.1.1 security.mdx scopes cache entries per (authenticated_agent,
    account_id, idempotency_key) — there is no anonymous slot in that tuple.
    Persisting one anonymously used to write (tenant, NULL, NULL); because the
    lookup index is NULLS NOT DISTINCT that is ONE row-space shared by every
    anonymous caller of the tenant, so a client-chosen key could collide across
    callers and replay one caller's envelope to another. [prior defect]

    Refusing the read outright is not spec-consistent either: authentication.mdx
    designates get_products / get_adcp_capabilities / list_creative_formats
    public, and security.mdx says SDK clients send idempotency_key uniformly on
    every call — refusing would 401 an anonymous SDK caller doing nothing wrong.

    So this asserts the middle path: the read runs normally (work() executes,
    the caller gets a real result) and no durable row is ever written
    (reserve_idempotent is never called) — the fix is "no scope to fuse",
    not "no read allowed".
    """
    identity = PrincipalFactory.make_identity(
        tenant_id="tenant-1",
        tenant={"tenant_id": "tenant-1"},
        principal_id=None,
    )
    work = AsyncMock(return_value={"supported_protocols": ["creative"]})

    with patch("src.services.idempotency_replay.reserve_idempotent") as reserve:
        result = await execute_idempotent_read(
            tool_name="get_adcp_capabilities",
            idempotency_key="anonymous-read-key",
            identity=identity,
            raw_wire_payload={"idempotency_key": "anonymous-read-key"},
            response_type=None,
            work=work,
        )

    assert result == {"supported_protocols": ["creative"]}
    work.assert_awaited_once()
    # No scope to fuse anonymous callers into: skip the mechanism entirely
    # rather than reserve a row under a shared (tenant, NULL, NULL) key.
    reserve.assert_not_called()


@pytest.mark.asyncio
async def test_anonymous_read_to_a_tool_requiring_auth_is_still_rejected() -> None:
    """Skipping the idempotency layer for anonymous callers is a caching
    decision, not an authorization one — a tool that requires auth must keep
    enforcing it from inside work(), unaffected by whether a key was supplied.

    This is the safety property the fix for R2-B1 depends on: _read_scope no
    longer refuses an anonymous caller itself, so if a PRIVATE tool's own
    auth check didn't independently fire, an anonymous caller could bypass it
    merely by attaching an idempotency_key. work() here simulates that
    independent check by raising, exactly as a real _impl's
    require_principal_id would.
    """
    identity = PrincipalFactory.make_identity(tenant_id="t1", tenant={"tenant_id": "t1"}, principal_id=None)

    async def private_work():
        raise AdCPAuthRequiredError("this tool requires authentication", suggestion="authenticate")

    with (
        patch("src.services.idempotency_replay.reserve_idempotent") as reserve,
        pytest.raises(AdCPAuthRequiredError, match="requires authentication"),
    ):
        await execute_idempotent_read(
            tool_name="get_media_buy_delivery",  # a PRIVATE tool, not AUTH_OPTIONAL_TOOLS
            idempotency_key="k1",
            identity=identity,
            raw_wire_payload={"idempotency_key": "k1"},
            response_type=None,
            work=private_work,
        )

    # No durable-row attempt either: the mechanism was skipped, not retried.
    reserve.assert_not_called()


async def test_read_replay_never_executes_work() -> None:
    work = AsyncMock()
    with patch(
        "src.services.idempotency_replay.reserve_idempotent",
        return_value=ReservationResult(replay={"items": [], "replayed": True}),
    ) as reserve:
        result = await execute_idempotent_read(
            tool_name="list_creative_formats",
            idempotency_key="read-replay-key",
            identity=PrincipalFactory.make_identity(
                tenant_id="tenant-1",
                tenant={"tenant_id": "tenant-1"},
                principal_id="principal-1",
            ),
            raw_wire_payload={"idempotency_key": "read-replay-key"},
            response_type=None,
            work=work,
        )

    assert result["replayed"] is True
    work.assert_not_awaited()
    # Reads and writes carry different insert-rate ceilings (the spec's split
    # budget, idempotency_policy.py); this pins the reservation to the read
    # class so a future refactor can't silently apply the write budget to read
    # traffic without a test going red.
    assert reserve.call_args.kwargs["operation_class"] == "read"


def test_typed_read_replay_keeps_text_and_structured_content_identical() -> None:
    original = ToolResult(
        structured_content={
            "products": [{"product_id": "p-1"}],
            "context": {"correlation_id": "original"},
        }
    )
    replay = _decode_read_response(
        {"response": original.model_dump(mode="json")},
        ToolResult,
        {"correlation_id": "retry"},
    )

    assert replay.structured_content == {
        "products": [{"product_id": "p-1"}],
        "context": {"correlation_id": "retry"},
        "replayed": True,
    }
    assert replay.content[0].text == (
        '{"products":[{"product_id":"p-1"}],"context":{"correlation_id":"retry"},"replayed":true}'
    )


def test_same_key_and_hash_cannot_be_reused_across_tools() -> None:
    with pytest.raises(AdCPIdempotencyConflictError, match="different tool"):
        raise_on_tool_conflict("get_products", "list_creatives")


def test_same_tool_replay_is_not_a_tool_conflict() -> None:
    raise_on_tool_conflict("get_products", "get_products")


def test_read_scope_extracts_tenant_id_from_dict_tenant_fallback() -> None:
    """The isinstance(identity.tenant, dict) fallback is LIVE production code,
    not dead code: src/services/delivery_webhook_scheduler.py and
    src/core/resolved_identity.py both construct ResolvedIdentity with a raw
    dict ``tenant`` and no top-level ``tenant_id``. _read_scope must still
    resolve the tenant scope from that dict rather than raising.
    """
    identity = PrincipalFactory.make_identity(
        tenant_id=None,
        tenant={"tenant_id": "some-tenant"},
        principal_id="principal-1",
    )

    scope = _read_scope(identity)

    assert scope is not None
    assert scope[0] == "some-tenant"


@pytest.mark.asyncio
async def test_unresolvable_tenant_on_a_keyed_read_is_a_correctable_4xx_not_transient_503() -> None:
    """An unroutable subdomain / missing tenant context is PERMANENT, not
    transient — retrying can never succeed, so it must not wire as
    SERVICE_UNAVAILABLE/503/transient. src/core/auth.py's require_tenant
    raises AUTH_REQUIRED/correctable for the identical condition ("no tenant
    context available"); _read_scope now matches it.

    execute_idempotent_read (called from the A2A skill-handler wrapper,
    adcp_a2a_server.py's _execute_explicit_skill_handler, BEFORE the tool's
    own handler runs) is the real production call this exercises — not a
    reimplementation of it.
    """
    identity = PrincipalFactory.make_identity(tenant_id=None, tenant=None, principal_id="principal-1")

    with (
        patch("src.services.idempotency_replay.reserve_idempotent") as reserve,
        pytest.raises(AdCPAuthenticationError) as exc_info,
    ):
        await execute_idempotent_read(
            tool_name="get_adcp_capabilities",
            idempotency_key="k1",
            identity=identity,
            raw_wire_payload={"idempotency_key": "k1"},
            response_type=None,
            work=AsyncMock(),
        )

    assert exc_info.value.error_code == "AUTH_REQUIRED"
    assert exc_info.value.recovery == "correctable"
    reserve.assert_not_called()
