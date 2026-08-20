"""Integration pin: a claimed update lease is resolved on EVERY exit path.

``_update_media_buy_impl`` claims a durable update lease before its first adapter
call and immediately marks ``update_adapter_invoked_at``. Fourteen ``raise``
statements sit between that claim and the end of the body, and the lease used to be
released only by explicit calls at the return sites — so a raise on ANY of them
leaked the claim. Once the lease TTL passes, ``claim_update_lease`` refuses to claim
while ``update_adapter_invoked_at`` is set, marking the buy for manual
reconciliation: every further update on that buy is fenced PERMANENTLY, with no
reconciler and no operator surface to clear it.

The pre-adapter arm is already pinned by
``test_media_buy_v3.py::test_pre_adapter_budget_rejection_leaves_no_update_lease``.
That test cannot catch this: it rejects BEFORE ``prepare_adapter_call()`` runs, so
no lease is ever claimed and no ``finally`` is needed to pass it. This module pins
the POST-adapter arm — the only arm the guard exists for.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


def _post_adapter_raise_request(media_buy_id: str, real_package_id: str):
    """Build an update whose FIRST package invokes the adapter and whose SECOND raises.

    Package 1 carries ``paused``, which reaches ``prepare_adapter_call()`` — the lease
    is claimed and ``update_adapter_invoked_at`` is stamped — and then the adapter.
    Package 2 names a package that does not exist on the buy, so
    ``get_package_or_raise`` raises PACKAGE_NOT_FOUND with the lease already held.
    """
    from src.core.schemas import UpdateMediaBuyRequest

    return UpdateMediaBuyRequest(
        media_buy_id=media_buy_id,
        packages=[
            {"package_id": real_package_id, "paused": True},
            {"package_id": "pkg_does_not_exist", "budget": 1000},
        ],
    )


class TestUpdateLeaseAlwaysResolved:
    """A raise AFTER the adapter call must leave the buy updatable, not fenced."""

    def test_post_adapter_raise_leaves_the_buy_updatable(self, env_with_media_buy):
        """A buyer-input rejection raised after the adapter call resolves the lease.

        Two independent oracles, because either alone can pass for the wrong reason:

        1. The lease columns are cleared — ``complete_update_lease`` ran.
        2. A corrected retry SUCCEEDS. This is the buyer-visible consequence: with a
           leaked lease the retry gets CONFLICT ("a previous update is still running
           or requires reconciliation") from ``claim_update_lease``, forever.
        """
        from src.core.database.repositories import MediaBuyUoW
        from src.core.exceptions import AdCPError
        from src.core.schemas import UpdateMediaBuyRequest

        env, media_buy = env_with_media_buy
        media_buy_id = media_buy.media_buy_id
        package_id = env._seeded_package.package_id
        tenant_id = env._owner_tenant.tenant_id

        with pytest.raises(AdCPError) as exc_info:
            env.call_impl(req=_post_adapter_raise_request(media_buy_id, package_id))
        assert exc_info.value.error_code == "PACKAGE_NOT_FOUND", (
            "the scenario must reject on the SECOND package, after the first package's "
            f"adapter call claimed the lease; got {exc_info.value.error_code}"
        )

        with MediaBuyUoW(tenant_id) as uow:
            assert uow.media_buys is not None
            row = uow.media_buys.get_by_id(media_buy_id)
            assert row is not None
            assert row.update_lease_id is None, "the update lease was leaked by the post-adapter raise"
            assert row.update_lease_expires_at is None
            assert row.update_adapter_invoked_at is None, (
                "update_adapter_invoked_at survived the raise — the next claim will refuse "
                "and fence this buy for manual reconciliation permanently"
            )

        # The buyer-visible consequence: the corrected retry must go through.
        retry = env.call_impl(req=UpdateMediaBuyRequest(media_buy_id=media_buy_id, paused=True))
        assert retry.status == "completed", f"a corrected retry was fenced by the leaked lease: {retry!r}"
