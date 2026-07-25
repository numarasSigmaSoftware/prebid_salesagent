"""Repository behavior for persistent strategy configuration."""

from unittest.mock import MagicMock

from src.core.database.models import Strategy
from src.core.database.repositories.strategy import StrategyRepository


def test_set_scenario_reassigns_json_config_for_dirty_tracking() -> None:
    session = MagicMock()
    repository = StrategyRepository(session)
    original_config = {"scenario": "normal", "other": "preserved"}
    strategy = Strategy(
        strategy_id="strategy-1",
        name="Test strategy",
        description="Repository unit test",
        config=original_config,
        is_simulation=True,
    )
    repository.get_by_id = MagicMock(return_value=strategy)  # type: ignore[method-assign]

    repository.set_scenario("strategy-1", "adapter_error")

    assert strategy.config == {"scenario": "adapter_error", "other": "preserved"}
    assert strategy.config is not original_config
