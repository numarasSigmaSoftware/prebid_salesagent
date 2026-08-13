"""Integration wire pins: schema-invalid filter fields on get_media_buys.

``GetMediaBuysBody`` (src/routes/api_v1.py) uses ``SkipValidation`` around the
concrete AdCP shapes (``list[str] | None`` /
``MediaBuyStatus | list[MediaBuyStatus] | None``). This preserves a malformed
wire value until the shared
``GetMediaBuysRequest`` validation boundary every transport goes through.
Staying ``Any`` lets the raw value reach that SAME shared boundary, so all
three converge on ``INVALID_REQUEST`` instead of diverging (contrast with ``revision`` on
``update_media_buy``, which IS concretely typed and accepts that divergence
for a different reason — see test_update_media_buy_revision_validation_wire.py).

``context`` and ``account`` do not carry this deviation (typed concretely,
per the sibling REST *Body models) because they have no such cross-transport
classification split to protect.

These tests pin the claim that the raw-value-preserving typing decision delivers
parity, not just that it exists: if a future edit types ``media_buy_ids``/
``status_filter`` concretely (reintroducing the split this decision exists to
avoid), the REST case here goes red instead of drifting silently. The A2A case
was previously pinned alone, for a different invariant (suggestion presence,
tests/integration/test_request_validation_suggestion_parity.py); this file
completes the claim across all three wire transports.
"""

from __future__ import annotations

import pytest

from tests.factories import PrincipalFactory, TenantFactory
from tests.harness.media_buy_list import MediaBuyListEnv
from tests.harness.transport import Transport

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

_WIRE_TRANSPORTS = [Transport.A2A, Transport.MCP, Transport.REST]


@pytest.mark.requires_db
class TestGetMediaBuysFilterValidationWire:
    """A wrong-typed ``media_buy_ids``/``status_filter`` must emit INVALID_REQUEST
    identically on every wire transport — proving the raw-value-preserving typing in
    ``GetMediaBuysBody`` achieves parity rather than merely claiming to."""

    @pytest.mark.parametrize("transport", _WIRE_TRANSPORTS, ids=lambda t: t.value)
    def test_wrong_type_media_buy_ids_emits_invalid_request_on_every_transport(self, integration_db, transport):
        """media_buy_ids as a bare string (not an array) reaches the shared
        GetMediaBuysRequest boundary on every transport -> INVALID_REQUEST/correctable.

        If GetMediaBuysBody drops ``SkipValidation``, FastAPI would reject this during
        REST body parsing -> INVALID_REQUEST, and
        only the REST parametrization here reddens — pinning the split, not just
        this one transport's happy case.
        """
        with MediaBuyListEnv(tenant_id="t1", principal_id="p1") as env:
            tenant = TenantFactory(tenant_id="t1")
            PrincipalFactory(tenant=tenant, principal_id="p1")

            result = env.call_via(transport, media_buy_ids="not-a-list")

            assert result.is_error, (
                f"{transport.value}: malformed media_buy_ids must be rejected, got success payload: {result.payload!r}"
            )
            result.assert_wire_error("INVALID_REQUEST", recovery="correctable")

    @pytest.mark.parametrize("transport", _WIRE_TRANSPORTS, ids=lambda t: t.value)
    def test_wrong_type_status_filter_emits_invalid_request_on_every_transport(self, integration_db, transport):
        """status_filter as an invalid-shaped value reaches the shared
        GetMediaBuysRequest boundary on every transport -> INVALID_REQUEST/correctable.
        Same split risk as media_buy_ids if this field is ever typed concretely.
        """
        with MediaBuyListEnv(tenant_id="t2", principal_id="p2") as env:
            tenant = TenantFactory(tenant_id="t2")
            PrincipalFactory(tenant=tenant, principal_id="p2")

            result = env.call_via(transport, status_filter={"not": "a-valid-status-shape"})

            assert result.is_error, (
                f"{transport.value}: malformed status_filter must be rejected, got success payload: {result.payload!r}"
            )
            result.assert_wire_error("INVALID_REQUEST", recovery="correctable")
