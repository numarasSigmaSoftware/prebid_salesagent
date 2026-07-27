"""Repository for strategy definitions and persistent simulation state."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.core.database.models import Strategy, StrategyState


class StrategyRepository:
    """Access strategy definitions and their associated simulation state."""

    def __init__(self, session: Session, tenant_id: str, principal_id: str | None) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._principal_id = principal_id

    def _owned_strategy(self, strategy_id: str) -> tuple[Any, Any, Any]:
        """Return the ownership predicates shared by strategy operations."""
        return (
            Strategy.strategy_id == strategy_id,
            Strategy.tenant_id == self._tenant_id,
            Strategy.principal_id == self._principal_id,
        )

    def get_by_id(self, strategy_id: str) -> Strategy | None:
        """Return a strategy only when it belongs to this repository scope."""
        return self._session.scalars(select(Strategy).where(*self._owned_strategy(strategy_id))).first()

    def create(self, strategy: Strategy) -> Strategy:
        """Add a strategy whose ownership matches this repository scope."""
        if strategy.tenant_id != self._tenant_id or strategy.principal_id != self._principal_id:
            raise ValueError("strategy ownership must match the repository scope")
        self._session.add(strategy)
        self._session.flush()
        return strategy

    def list_states(self, strategy_id: str) -> list[StrategyState]:
        """Return state only when the strategy belongs to this repository scope."""
        return list(
            self._session.scalars(
                select(StrategyState)
                .join(Strategy, Strategy.strategy_id == StrategyState.strategy_id)
                .where(*self._owned_strategy(strategy_id))
            ).all()
        )

    def upsert_state(self, strategy_id: str, key: str, value: dict[str, Any]) -> None:
        """Create or update state for a strategy owned by this scope."""
        if self.get_by_id(strategy_id) is None:
            raise ValueError("strategy does not belong to the repository scope")
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
        """Delete state only when the strategy belongs to this scope."""
        owned_strategy_id = select(Strategy.strategy_id).where(*self._owned_strategy(strategy_id)).scalar_subquery()
        self._session.execute(delete(StrategyState).where(StrategyState.strategy_id == owned_strategy_id))

    def set_scenario(self, strategy_id: str, scenario: str) -> None:
        """Update the scenario value when the strategy exists."""
        strategy = self.get_by_id(strategy_id)
        if strategy is not None:
            # JSONType is plain JSONB rather than MutableDict, so an in-place
            # mutation is invisible to SQLAlchemy's dirty tracking.
            strategy.config = {**(strategy.config or {}), "scenario": scenario}
