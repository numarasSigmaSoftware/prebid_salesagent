"""PostgreSQL persistence coverage for the strategy repository."""

import pytest

from src.core.database.repositories.uow import StrategyUoW
from tests.factories import PrincipalFactory, StrategyFactory, TenantFactory
from tests.harness._base import IntegrationEnv

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


class _StrategyRepoEnv(IntegrationEnv):
    """Bind factories to the integration database without external patches."""

    EXTERNAL_PATCHES: dict[str, str] = {}


def test_set_scenario_persists_json_config_across_uows(integration_db) -> None:
    with _StrategyRepoEnv():
        tenant = TenantFactory(tenant_id="strategy_roundtrip_tenant")
        principal = PrincipalFactory(tenant=tenant, principal_id="strategy_roundtrip_principal")
        StrategyFactory(
            strategy_id="strategy_scenario_roundtrip",
            tenant_id=tenant.tenant_id,
            principal_id=principal.principal_id,
            config={"scenario": "normal", "preserved": "sibling-value"},
        )
        tenant_id = tenant.tenant_id
        principal_id = principal.principal_id

    with StrategyUoW(tenant_id, principal_id) as uow:
        assert uow.strategies is not None
        uow.strategies.set_scenario("strategy_scenario_roundtrip", "adapter_error")

    with StrategyUoW(tenant_id, principal_id) as uow:
        assert uow.strategies is not None
        reloaded = uow.strategies.get_by_id("strategy_scenario_roundtrip")
        assert reloaded is not None
        assert reloaded.config == {
            "scenario": "adapter_error",
            "preserved": "sibling-value",
        }


def test_strategy_and_state_operations_reject_cross_tenant_scope(integration_db) -> None:
    with _StrategyRepoEnv():
        owner_tenant = TenantFactory(tenant_id="strategy_owner_tenant")
        owner = PrincipalFactory(tenant=owner_tenant, principal_id="strategy_owner")
        other_tenant = TenantFactory(tenant_id="strategy_other_tenant")
        other = PrincipalFactory(tenant=other_tenant, principal_id="strategy_other")
        StrategyFactory(
            strategy_id="sim_happy_path",
            tenant_id=owner_tenant.tenant_id,
            principal_id=owner.principal_id,
        )
        owner_tenant_id = owner_tenant.tenant_id
        owner_principal_id = owner.principal_id
        other_tenant_id = other_tenant.tenant_id
        other_principal_id = other.principal_id

    with StrategyUoW(owner_tenant_id, owner_principal_id) as uow:
        assert uow.strategies is not None
        uow.strategies.upsert_state("sim_happy_path", "current_time", {"time": "owner-value"})

    with StrategyUoW(other_tenant_id, other_principal_id) as uow:
        assert uow.strategies is not None
        assert uow.strategies.get_by_id("sim_happy_path") is None
        assert uow.strategies.list_states("sim_happy_path") == []
        uow.strategies.set_scenario("sim_happy_path", "attacker-value")
        uow.strategies.clear_states("sim_happy_path")
        with pytest.raises(ValueError, match="does not belong"):
            uow.strategies.upsert_state("sim_happy_path", "current_time", {"time": "attacker-value"})

    with StrategyUoW(owner_tenant_id, owner_principal_id) as uow:
        assert uow.strategies is not None
        strategy = uow.strategies.get_by_id("sim_happy_path")
        assert strategy is not None
        assert strategy.config == {"scenario": "normal"}
        states = uow.strategies.list_states("sim_happy_path")
        assert [(state.state_key, state.state_value) for state in states] == [("current_time", {"time": "owner-value"})]
