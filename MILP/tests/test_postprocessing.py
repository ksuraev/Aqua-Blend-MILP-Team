"""Tests for AquaBlend post-processing."""

from __future__ import annotations

from dataclasses import replace

import pyomo.environ as pyo
import pytest
from pyomo.opt import SolverResults, SolverStatus, TerminationCondition

from src.contracts import (
    DemandZoneInput,
    FlowResult,
    PlantInput,
    PlantZoneLinkInput,
    ScenarioData,
    SourceInput,
    SourcePlantLinkInput,
)

from src.postprocessing import (
    PostprocessingError,
    SolverVariables,
    _build_cost_summary,
    _build_demand_zone_results,
    _build_plant_results,
    _build_plant_zone_flows,
    _build_quality_result,
    _build_source_plant_flows,
    _build_source_results,
    _clean_binary_value,
    _clean_flow_value,
    _extract_solver_status,
    _run_output_consistency_checks,
    hydrogen_concentration_to_ph,
    postprocess_solution,
)

from src.preprocessing import ModelParameters


# ---------------------------------------------------------------------------
# Reusable test data
# ---------------------------------------------------------------------------

# Used to provide a valid two source ModelParameters object to multiple tests.
@pytest.fixture
def parameters() -> ModelParameters:
    """Return a small, internally consistent two-source network."""
    return ModelParameters(
        scenario_id="postprocessing_test",
        source_ids=("S1", "S2"),
        plant_ids=("T1",),
        zone_ids=("Z1",),
        quality_parameter_ids=("turbidity",),
        source_plant_arcs=(("S1", "T1"), ("S2", "T1")),
        plant_zone_arcs=(("T1", "Z1"),),
        demand_by_zone={"Z1": 100.0},
        source_fixed_cost={"S1": 10.0, "S2": 20.0},
        plant_fixed_cost={"T1": 50.0},
        source_unit_cost={"S1": 2.0, "S2": 3.0},
        plant_unit_treatment_cost={"T1": 0.5},
        source_min_withdrawal={"S1": 0.0, "S2": 0.0},
        source_max_withdrawal={"S1": 100.0, "S2": 100.0},
        plant_min_throughput={"T1": 0.0},
        plant_max_throughput={"T1": 150.0},
        source_plant_link_capacity={
            ("S1", "T1"): 100.0,
            ("S2", "T1"): 100.0,
        },
        plant_zone_link_capacity={("T1", "Z1"): 150.0},
        source_quality={
            ("S1", "turbidity"): 2.0,
            ("S2", "turbidity"): 6.0,
        },
        quality_lower_bound={"turbidity": 0.0},
        quality_upper_bound={"turbidity": 8.0},
        quality_units={"turbidity": "NTU"},
        warnings=(),
    )


# Used to create a SourceInput without repeating all source fields in the fixture.
def _source(source_id: str, quality: float) -> SourceInput:
    """Build one source input while keeping fixture setup readable."""
    return SourceInput(
        source_id=source_id,
        name=source_id,
        source_type="reservoir",
        enabled=True,
        forced_inactive=False,
        minimum_withdrawal_ml_per_day=0.0,
        maximum_withdrawal_ml_per_day=100.0,
        withdrawal_bounds_origin="test",
        fixed_activation_cost=10.0,
        cost_per_ml=2.0,
        quality={"turbidity": quality},
        has_estimated_values=False,
        database_model_ready=True,
        availability_status="AVAILABLE",
        provenance={},
    )


