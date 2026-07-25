"""Factory for persisted strategy definitions."""

import factory

from src.core.database.models import Strategy


class StrategyFactory(factory.alchemy.SQLAlchemyModelFactory):
    class Meta:
        model = Strategy
        sqlalchemy_session = None
        sqlalchemy_session_persistence = "commit"

    strategy_id = factory.Sequence(lambda n: f"strategy_{n:04d}")
    name = factory.LazyAttribute(lambda strategy: f"Test Strategy {strategy.strategy_id}")
    description = "Integration-test strategy"
    config = factory.LazyFunction(lambda: {"scenario": "normal"})
    is_simulation = True
