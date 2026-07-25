"""Repository for strategy definitions and persistent simulation state."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.core.database.models import Strategy, StrategyState


class StrategyRepository:
    """Access strategy definitions and their associated simulation state."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, strategy_id: str) -> Strategy | None:
        """Return a strategy by its globally unique ID."""
        return self._session.scalars(select(Strategy).where(Strategy.strategy_id == strategy_id)).first()

    def create(self, strategy: Strategy) -> Strategy:
        """Add a new strategy to the active unit of work."""
        self._session.add(strategy)
        self._session.flush()
        return strategy

    def list_states(self, strategy_id: str) -> list[StrategyState]:
        """Return all persisted state entries for a strategy."""
        return list(self._session.scalars(select(StrategyState).where(StrategyState.strategy_id == strategy_id)).all())

    def upsert_state(self, strategy_id: str, key: str, value: dict[str, Any]) -> None:
        """Create or update one persisted simulation state entry."""
        state = self._session.scalars(
            select(StrategyState).where(
                StrategyState.strategy_id == strategy_id,
                StrategyState.state_key == key,
            )
        ).first()
        if state is None:
            self._session.add(StrategyState(strategy_id=strategy_id, state_key=key, state_value=value))
            return
        state.state_value = value
        state.updated_at = datetime.now(UTC)

    def clear_states(self, strategy_id: str) -> None:
        """Delete all persisted simulation state for a strategy."""
        self._session.execute(delete(StrategyState).where(StrategyState.strategy_id == strategy_id))

    def set_scenario(self, strategy_id: str, scenario: str) -> None:
        """Update the scenario value when the strategy exists."""
        strategy = self.get_by_id(strategy_id)
        if strategy is not None:
            # JSONType is plain JSONB rather than MutableDict, so an in-place
            # mutation is invisible to SQLAlchemy's dirty tracking.
            strategy.config = {**(strategy.config or {}), "scenario": scenario}