# Used to provide the original scenario information required by postprocessing.
@pytest.fixture
def scenario() -> ScenarioData:
    """Return the scenario-side information used to report quality."""
    return ScenarioData(
        scenario_id="postprocessing_test",
        scenario_name="Post-processing test",
        status="READY",
        description="Two sources, one plant and one demand zone.",
        sources=(_source("S1", 2.0), _source("S2", 6.0)),
        plants=(
            PlantInput(
                plant_id="T1",
                name="Plant 1",
                enabled=True,
                minimum_processing_capacity_ml_per_day=0.0,
                maximum_processing_capacity_ml_per_day=150.0,
                fixed_activation_cost=50.0,
                treatment_cost_per_ml=0.5,
            ),
        ),
        demand_zones=(
            DemandZoneInput(zone_id="Z1", name="Zone 1", demand_ml_per_day=100.0),
        ),
        source_to_plant_links=(
            SourcePlantLinkInput("S1", "T1", True, 100.0),
            SourcePlantLinkInput("S2", "T1", True, 100.0),
        ),
        plant_to_zone_links=(PlantZoneLinkInput("T1", "Z1", True, 150.0),),
        quality_limits={
            "parameters": {
                "turbidity": {
                    "minimum": 0.0,
                    "maximum": 8.0,
                    "unit": "NTU",
                    "transform": "identity",
                }
            }
        },
        validation_issues=(),
    )


# Used to provide simulated solved decision values for a valid 60/40 blend.
@pytest.fixture
def variables() -> SolverVariables:
    """Represent the solved decision values for a 60/40 blend."""
    return SolverVariables(
        source_active={"S1": 1.0, "S2": 1.0},
        plant_active={"T1": 1.0},
        source_withdrawal={"S1": 60.0, "S2": 40.0},
        source_plant_flow={("S1", "T1"): 60.0, ("S2", "T1"): 40.0},
        plant_zone_flow={("T1", "Z1"): 100.0},
    )


# Used to run the postprocessing builders once and reuse their outputs in tests.
def _assembled_results(scenario, parameters, variables, *, objective=370.0):
    """Run the implemented builders and return one consistent output bundle."""
    warnings: list[str] = []
    inflow_by_plant, source_plant = _build_source_plant_flows(
        parameters, variables, warnings
    )
    outflow_by_plant, inflow_by_zone, plant_zone = _build_plant_zone_flows(
        parameters, variables, warnings
    )
    flows = FlowResult(source_to_plant=source_plant, plant_to_zone=plant_zone)

    sources, source_fixed, source_variable = _build_source_results(
        scenario, parameters, variables, sum(inflow_by_zone.values()), warnings
    )
    plants, plant_fixed, plant_variable = _build_plant_results(
        parameters, variables, inflow_by_plant, outflow_by_plant, warnings
    )
    demand_zones = _build_demand_zone_results(parameters, inflow_by_zone, warnings)
    quality = _build_quality_result(
        scenario, parameters, source_plant, inflow_by_plant, warnings
    )
    summary = _build_cost_summary(
        parameters,
        sources,
        plants,
        sum(inflow_by_zone.values()),
        source_fixed,
        source_variable,
        plant_fixed,
        plant_variable,
        objective,
        warnings,
    )
    return sources, plants, demand_zones, flows, quality, summary, warnings


# ---------------------------------------------------------------------------
# Numerical cleaning and transformation tests
# ---------------------------------------------------------------------------


# Used to check that tiny negative solver noise is converted to zero.
def test_small_negative_flow_is_cleaned_to_zero() -> None:
    """Tiny negative solver noise is not a physically negative flow."""
    assert _clean_flow_value(-1e-10, "test flow", []) == 0.0


# Used to check that a genuinely negative flow is rejected.
def test_material_negative_flow_raises_error() -> None:
    """A negative value beyond tolerance must never enter the output."""
    with pytest.raises(PostprocessingError, match="invalid negative value"):
        _clean_flow_value(-0.01, "test flow", [])


# Used to check that an unset flow becomes zero and produces a warning.
def test_unset_flow_becomes_zero_and_adds_warning() -> None:
    """An unset solver variable is made explicit rather than causing a crash."""
    warnings: list[str] = []
    assert _clean_flow_value(None, "test flow", warnings) == 0.0
    assert warnings == ["test flow was unset by the solver (None); treated as 0.0."]


