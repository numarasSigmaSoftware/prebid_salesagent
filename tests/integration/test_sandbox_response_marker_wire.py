"""#1874: update_media_buy must SAY when its response describes a sandbox buy.

Routing a sandbox buy to the mock adapter keeps the buyer's money safe (#1864); the
``sandbox`` marker is what lets the buyer tell a simulated result from a real one.
Without it an update against a sandbox account is indistinguishable on the wire from
one that really moved budget on the tenant's ad server.

AdCP 3.1.1 ``media-buy/advanced-topics/sandbox.mdx`` §Seller implementation and
§Protocol compliance both state it as a SHOULD: "Sellers SHOULD include
``sandbox: true`` in success responses when processing a sandbox account request."
Absent (not ``false``) is the correct encoding for a live response — the obligation is
to include it when true.

Graded on ``result.wire_response`` across every wire transport rather than on the typed
payload: the marker is a buyer-facing contract, so asserting the model would only prove
the model round-trips, and a transport that dropped the field on serialization would
still look correct.
"""

from __future__ import annotations

from datetime import date

import pytest

from tests.harness.transport import Transport, TransportResult

pytestmark = pytest.mark.requires_db

_WIRE_TRANSPORTS = (Transport.MCP, Transport.A2A, Transport.REST)


def _wire_sandbox_marker(result: TransportResult) -> bool | None:
    """The top-level ``sandbox`` marker as the buyer receives it.

    ``None`` means the key is absent, which is what a live response must look like.
    Fails loudly when no wire was captured rather than silently grading nothing.
    """
    assert not result.is_error, f"dispatch failed: {result.error!r}"
    wire = result.wire_response
    assert wire is not None, "no wire response captured — this assertion would grade nothing"
    return wire.get("sandbox")


def _seed_buy_on_account(env, *, sandbox: bool, suffix: str):
    """A media buy owned by an account whose mode is ``sandbox``.

    Factories only — no session.add()/get_db_session() in the test body
    (CLAUDE.md §Test Fixtures, enforced by test_architecture_repository_pattern).
    """
    from tests.factories import AccountFactory, AgentAccountAccessFactory, MediaBuyFactory

    tenant, principal, _product, _pricing = env.setup_media_buy_data()
    account_id = f"acc_{suffix}"
    AccountFactory(tenant=tenant, account_id=account_id, sandbox=sandbox)
    # Resolution enforces agent access; without the grant the request would fail
    # authorization long before the response is ever shaped.
    AgentAccountAccessFactory(
        tenant_id=tenant.tenant_id,
        principal_id=principal.principal_id,
        account_id=account_id,
    )
    buy = MediaBuyFactory(
        tenant=tenant,
        principal=principal,
        account_id=account_id,
        status="active",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
    )
    env._commit_factory_data()
    return buy


class TestUpdateMediaBuySandboxMarkerOnTheWire:
    """The mode is derived from the BUY's account, so no account reference is sent.

    ``update_media_buy`` is addressed by ``media_buy_id`` alone — ``identity.sandbox`` is
    structurally False on this path — which is exactly why the marker has to come from
    the same buy-derived value that chose the adapter, not from the request.
    """

    def _marker_for(self, transport, *, sandbox: bool):
        from src.core.schemas import UpdateMediaBuyRequest
        from tests.harness.media_buy_dual import MediaBuyDualEnv

        suffix = f"{transport.value}_{'sbx' if sandbox else 'live'}"
        with MediaBuyDualEnv() as env:
            buy = _seed_buy_on_account(env, sandbox=sandbox, suffix=suffix)
            # The dual env routes to the UPDATE wrappers only when it sees an
            # UpdateMediaBuyRequest; flat kwargs would dispatch to create.
            return _wire_sandbox_marker(
                env.call_via(
                    transport,
                    req=UpdateMediaBuyRequest(media_buy_id=buy.media_buy_id, paused=True),
                )
            )

    @pytest.mark.parametrize("transport", _WIRE_TRANSPORTS)
    def test_sandbox_buy_response_is_marked(self, integration_db, transport):
        assert self._marker_for(transport, sandbox=True) is True, (
            f"[{transport.value}] updating a sandbox buy must return sandbox=true; without it "
            "the buyer cannot tell a simulated update from one that moved real budget"
        )

    @pytest.mark.parametrize("transport", _WIRE_TRANSPORTS)
    def test_live_buy_response_is_not_marked(self, integration_db, transport):
        """Negative control — 'always mark' would pass the test above and mislabel real updates."""
        assert self._marker_for(transport, sandbox=False) is None, (
            f"[{transport.value}] a live response must omit the marker entirely; marking it "
            "would tell the buyer a real budget change was simulated"
        )
