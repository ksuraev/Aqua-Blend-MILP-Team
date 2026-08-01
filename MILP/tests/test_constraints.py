"""Tests for demand, source-activation, and source-capacity constraints."""

from __future__ import annotations

from dataclasses import dataclass

import pulp
import pytest

from MILP.src.solver.constraints import add_constraints


@dataclass(frozen=True)
class Reservoir:
    name: str
    minimum_flow_if_active: float
    max_flow: float


@dataclass(frozen=True)
class Demand:
    required_volume: float


@dataclass(frozen=True)
class SolverInput:
    reservoirs: tuple[Reservoir, ...]
    demand: Demand


@dataclass(frozen=True)
class ModelParameters:
    source_ids: tuple[str, ...]
    demand_by_zone: dict[str, float]
    source_min_withdrawal: dict[str, float]
    source_max_withdrawal: dict[str, float]


def solver_input(demand: float = 12.0) -> SolverInput:
    return SolverInput(
        reservoirs=(
            Reservoir("Reservoir A", 2.0, 10.0),
            Reservoir("Reservoir B", 1.0, 8.0),
            Reservoir("Reservoir C", 0.0, 6.0),
        ),
        demand=Demand(demand),
    )


def model_parameters(demand: float = 12.0) -> ModelParameters:
    return ModelParameters(
        source_ids=("Reservoir A", "Reservoir B", "Reservoir C"),
        demand_by_zone={"Zone 1": demand},
        source_min_withdrawal={
            "Reservoir A": 2.0,
            "Reservoir B": 1.0,
            "Reservoir C": 0.0,
        },
        source_max_withdrawal={
            "Reservoir A": 10.0,
            "Reservoir B": 8.0,
            "Reservoir C": 6.0,
        },
    )


def make_variables(source_names: tuple[str, ...]) -> dict[str, dict[str, pulp.LpVariable]]:
    return {
        "flow": {
            source: pulp.LpVariable(f"flow_{index}", lowBound=0)
            for index, source in enumerate(source_names)
        },
        "active": {
            source: pulp.LpVariable(f"active_{index}", cat=pulp.LpBinary)
            for index, source in enumerate(source_names)
        },
    }


def build_model(data: object) -> tuple[pulp.LpProblem, dict[str, dict[str, pulp.LpVariable]]]:
    names = (
        data.source_ids
        if hasattr(data, "source_ids")
        else tuple(reservoir.name for reservoir in data.reservoirs)
    )
    variables = make_variables(names)
    model = pulp.LpProblem("Constraint_Test", pulp.LpMinimize)
    model += pulp.lpSum(variables["flow"].values()), "Test_Objective"
    add_constraints(model, data, variables)
    return model, variables


def solve(model: pulp.LpProblem) -> str:
    model.solve(pulp.HiGHS(msg=False))
    return pulp.LpStatus[model.status]


@pytest.mark.parametrize("data", [solver_input(), model_parameters()])
def test_supports_both_team_input_interfaces(data: object) -> None:
    model, _ = build_model(data)
    assert solve(model) == "Optimal"
    assert "Demand_Satisfaction" in model.constraints


def test_expected_constraints_are_added() -> None:
    model, _ = build_model(model_parameters())
    assert set(model.constraints) == {
        "Demand_Satisfaction",
        "Source_Min_Withdrawal_Reservoir_A",
        "Source_Max_Withdrawal_Reservoir_A",
        "Source_Min_Withdrawal_Reservoir_B",
        "Source_Max_Withdrawal_Reservoir_B",
        "Source_Min_Withdrawal_Reservoir_C",
        "Source_Max_Withdrawal_Reservoir_C",
    }


def test_inactive_source_has_zero_flow() -> None:
    data = model_parameters(5.0)
    model, variables = build_model(data)
    model += variables["active"]["Reservoir B"] == 0, "Force_B_Inactive"
    assert solve(model) == "Optimal"
    assert pulp.value(variables["flow"]["Reservoir B"]) == pytest.approx(0.0)


def test_active_source_obeys_minimum_withdrawal() -> None:
    data = model_parameters(2.0)
    model, variables = build_model(data)
    model += variables["active"]["Reservoir A"] == 1, "Force_A_Active"
    assert solve(model) == "Optimal"
    assert pulp.value(variables["flow"]["Reservoir A"]) >= 2.0 - 1e-7


def test_demand_above_total_capacity_is_infeasible() -> None:
    model, _ = build_model(model_parameters(25.0))
    assert solve(model) == "Infeasible"


def test_current_preprocessing_without_minimum_field_defaults_to_zero() -> None:
    @dataclass(frozen=True)
    class CurrentModelParameters:
        source_ids: tuple[str, ...]
        demand_by_zone: dict[str, float]
        source_max_withdrawal: dict[str, float]

    data = CurrentModelParameters(
        source_ids=("A",),
        demand_by_zone={"Z": 3.0},
        source_max_withdrawal={"A": 5.0},
    )
    model, _ = build_model(data)
    assert solve(model) == "Optimal"


def test_missing_variable_has_clear_error() -> None:
    data = model_parameters()
    variables = make_variables(data.source_ids)
    variables["flow"].pop("Reservoir C")
    with pytest.raises(KeyError, match="Reservoir C"):
        add_constraints(pulp.LpProblem("Missing"), data, variables)


def test_invalid_source_bounds_are_rejected() -> None:
    data = ModelParameters(
        source_ids=("A",),
        demand_by_zone={"Z": 1.0},
        source_min_withdrawal={"A": 11.0},
        source_max_withdrawal={"A": 10.0},
    )
    with pytest.raises(ValueError, match="minimum withdrawal"):
        build_model(data)