# Used to check that values close to binary 0 and 1 are cleaned correctly.
@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [(0.0, False), (1.0, True), (1e-8, False), (1.0 - 1e-8, True)],
)
def test_binary_values_are_snapped_within_tolerance(raw_value, expected) -> None:
    """Small numerical deviations from binary 0/1 are normal solver noise."""
    assert _clean_binary_value(raw_value, "activation", []) is expected


# Used to check that a binary value far from 0 and 1 is rejected.
def test_invalid_binary_value_raises_error() -> None:
    """A value such as 0.4 must not be guessed as active or inactive."""
    with pytest.raises(PostprocessingError, match="not within tolerance"):
        _clean_binary_value(0.4, "activation", [])


# Used to check the conversion from hydrogen concentration in nmol/L back to pH.
@pytest.mark.parametrize(
    ("concentration", "expected_ph"),
    [(100.0, 7.0), (10.0, 8.0), (1000.0, 6.0)],
)
def test_hydrogen_concentration_converts_back_to_ph(
    concentration, expected_ph
) -> None:
    """The inverse transformation must use hydrogen concentration in nmol/L."""
    assert hydrogen_concentration_to_ph(concentration) == pytest.approx(expected_ph)


# Used to check that invalid hydrogen concentrations cannot be converted to pH.
@pytest.mark.parametrize("value", [0.0, -1.0, float("inf"), float("nan")])
def test_invalid_hydrogen_concentration_is_rejected(value) -> None:
    """pH is undefined for non-positive or non-finite concentrations."""
    with pytest.raises(PostprocessingError):
        hydrogen_concentration_to_ph(value)


# ---------------------------------------------------------------------------
# Solver status and result builder tests
# ---------------------------------------------------------------------------


# Used to create simulated Pyomo solver status information without running a solver.
def _solver_results(status, termination) -> SolverResults:
    results = SolverResults()
    results.solver.status = status
    results.solver.termination_condition = termination
    return results


# Used to create a minimal Pyomo model with an objective value for status tests.
def _objective_model(value: float = 12.5) -> pyo.ConcreteModel:
    model = pyo.ConcreteModel()
    model.x = pyo.Var(initialize=value)
    model.objective = pyo.Objective(expr=model.x)
    return model


# Used to check that an optimal result is feasible and includes its objective.
def test_optimal_solver_status_includes_objective() -> None:
    """A proven optimum is both feasible and optimal and has an objective."""
    status, feasible, optimal, objective = _extract_solver_status(
        _objective_model(),
        _solver_results(SolverStatus.ok, TerminationCondition.optimal),
    )
    assert status == "optimal"
    assert feasible is True
    assert optimal is True
    assert objective == pytest.approx(12.5)


# Used to check that an infeasible result does not report an objective value.
def test_infeasible_solver_status_has_no_objective() -> None:
    """An infeasible run must not expose a solved objective value."""
    _, feasible, optimal, objective = _extract_solver_status(
        _objective_model(),
        _solver_results(SolverStatus.warning, TerminationCondition.infeasible),
    )
    assert feasible is False
    assert optimal is False
    assert objective is None


# Used to check flow totals and capacity utilisation calculations on all arcs.
def test_flow_builders_calculate_volume_and_utilisation(parameters, variables) -> None:
    """Arc results must preserve flow totals and calculate capacity use."""
    warnings: list[str] = []
    plant_inflow, source_flows = _build_source_plant_flows(
        parameters, variables, warnings
    )
    plant_outflow, zone_inflow, plant_flows = _build_plant_zone_flows(
        parameters, variables, warnings
    )

    assert plant_inflow == {"T1": 100.0}
    assert plant_outflow == {"T1": 100.0}
    assert zone_inflow == {"Z1": 100.0}
    assert source_flows[0].utilisation_percent == pytest.approx(60.0)
    assert source_flows[1].utilisation_percent == pytest.approx(40.0)
    assert plant_flows[0].utilisation_percent == pytest.approx(100 / 150 * 100)
    assert warnings == []


