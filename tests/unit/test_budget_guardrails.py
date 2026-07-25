"""Unit tests for PR04 financial guardrails: F-05, F-07, F-08.

F-05 — Budget ceiling: updates exceeding MAX_CAMPAIGN_BUDGET are rejected.
F-07 — Currency preservation: float-only budget updates use existing DB currency.
F-08 — Min-spend parity: package budget updates honor currency_limit.min_package_budget.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest

from src.core.exceptions import AdCPBudgetExceededError, AdCPBudgetTooLowError, AdCPConflictError
from src.core.schemas import Budget, UpdateMediaBuySuccess
from src.core.tools.media_buy_update import MAX_CAMPAIGN_BUDGET, MEDIA_BUY_UPDATE_LEASE_TTL_SECONDS
from tests.harness.media_buy_update import MediaBuyUpdateEnv

# ---------------------------------------------------------------------------
# F-05: Budget ceiling
# ---------------------------------------------------------------------------


def test_max_campaign_budget_constant_is_ten_million() -> None:
    """Default ceiling must be 10,000,000."""
    assert MAX_CAMPAIGN_BUDGET == Decimal("10000000")


def test_extreme_budget_rejected() -> None:
    """Budget exceeding MAX_CAMPAIGN_BUDGET must raise AdCPBudgetExceededError."""
    with MediaBuyUpdateEnv() as env:
        with pytest.raises(AdCPBudgetExceededError):
            env.call_impl(budget=Budget(total=888_888_888, currency="USD"))


# ---------------------------------------------------------------------------
# F-05: Constant is configurable via env var
# ---------------------------------------------------------------------------


def test_max_campaign_budget_env_override(monkeypatch) -> None:
    """MAX_CAMPAIGN_BUDGET should reflect MAX_CAMPAIGN_BUDGET_USD env var."""
    import importlib

    monkeypatch.setenv("MAX_CAMPAIGN_BUDGET_USD", "5000000")
    import src.core.tools.media_buy_update as mod

    importlib.reload(mod)
    assert mod.MAX_CAMPAIGN_BUDGET == Decimal("5000000")
    # Restore
    importlib.reload(mod)


# ---------------------------------------------------------------------------
# F-08: Min-spend parity via CurrencyLimitRepository
# ---------------------------------------------------------------------------


def test_package_budget_uses_currency_limit_repository() -> None:
    """Package min-spend validation must go through uow.currency_limits, not raw session selects."""
    with MediaBuyUpdateEnv() as env:
        env.set_media_buy(currency="EUR")
        env.set_currency_limit(min_package_budget=Decimal("100"))

        with pytest.raises(AdCPBudgetTooLowError):
            env.call_impl(packages=[{"package_id": "pkg-1", "budget": 50.0}])

        env.mock["uow"].return_value.currency_limits.get_for_currency.assert_called_with("EUR")
        env.mock["uow"].return_value.session.scalars.assert_not_called()


def test_invalid_update_does_not_claim_lease_and_a_corrected_retry_proceeds() -> None:
    """Validation before an adapter call must not leave a durable retry lockout."""
    with MediaBuyUpdateEnv() as env:
        media_buy = env.set_media_buy(currency="EUR")
        media_buy.revision = 1
        env.set_currency_limit(min_package_budget=Decimal("100"))

        with pytest.raises(AdCPBudgetTooLowError):
            env.call_impl(packages=[{"package_id": "pkg-1", "budget": 50.0}])

        media_buy_repo = env.mock["uow"].return_value.media_buys
        media_buy_repo.claim_update_lease.assert_not_called()

        env.mock["adapter"].return_value.update_media_buy.return_value = UpdateMediaBuySuccess(
            media_buy_id="mb-001", affected_packages=[]
        )
        media_buy_repo.claim_update_lease.return_value = "lease_retry"
        media_buy_repo.mark_update_adapter_invoked.return_value = True
        media_buy_repo.complete_update_lease.return_value = True

        result = env.call_impl(paused=True)

        assert isinstance(result.response, UpdateMediaBuySuccess)
        media_buy_repo.claim_update_lease.assert_called_once_with(
            "mb-001", lease_ttl_seconds=MEDIA_BUY_UPDATE_LEASE_TTL_SECONDS
        )


def test_expired_completion_rolls_back_before_persisting_manual_fence() -> None:
    """A completion conflict must not commit staged local update data."""
    with MediaBuyUpdateEnv() as env:
        media_buy = env.set_media_buy()
        media_buy.revision = 1
        media_buy_repo = env.mock["uow"].return_value.media_buys
        media_buy_repo.claim_update_lease.return_value = "lease_expired"
        media_buy_repo.mark_update_adapter_invoked.return_value = True
        media_buy_repo.complete_update_lease.return_value = False
        env.mock["adapter"].return_value.update_media_buy.return_value = UpdateMediaBuySuccess(
            media_buy_id="mb-001", affected_packages=[]
        )
        events: list[str] = []
        env.mock["uow"].return_value.rollback.side_effect = lambda: events.append("rollback")

        with patch(
            "src.core.tools.media_buy_update._persist_expired_update_lease_reconciliation",
            side_effect=lambda *_: events.append("persist_fence"),
        ) as persist_fence:
            with pytest.raises(AdCPConflictError):
                env.call_impl(paused=True)

        env.mock["uow"].return_value.rollback.assert_called_once_with()
        persist_fence.assert_called_once_with(env.identity.tenant_id, "mb-001", "lease_expired")
        assert events == ["rollback", "persist_fence"]
