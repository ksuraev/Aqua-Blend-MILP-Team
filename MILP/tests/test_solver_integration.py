"""Tests for HiGHS configuration, execution and input adaptation."""

from __future__ import annotations

from dataclasses import dataclass

import pulp
import pytest

from MILP.src.solver.config import SolverConfig
from MILP.src.solver.input_schema import SolverInput
from MILP.src.solver.solve import solve_model


def make_model(demand: float = 7.0, capacity: float = 10.0):
    model = pulp.LpProblem("Solver_Integration_Test", pulp.LpMinimize)
    flow = {"A": pulp.LpVariable("flow_A", lowBound=0)}
    active = {"A": pulp.LpVariable("active_A", cat=pulp.LpBinary)}
    model += flow["A"] + active["A"], "Total_Cost"
    model += flow["A"] >= demand, "Demand"
    model += flow["A"] <= capacity * active["A"], "Activation_Capacity"
    return model, {"flow": flow, "active": active}


def test_optimal_model_returns_structured_solution() -> None:
    model, variables = make_model()
    result = solve_model(model, variables)
    assert result.status == "Optimal"
    assert result.has_solution is True
    assert result.objective_value == pytest.approx(8.0)
    assert result.source_flows == {"A": pytest.approx(7.0)}
    assert result.source_activation == {"A": 1}


def test_infeasible_model_does_not_report_values() -> None:
    model, variables = make_model(demand=11.0, capacity=10.0)
    result = solve_model(model, variables)
    assert result.status == "Infeasible"
    assert result.has_solution is False
    assert result.objective_value is None
    assert result.source_flows == {}


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (SolverConfig(time_limit_seconds=0), "time_limit_seconds"),
        (SolverConfig(relative_mip_gap=-0.1), "relative_mip_gap"),
        (SolverConfig(relative_mip_gap=1.1), "relative_mip_gap"),
        (SolverConfig(threads=0), "threads"),
        (SolverConfig(random_seed=-1), "random_seed"),
    ],
)
def test_invalid_configuration_is_rejected(config: SolverConfig, message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        config.validate()


def test_missing_variable_group_has_clear_error() -> None:
    model, variables = make_model()
    variables.pop("active")
    with pytest.raises(KeyError, match="active"):
        solve_model(model, variables)


def test_model_parameters_adapter_matches_team_fields() -> None:
    @dataclass(frozen=True)
    class ModelParameters:
        source_ids: tuple[str, ...]
        plant_ids: tuple[str, ...]
        quality_parameter_ids: tuple[str, ...]
        demand_by_zone: dict[str, float]
        source_fixed_cost: dict[str, float]
        source_unit_cost: dict[str, float]
        source_max_withdrawal: dict[str, float]
        plant_max_throughput: dict[str, float]
        plant_unit_treatment_cost: dict[str, float]
        source_quality: dict[tuple[str, str], float]

    parameters = ModelParameters(
        source_ids=("A",),
        plant_ids=("P",),
        quality_parameter_ids=("turbidity",),
        demand_by_zone={"Z": 5.0},
        source_fixed_cost={"A": 2.0},
        source_unit_cost={"A": 3.0},
        source_max_withdrawal={"A": 10.0},
        plant_max_throughput={"P": 15.0},
        plant_unit_treatment_cost={"P": 1.0},
        source_quality={("A", "turbidity"): 2.5},
    )
    adapted = SolverInput.from_model_parameters(parameters)
    assert adapted.reservoirs[0].name == "A"
    assert adapted.reservoirs[0].minimum_flow_if_active == 0.0
    assert adapted.demand.required_volume == 5.0
    assert adapted.plant.capacity == 15.0
