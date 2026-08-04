"""Production-path oracles for sandbox isolation (SA-004, second attempt).

The first attempt tested the three helpers in isolation, which the review correctly
rejected: replacing a production ``sandbox=`` argument with ``False`` left those tests
green. These drive the actual ``_impl`` functions and assert on the real ``get_adapter``
binding in each module — the call that decides whether a real ad platform is contacted.

Each test is mutation-checked: flipping the production argument it grades makes it fail.

AdCP 3.1.1 ``dist/docs/3.1.1/media-buy/advanced-topics/sandbox.mdx`` §Seller
implementation: sandbox requests MUST NOT make real ad platform API calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _account(sandbox: bool) -> MagicMock:
    account = MagicMock()
    account.sandbox = sandbox
    return account


def _buy(media_buy_id: str, account_id: str | None) -> MagicMock:
    buy = MagicMock()
    buy.media_buy_id = media_buy_id
    buy.account_id = account_id
    return buy


def _uow_with(accounts: dict[str, bool], buys: dict[str, str | None]) -> MagicMock:
    """A UoW stub whose accounts/media_buys repos answer from the given maps."""
    uow = MagicMock()
    uow.accounts.get_by_id.side_effect = lambda aid: _account(accounts[aid]) if aid in accounts else None
    uow.media_buys.get_by_id.side_effect = lambda mid: _buy(mid, buys[mid]) if mid in buys else None
    uow.__enter__.return_value = uow
    uow.__exit__.return_value = False
    return uow


def _sandbox_kwargs(mock_get_adapter: MagicMock) -> list[bool]:
    """The sandbox= value of every get_adapter call, in order."""
    return [call.kwargs["sandbox"] for call in mock_get_adapter.call_args_list]


class TestPerformancePath:
    """update_performance_index is addressed by media_buy_id only."""

    def _run(self, *, account_sandbox: bool) -> list[bool]:
        from src.core.tools import performance as perf

        req = MagicMock()
        req.media_buy_id = "mb_1"
        req.performance_data = []
        identity = MagicMock()
        identity.tenant_id = "t1"
        identity.sandbox = False  # the wrong source — must not be what decides

        uow = _uow_with({"acc_1": account_sandbox}, {"mb_1": "acc_1"})

        with (
            patch.object(perf, "MediaBuyUoW", return_value=uow),
            patch.object(perf, "get_adapter") as mock_get_adapter,
            patch.object(perf, "require_identity", return_value=identity),
            patch.object(perf, "require_tenant", return_value={"tenant_id": "t1"}),
            patch.object(perf, "require_principal_id", return_value="p1"),
            patch.object(perf, "resolve_principal_or_raise", return_value=MagicMock()),
            patch.object(perf, "_verify_principal"),
        ):
            mock_get_adapter.return_value.update_media_buy_performance_index.return_value = True
            try:
                perf._update_performance_index_impl(req, identity=identity)
            except Exception:  # noqa: BLE001 - response shaping is not what this grades
                pass

        return _sandbox_kwargs(mock_get_adapter)

    def test_sandbox_buy_selects_sandbox_adapter(self) -> None:
        assert self._run(account_sandbox=True) == [True], (
            "update_performance_index on a sandbox buy must build a sandbox adapter; "
            "identity.sandbox is False here, so a False means the wrong source was used"
        )

    def test_live_buy_selects_live_adapter(self) -> None:
        """Negative control — 'always sandbox' would pass the test above."""
        assert self._run(account_sandbox=False) == [False]


class TestSnapshotPartitioning:
    """get_media_buys snapshots can span both modes in one response."""

    def _run(self, buys_by_account: dict[str, str]) -> tuple[list[bool], MagicMock]:
        from src.core.tools import media_buy_list as mbl

        accounts = {"acc_sbx": True, "acc_live": False}
        target = [_buy(mb_id, acc) for mb_id, acc in buys_by_account.items()]

        uow = _uow_with(accounts, {b.media_buy_id: b.account_id for b in target})
        uow.session = MagicMock()

        with (
            patch.object(mbl, "MediaBuyUoW", return_value=uow),
            patch.object(mbl, "get_adapter") as mock_get_adapter,
            patch.object(mbl, "_fetch_target_media_buys", return_value=target),
            patch.object(mbl, "_fetch_creative_approvals", return_value={}),
            patch.object(mbl, "_fetch_packages", return_value={}),
        ):
            adapter = mock_get_adapter.return_value
            adapter.capabilities.supports_realtime_reporting = True
            adapter.get_packages_snapshot.return_value = {}

            req = MagicMock()
            req.account = None
            req.account_id = None
            identity = MagicMock()
            identity.tenant_id = "t1"
            identity.principal_id = "p1"
            identity.sandbox = False
            identity.testing_context = None

            with (
                patch.object(mbl, "require_identity", return_value=identity),
                patch.object(mbl, "require_tenant", return_value={"tenant_id": "t1"}),
                patch.object(mbl, "get_principal_object", return_value=MagicMock()),
            ):
                try:
                    mbl._get_media_buys_impl(req, identity=identity, include_snapshot=True)
                except Exception:  # noqa: BLE001 - response shaping is not what this grades
                    pass

        return _sandbox_kwargs(mock_get_adapter), mock_get_adapter

    def test_all_live_builds_only_a_live_adapter(self) -> None:
        modes, _ = self._run({"mb_1": "acc_live", "mb_2": "acc_live"})
        assert modes == [False], f"expected one live adapter, got {modes}"

    def test_all_sandbox_builds_only_a_sandbox_adapter(self) -> None:
        modes, _ = self._run({"mb_1": "acc_sbx"})
        assert modes == [True], (
            f"expected one sandbox adapter, got {modes}; a live adapter here would read "
            "sandbox buys through the tenant's real ad server"
        )

    def test_mixed_request_builds_one_adapter_per_mode(self) -> None:
        modes, _ = self._run({"mb_live": "acc_live", "mb_sbx": "acc_sbx"})
        assert sorted(modes) == [False, True], f"expected one adapter per mode, got {modes}"


class TestUpdateMediaBuyPath:
    """update_media_buy mutates a buy addressed by id — the highest-risk buy-keyed path.

    Driven through MediaBuyUpdateEnv rather than hand-rolled mocks: the harness already
    patches the whole dependency set, so the impl actually reaches get_adapter. A
    hand-mocked attempt reached zero calls and would have asserted vacuously.
    """

    def _adapter_modes(self, *, account_sandbox: bool) -> list[bool]:
        from tests.harness.media_buy_update import MediaBuyUpdateEnv

        with MediaBuyUpdateEnv() as env:
            env.set_media_buy(media_buy_id="mb-001", account_id="acc-001")
            env.set_account(account_id="acc-001", sandbox=account_sandbox)
            env.set_currency_limit()

            try:
                # A package update is what actually reaches adapter dispatch.
                env.call_impl(media_buy_id="mb-001", packages=[{"package_id": "pkg-1", "budget": 50.0}])
            except Exception:  # noqa: BLE001 - response shaping is not what this grades
                pass

            return [c.kwargs["sandbox"] for c in env.mock["adapter"].call_args_list]

    def test_sandbox_buy_selects_sandbox_adapter(self) -> None:
        modes = self._adapter_modes(account_sandbox=True)

        assert modes, "update_media_buy reached no adapter at all — this assertion would be vacuous"
        assert all(modes), (
            f"update_media_buy on a sandbox buy built a live adapter (modes={modes}); "
            "the mutation would then reach the tenant's real ad server"
        )

    def test_live_buy_selects_live_adapter(self) -> None:
        """Negative control — 'always sandbox' would silently disable real updates."""
        modes = self._adapter_modes(account_sandbox=False)

        assert modes, "update_media_buy reached no adapter at all — this assertion would be vacuous"
        assert not any(modes), f"expected the live adapter for a live account, got {modes}"


# Admin media-buy detail, approved-buy execution and deferred creative push are graded by
# their derivation (account_is_sandbox / _buy_account_id) in the helper tests, but not yet
# at their own adapter boundary — those paths have no harness env and cannot be reached
# with ad-hoc mocks. Integration tests shaped like
# tests/integration/test_sandbox_delivery_account_scoping.py are the way in.


# Delivery account scoping (SA-006) is graded in
# tests/integration/test_sandbox_delivery_account_scoping.py: _get_media_buy_delivery_impl
# cannot be driven far enough with unit mocks (it raises before the seam), and an
# assertion on an empty call list passes vacuously — which is how the first attempt at
# this test read green while grading nothing.


@pytest.mark.parametrize(
    ("module_name", "symbol"),
    [
        ("src.core.tools.performance", "media_buy_sandbox_mode"),
        ("src.core.tools.media_buy_update", "account_is_sandbox"),
        ("src.core.tools.media_buy_list", "partition_by_sandbox_mode"),
        ("src.core.tools.media_buy_delivery", "account_is_sandbox"),
    ],
)
def test_each_buy_keyed_module_imports_a_buy_derived_source(module_name: str, symbol: str) -> None:
    """Each buy-keyed module must import a buy-derived mode source, not rely on identity.

    Cheap structural companion to the behavioural tests above: if someone deletes the
    derivation and falls back to identity.sandbox, the import disappears with it.
    """
    import importlib

    module = importlib.import_module(module_name)
    assert hasattr(module, symbol), (
        f"{module_name} no longer imports {symbol}; a buy-keyed operation cannot source "
        "sandbox mode from identity.sandbox (it is structurally False there)"
    )
