"""Tests for demand, source-activation and source-capacity constraints."""

from __future__ import annotations

from dataclasses import dataclass

import pulp
import pytest

from MILP.src.preprocessing import ModelParameters
from MILP.src.solver.constraints import add_constraints


Arc = tuple[str, str]


@dataclass
class ConstraintVariables:
    """Small variable container matching the shared model interface."""

    source_to_plant_flow: dict[Arc, pulp.LpVariable]
    plant_to_zone_flow: dict[Arc, pulp.LpVariable]
    source_active: dict[str, pulp.LpVariable]


def model_parameters(
    demand_by_zone: dict[str, float] | None = None,
    plant_zone_capacity: dict[Arc, float] | None = None,
) -> ModelParameters:
    """Create a complete formulation-ready parameter object for testing."""
    demand = demand_by_zone or {
        "Zone 1": 5.0,
        "Zone 2": 3.0,
    }

    zone_capacity = plant_zone_capacity or {
        ("Plant 1", "Zone 1"): 10.0,
        ("Plant 2", "Zone 2"): 6.0,
    }

    return ModelParameters(
        source_ids=("Source A", "Source B"),
        plant_ids=("Plant 1", "Plant 2"),
        zone_ids=("Zone 1", "Zone 2"),
        quality_parameter_ids=("turbidity",),
        source_plant_arcs=(
            ("Source A", "Plant 1"),
            ("Source B", "Plant 1"),
            ("Source B", "Plant 2"),
        ),
        plant_zone_arcs=(
            ("Plant 1", "Zone 1"),
            ("Plant 2", "Zone 2"),
        ),
        demand_by_zone=demand,
        source_fixed_cost={
            "Source A": 10.0,
            "Source B": 8.0,
        },
        plant_fixed_cost={
            "Plant 1": 5.0,
            "Plant 2": 5.0,
        },
        source_unit_cost={
            "Source A": 1.0,
            "Source B": 1.5,
        },
        plant_unit_treatment_cost={
            "Plant 1": 0.5,
            "Plant 2": 0.6,
        },
        source_min_withdrawal={
            "Source A": 2.0,
            "Source B": 1.0,
        },
        source_max_withdrawal={
            "Source A": 10.0,
            "Source B": 8.0,
        },
        plant_min_throughput={
            "Plant 1": 0.0,
            "Plant 2": 0.0,
        },
        plant_max_throughput={
            "Plant 1": 12.0,
            "Plant 2": 8.0,
        },
        source_plant_link_capacity={
            ("Source A", "Plant 1"): 10.0,
            ("Source B", "Plant 1"): 8.0,
            ("Source B", "Plant 2"): 8.0,
        },
        plant_zone_link_capacity=zone_capacity,
        source_quality={
            ("Source A", "turbidity"): 1.0,
            ("Source B", "turbidity"): 2.0,
        },
        quality_lower_bound={
            "turbidity": 0.0,
        },
        quality_upper_bound={
            "turbidity": 5.0,
        },
        quality_units={
            "turbidity": "NTU",
        },
        warnings=(),
    )


def make_variables(
    model: pulp.LpProblem,
    parameters: ModelParameters,
) -> ConstraintVariables:
    """Create the decision variables required by the constraint module."""
    source_to_plant_flow = {
        arc: model.add_variable(
            f"source_plant_flow_{index}",
            lowBound=0,
            upBound=parameters.source_plant_link_capacity[arc],
        )
        for index, arc in enumerate(parameters.source_plant_arcs)
    }

    plant_to_zone_flow = {
        arc: model.add_variable(
            f"plant_zone_flow_{index}",
            lowBound=0,
            upBound=parameters.plant_zone_link_capacity[arc],
        )
        for index, arc in enumerate(parameters.plant_zone_arcs)
    }

    source_active = {
        source_id: model.add_variable(
            f"source_active_{index}",
            cat=pulp.LpBinary,
        )
        for index, source_id in enumerate(parameters.source_ids)
    }

    return ConstraintVariables(
        source_to_plant_flow=source_to_plant_flow,
        plant_to_zone_flow=plant_to_zone_flow,
        source_active=source_active,
    )