# Used to check that a missing solved value for a modelled arc raises an error.
def test_missing_arc_variable_raises_clear_error(parameters, variables) -> None:
    """Every modelled arc must have a corresponding solved decision value."""
    incomplete = replace(
        variables,
        source_plant_flow={("S1", "T1"): 60.0},
    )
    with pytest.raises(PostprocessingError, match="No solved flow variable"):
        _build_source_plant_flows(parameters, incomplete, [])


# Used to check source activation, withdrawal, utilisation, blend ratio, and costs.
def test_source_results_calculate_cost_utilisation_and_blend_ratio(
    scenario, parameters, variables
) -> None:
    """Source output should explain both the allocation and its cost."""
    sources, fixed_cost, variable_cost = _build_source_results(
        scenario, parameters, variables, 100.0, []
    )

    s1 = next(source for source in sources if source.source_id == "S1")
    assert s1.activated is True
    assert s1.withdrawal_ml_per_day == pytest.approx(60.0)
    assert s1.utilisation_percent == pytest.approx(60.0)
    assert s1.blend_ratio == pytest.approx(0.60)
    assert s1.variable_withdrawal_cost == pytest.approx(120.0)
    assert fixed_cost == pytest.approx(30.0)
    assert variable_cost == pytest.approx(240.0)


# Used to check that conflicting activation and withdrawal values add a warning.
def test_activation_flow_disagreement_adds_warning(
    scenario, parameters, variables
) -> None:
    """Physical flow is reported, but disagreement with a binary is visible."""
    inconsistent = replace(
        variables,
        source_active={"S1": 0.0, "S2": 1.0},
    )
    warnings: list[str] = []
    _build_source_results(scenario, parameters, inconsistent, 100.0, warnings)
    assert any("S1" in warning and "disagrees" in warning for warning in warnings)


# Used to check exact demand, surplus delivery, and demand shortfall reporting.
@pytest.mark.parametrize(
    ("delivered", "satisfied", "unmet", "surplus"),
    [(100.0, True, 0.0, 0.0), (120.0, True, 0.0, 20.0), (90.0, False, 10.0, 0.0)],
)
def test_demand_result_distinguishes_exact_surplus_and_shortfall(
    parameters, delivered, satisfied, unmet, surplus
) -> None:
    """Demand reporting must reflect the model's greater-than-or-equal rule."""
    warnings: list[str] = []
    result = _build_demand_zone_results(parameters, {"Z1": delivered}, warnings)[0]
    assert result.demand_satisfied is satisfied
    assert result.unmet_demand_ml_per_day == pytest.approx(unmet)
    assert result.surplus_ml_per_day == pytest.approx(surplus)
    assert bool(warnings) is (not satisfied)


# ---------------------------------------------------------------------------
# Quality, cost and output consistency tests
# ---------------------------------------------------------------------------


# Used to check that plant inflow quality is calculated as a flow weighted blend.
def test_quality_blend_is_flow_weighted(scenario, parameters, variables) -> None:
    """The 60/40 blend of 2 and 6 NTU must equal 3.6 NTU."""
    inflow, source_flows = _build_source_plant_flows(parameters, variables, [])
    quality = _build_quality_result(scenario, parameters, source_flows, inflow, [])
    turbidity = quality.plant_inflow[0].parameters[0]

    assert turbidity.model_value == pytest.approx(3.6)
    assert turbidity.reported_value == pytest.approx(3.6)
    assert turbidity.within_limits is True
    assert turbidity.binding_lower is False
    assert turbidity.binding_upper is False


