"""Integration tests for the Pyomo and HiGHS solver."""

from __future__ import annotations

import pyomo.environ as pyo
import pytest
from pyomo.opt import TerminationCondition

from src.model import solve
from src.preprocessing import ModelParameters


def make_parameters(demand: float = 30.0) -> ModelParameters:
    """Create a small parameter set for solver testing."""
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
            "Z1": demand,
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


def test_solve_returns_optimal_model_and_results() -> None:
    """Check that HiGHS solves a feasible AquaBlend model."""
    model, results = solve(make_parameters())

    assert results.solver.termination_condition == TerminationCondition.optimal
    assert pyo.value(model.total_cost) == pytest.approx(60.0)

    assert pyo.value(model.alpha["S1"]) == pytest.approx(1.0)
    assert pyo.value(model.a["S1"]) == pytest.approx(30.0)
    assert pyo.value(model.b["S1", "T1"]) == pytest.approx(30.0)
    assert pyo.value(model.c["T1", "Z1"]) == pytest.approx(30.0)


def test_solve_reports_infeasible_model() -> None:
    """Check that impossible demand returns an infeasible status."""
    model, results = solve(make_parameters(demand=70.0))

    assert model is not None
    assert results.solver.termination_condition == TerminationCondition.infeasible


def test_solve_preserves_shared_variables() -> None:
    """Check that the solved model uses the agreed variable interface."""
    model, _ = solve(make_parameters())

    expected_variables = {
        "alpha",
        "a",
        "beta",
        "gamma",
        "b",
        "delta",
        "c",
    }

    actual_variables = set(model.component_map(pyo.Var))

    assert expected_variables <= actual_variables


def test_solve_accepts_tee_option() -> None:
    """Check that the HiGHS solver log can be enabled."""
    model, results = solve(make_parameters(), tee=True)

    assert model is not None
    assert results.solver.termination_condition == TerminationCondition.optimal
