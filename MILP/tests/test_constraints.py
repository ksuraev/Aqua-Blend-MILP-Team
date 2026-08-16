"""Component tests for the AquaBlend Pyomo constraints."""

import pyomo.environ as pyo
import pytest

from src.model import build_model
from src.preprocessing import ModelParameters


@pytest.fixture
def parameters() -> ModelParameters:
    """Create a small parameter object for constraint testing."""
    return ModelParameters(
        source_ids=("S1", "S2"),
        plant_ids=("T1",),
        zone_ids=("Z1",),
        quality_parameter_ids=("turbidity",),
        source_plant_arcs=(
            ("S1", "T1"),
            ("S2", "T1"),
        ),
        plant_zone_arcs=(("T1", "Z1"),),
        demand_by_zone={
            "Z1": 30.0,
        },
        source_fixed_cost={
            "S1": 10.0,
            "S2": 20.0,
        },
        plant_fixed_cost={
            "T1": 5.0,
        },
        source_unit_cost={
            "S1": 1.0,
            "S2": 2.0,
        },
        plant_unit_treatment_cost={
            "T1": 0.5,
        },
        source_min_withdrawal={
            "S1": 5.0,
            "S2": 0.0,
        },
        source_max_withdrawal={
            "S1": 40.0,
            "S2": 30.0,
        },
        plant_min_throughput={
            "T1": 0.0,
        },
        plant_max_throughput={
            "T1": 60.0,
        },
        source_plant_link_capacity={
            ("S1", "T1"): 40.0,
            ("S2", "T1"): 30.0,
        },
        plant_zone_link_capacity={
            ("T1", "Z1"): 60.0,
        },
        source_quality={
            ("S1", "turbidity"): 1.0,
            ("S2", "turbidity"): 4.0,
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


@pytest.fixture
def model(parameters: ModelParameters) -> pyo.ConcreteModel:
    """Build the Pyomo model used by each test."""
    return build_model(parameters)


def test_all_constraint_families_are_created(
    model: pyo.ConcreteModel,
) -> None:
    """Check that every expected constraint family is present."""
    expected = {
        "demand_satisfaction",
        "source_min_withdrawal",
        "source_max_withdrawal",
        "source_flow_balance",
        "plant_min_throughput",
        "plant_max_throughput",
        "plant_flow_balance",
        "source_plant_link_capacity",
        "plant_zone_link_capacity",
        "source_link_activation",
        "plant_link_activation",
        "quality_lower_bound",
        "quality_upper_bound",
    }

    actual = set(model.component_map(pyo.Constraint))

    assert expected <= actual


def test_constraint_indices_match_network(
    model: pyo.ConcreteModel,
) -> None:
    """Check that constraints use only configured nodes and arcs."""
    assert set(model.demand_satisfaction) == {"Z1"}

    assert set(model.source_flow_balance) == {
        "S1",
        "S2",
    }

    assert set(model.plant_flow_balance) == {
        "T1",
    }

    assert set(model.source_plant_link_capacity) == {
        ("S1", "T1"),
        ("S2", "T1"),
    }

    assert set(model.plant_zone_link_capacity) == {
        ("T1", "Z1"),
    }

    assert set(model.quality_upper_bound) == {
        ("T1", "turbidity"),
    }


def test_feasible_values_satisfy_all_constraints(
    model: pyo.ConcreteModel,
) -> None:
    """Check a manually assigned feasible solution."""
    model.alpha["S1"].set_value(1)
    model.alpha["S2"].set_value(0)

    model.a["S1"].set_value(30.0)
    model.a["S2"].set_value(0.0)

    model.beta["T1"].set_value(1)

    model.gamma["S1", "T1"].set_value(1)
    model.gamma["S2", "T1"].set_value(0)

    model.b["S1", "T1"].set_value(30.0)
    model.b["S2", "T1"].set_value(0.0)

    model.delta["T1", "Z1"].set_value(1)
    model.c["T1", "Z1"].set_value(30.0)

    for constraint in model.component_data_objects(
        pyo.Constraint,
        active=True,
    ):
        body = pyo.value(constraint.body)

        if constraint.has_lb():
            assert body >= pyo.value(constraint.lower) - 1e-8

        if constraint.has_ub():
            assert body <= pyo.value(constraint.upper) + 1e-8


def test_inactive_source_forces_zero_withdrawal(
    model: pyo.ConcreteModel,
) -> None:
    """Check that an inactive source cannot have positive withdrawal."""
    model.alpha["S1"].set_value(0)
    model.a["S1"].set_value(1.0)

    constraint = model.source_max_withdrawal["S1"]

    assert pyo.value(constraint.body) > pyo.value(constraint.upper)


def test_source_balance_detects_unrouted_withdrawal(
    model: pyo.ConcreteModel,
) -> None:
    """Check that source withdrawal must equal source-to-plant flow."""
    model.a["S1"].set_value(20.0)
    model.b["S1", "T1"].set_value(15.0)

    constraint = model.source_flow_balance["S1"]

    assert pyo.value(constraint.body) != pytest.approx(0.0)