# Used to check that quality on its upper limit is feasible and binding.
def test_quality_at_upper_limit_is_feasible_and_binding(
    scenario, parameters, variables
) -> None:
    """A value exactly on a bound is feasible, but the bound is binding."""
    bound_parameters = replace(
        parameters,
        quality_upper_bound={"turbidity": 3.6},
    )
    inflow, source_flows = _build_source_plant_flows(bound_parameters, variables, [])
    quality = _build_quality_result(
        scenario, bound_parameters, source_flows, inflow, []
    )
    result = quality.plant_inflow[0].parameters[0]
    assert result.within_limits is True
    assert result.binding_upper is True


# Used to check that quality is omitted when a plant has no inflow.
def test_zero_flow_plant_omits_undefined_quality(scenario, parameters) -> None:
    """A weighted average is undefined at zero inflow and must not divide by zero."""
    zero_variables = SolverVariables(
        source_active={"S1": 0.0, "S2": 0.0},
        plant_active={"T1": 0.0},
        source_withdrawal={"S1": 0.0, "S2": 0.0},
        source_plant_flow={("S1", "T1"): 0.0, ("S2", "T1"): 0.0},
        plant_zone_flow={("T1", "Z1"): 0.0},
    )
    warnings: list[str] = []
    inflow, source_flows = _build_source_plant_flows(
        parameters, zero_variables, warnings
    )
    quality = _build_quality_result(
        scenario, parameters, source_flows, inflow, warnings
    )
    assert quality.plant_inflow[0].parameters == ()
    assert any("has no inflow" in warning for warning in warnings)


# Used to check that a cost mismatch is detected and reported as a warning.
def test_cost_mismatch_is_detected_and_adds_warning(
    scenario, parameters, variables
) -> None:
    """A reconstructed cost different from the objective must be reported."""
    *_, summary, warnings = _assembled_results(
        scenario, parameters, variables, objective=330.0
    )
    assert summary.costs.total_source_fixed_cost == pytest.approx(30.0)
    assert summary.costs.total_source_variable_cost == pytest.approx(240.0)
    assert summary.costs.total_plant_fixed_cost == pytest.approx(50.0)
    assert summary.costs.total_plant_variable_cost == pytest.approx(50.0)
    assert summary.costs.reconstructed_total_cost == pytest.approx(370.0)
    assert summary.costs.cost_reconciles is False
    assert any("does not match" in warning for warning in warnings)


# Used to check that matching cost components and objective pass reconciliation.
def test_cost_breakdown_passes_when_objective_is_correct(
    scenario, parameters, variables
) -> None:
    """The valid 60/40 solution has a total reconstructed cost of 370."""
    *_, summary, warnings = _assembled_results(
        scenario, parameters, variables, objective=370.0
    )
    assert summary.costs.reconstructed_total_cost == pytest.approx(370.0)
    assert summary.costs.total_cost == pytest.approx(370.0)
    assert summary.costs.cost_reconciles is True
    assert not any("does not match" in warning for warning in warnings)


# Used to retrieve one output consistency check by its name.
def _check_by_name(validation, name):
    return next(check for check in validation.checks if check.check == name)


# Used to check that a valid result passes all twelve consistency checks.
def test_valid_output_passes_all_twelve_consistency_checks(
    scenario, parameters, variables
) -> None:
    """A sound output must pass every check before downstream consumption."""
    sources, plants, zones, flows, quality, summary, warnings = _assembled_results(
        scenario, parameters, variables, objective=370.0
    )
    validation = _run_output_consistency_checks(
        parameters, sources, plants, zones, flows, quality, summary, warnings
    )

    assert validation.status == "PASSED"
    assert len(validation.checks) == 12
    assert all(check.passed for check in validation.checks)