def build_model(
    parameters: ModelParameters,
) -> tuple[pulp.LpProblem, ConstraintVariables]:
    """Build a small model containing the constraints under test."""
    model = pulp.LpProblem("Constraint_Test", pulp.LpMinimize)
    variables = make_variables(model, parameters)

    model += (
        pulp.lpSum(variables.source_to_plant_flow.values())
        + pulp.lpSum(variables.plant_to_zone_flow.values()),
        "Test_Objective",
    )

    add_constraints(model, parameters, variables)
    return model, variables


def solve(model: pulp.LpProblem) -> str:
    """Solve the test problem using HiGHS and return its status."""
    model.solve(pulp.HiGHS(msg=False))
    return pulp.LpStatus[model.status]


def test_expected_constraints_are_added() -> None:
    parameters = model_parameters()
    model, _ = build_model(parameters)

    assert {constraint.name for constraint in model.constraints()} == {
        "Demand_Satisfaction_0_Zone_1",
        "Demand_Satisfaction_1_Zone_2",
        "Source_Min_Withdrawal_0_Source_A",
        "Source_Max_Withdrawal_0_Source_A",
        "Source_Min_Withdrawal_1_Source_B",
        "Source_Max_Withdrawal_1_Source_B",
    }


def test_feasible_multi_zone_demand_is_optimal() -> None:
    model, _ = build_model(model_parameters())

    assert solve(model) == "Optimal"


def test_demand_is_enforced_separately_for_each_zone() -> None:
    parameters = model_parameters(
        demand_by_zone={
            "Zone 1": 5.0,
            "Zone 2": 4.0,
        },
        plant_zone_capacity={
            ("Plant 1", "Zone 1"): 10.0,
            ("Plant 2", "Zone 2"): 3.0,
        },
    )
    model, _ = build_model(parameters)

    assert solve(model) == "Infeasible"


def test_inactive_source_has_zero_outgoing_flow() -> None:
    parameters = model_parameters()
    model, variables = build_model(parameters)

    model += (
        variables.source_active["Source B"] == 0,
        "Force_Source_B_Inactive",
    )

    assert solve(model) == "Optimal"

    source_b_withdrawal = sum(
        pulp.value(variable)
        for arc, variable in variables.source_to_plant_flow.items()
        if arc[0] == "Source B"
    )
    assert source_b_withdrawal == pytest.approx(0.0)


def test_active_source_obeys_minimum_withdrawal() -> None:
    parameters = model_parameters()
    model, variables = build_model(parameters)

    model += (
        variables.source_active["Source A"] == 1,
        "Force_Source_A_Active",
    )

    assert solve(model) == "Optimal"

    source_a_withdrawal = sum(
        pulp.value(variable)
        for arc, variable in variables.source_to_plant_flow.items()
        if arc[0] == "Source A"
    )
    assert source_a_withdrawal >= 2.0 - 1e-7


def test_total_source_withdrawal_cannot_exceed_capacity() -> None:
    parameters = model_parameters()
    model, variables = build_model(parameters)

    source_b_flow = pulp.lpSum(
        variable
        for arc, variable in variables.source_to_plant_flow.items()
        if arc[0] == "Source B"
    )
    model += source_b_flow >= 9.0, "Force_Source_B_Above_Capacity"

    assert solve(model) == "Infeasible"


def test_missing_arc_variable_has_clear_error() -> None:
    parameters = model_parameters()
    model = pulp.LpProblem("Missing_Arc_Test")
    variables = make_variables(model, parameters)

    missing_arc = ("Source B", "Plant 2")
    variables.source_to_plant_flow.pop(missing_arc)

    with pytest.raises(KeyError, match="Source B.*Plant 2"):
        add_constraints(model, parameters, variables)


def test_missing_source_activation_variable_has_clear_error() -> None:
    parameters = model_parameters()
    model = pulp.LpProblem("Missing_Activation_Test")
    variables = make_variables(model, parameters)

    variables.source_active.pop("Source B")

    with pytest.raises(KeyError, match="Source B"):
        add_constraints(model, parameters, variables)