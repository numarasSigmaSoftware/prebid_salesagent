"""PostgreSQL persistence coverage for the strategy repository."""

import pytest

from src.core.database.repositories.uow import StrategyUoW
from tests.factories import StrategyFactory
from tests.harness._base import IntegrationEnv

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


class _StrategyRepoEnv(IntegrationEnv):
    """Bind factories to the integration database without external patches."""

    EXTERNAL_PATCHES: dict[str, str] = {}


def test_set_scenario_persists_json_config_across_uows(integration_db) -> None:
    with _StrategyRepoEnv():
        StrategyFactory(
            strategy_id="strategy_scenario_roundtrip",
            config={"scenario": "normal", "preserved": "sibling-value"},
        )

    with StrategyUoW() as uow:
        assert uow.strategies is not None
        uow.strategies.set_scenario("strategy_scenario_roundtrip", "adapter_error")

    with StrategyUoW() as uow:
        assert uow.strategies is not None
        reloaded = uow.strategies.get_by_id("strategy_scenario_roundtrip")
        assert reloaded is not None
        assert reloaded.config == {
            "scenario": "adapter_error",
            "preserved": "sibling-value",
        }