# Used to check that withdrawal and outgoing flow disagreement fails validation.
def test_source_flow_mismatch_fails_conservation_check(
    scenario, parameters, variables
) -> None:
    """Reported withdrawal must equal total flow leaving each source."""
    sources, plants, zones, flows, quality, summary, warnings = _assembled_results(
        scenario, parameters, variables, objective=370.0
    )
    bad_source_flow = replace(flows.source_to_plant[0], flow_ml_per_day=55.0)
    bad_flows = replace(
        flows,
        source_to_plant=(bad_source_flow, *flows.source_to_plant[1:]),
    )
    validation = _run_output_consistency_checks(
        parameters, sources, plants, zones, bad_flows, quality, summary, warnings
    )

    assert validation.status == "FAILED"
    assert _check_by_name(validation, "source_flow_conservation").passed is False
    assert any("source_flow_conservation" in warning for warning in warnings)


# Used to check that a source-to-plant flow above capacity fails validation.
def test_capacity_violation_fails_link_check(
    scenario, parameters, variables
) -> None:
    """A reported arc flow above its modelled capacity must be detected."""
    sources, plants, zones, flows, quality, summary, warnings = _assembled_results(
        scenario, parameters, variables, objective=370.0
    )
    excessive = replace(flows.source_to_plant[0], flow_ml_per_day=101.0)
    bad_flows = replace(flows, source_to_plant=(excessive, flows.source_to_plant[1]))
    validation = _run_output_consistency_checks(
        parameters, sources, plants, zones, bad_flows, quality, summary, warnings
    )
    assert _check_by_name(validation, "source_to_plant_link_capacity").passed is False


# Used to check that quality outside its permitted range fails validation.
def test_quality_violation_fails_quality_check(
    scenario, parameters, variables
) -> None:
    """A value outside the permitted quality range must fail validation."""
    sources, plants, zones, flows, quality, summary, warnings = _assembled_results(
        scenario, parameters, variables, objective=370.0
    )
    parameter = quality.plant_inflow[0].parameters[0]
    bad_parameter = replace(parameter, model_value=9.0, within_limits=False)
    bad_plant_quality = replace(
        quality.plant_inflow[0], parameters=(bad_parameter,)
    )
    bad_quality = replace(quality, plant_inflow=(bad_plant_quality,))
    validation = _run_output_consistency_checks(
        parameters, sources, plants, zones, flows, bad_quality, summary, warnings
    )
    assert _check_by_name(validation, "plant_inflow_quality_limits").passed is False


# ---------------------------------------------------------------------------
# Known integration gap
# ---------------------------------------------------------------------------


# Used to check the complete postprocessing path after upstream fields are added.
@pytest.mark.xfail(
    strict=True,
    reason=(
        "ScenarioData and ModelParameters do not yet carry input/loader/"
        "preprocessing validation objects and run_id required by postprocess_solution."
    ),
)
def test_full_postprocess_solution_after_upstream_passthrough_is_implemented(
    scenario, parameters, variables
) -> None:
    """Remove xfail once upstream contracts carry validation objects and run_id."""
    model = pyo.ConcreteModel()
    model.S = pyo.Set(initialize=parameters.source_ids)
    model.T = pyo.Set(initialize=parameters.plant_ids)
    model.Z = pyo.Set(initialize=parameters.zone_ids)
    model.ST = pyo.Set(initialize=parameters.source_plant_arcs, dimen=2)
    model.TZ = pyo.Set(initialize=parameters.plant_zone_arcs, dimen=2)
    model.alpha = pyo.Var(model.S, initialize=variables.source_active)
    model.beta = pyo.Var(model.T, initialize=variables.plant_active)
    model.a = pyo.Var(model.S, initialize=variables.source_withdrawal)
    model.b = pyo.Var(model.ST, initialize=variables.source_plant_flow)
    model.c = pyo.Var(model.TZ, initialize=variables.plant_zone_flow)
    model.objective = pyo.Objective(expr=370.0)

    results = _solver_results(SolverStatus.ok, TerminationCondition.optimal)
    solved = postprocess_solution(scenario, parameters, model, results)
    assert solved.validation.output_consistency.status == "PASSED"