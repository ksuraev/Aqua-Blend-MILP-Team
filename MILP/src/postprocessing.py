"""
This script is used for post processing after the solver is run.

- Extracts the solver status, objective value, active sources and draw values
- Cleans results and checks for Nones, near-zero values and invalid values.
- Calculates the total delivered volume and source/plant level cost contributions
- Calculates blended turbidity, alkalinity, and hydrogen-ion concentration/pH
  at each plant's inflow
- Runs the output-consistency validation checks (flow conservation, bound
  compliance, cost reconciliation, etc.).
- Assembles everything into a SolvedScenario matching the output contract
  in`solved_data.py / output_contract_v1.json.

Postprocessing does NOT run loader or preprocessing validation. Those are the
responsibility of the data-loading and preprocessing stages respectively, and
their results (InputScenario, InputValidationPolicy,
LoaderValidation, PreprocessingValidation) are expected to be handed
to postprocess_solution as pass-through arguments, unchanged. Currently
neither ScenarioData (scenario_data.py) nor ModelParameters
(preprocessing.py) carry these objects, so callers must construct them
upstream (in the loader/preprocessing stages) and pass them straight through
here.

For the purpose of the toy model, we are assuming linear blending.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

try:
    from .scenario_data import ScenarioData
    from .preprocessing import ModelParameters
    from .solved_data import (
        CostBreakdown,
        CostSummary,
        DemandZoneResult,
        FlowResult,
        InflowQualityPlantResult,
        InputScenario,
        InputValidationPolicy,
        LoaderValidation,
        PlantResult,
        PlantZoneFlowResult,
        PreprocessingValidation,
        OutputValidation,
        QualityParameterResult,
        QualityResult,
        SolutionValidation,
        SolvedScenario,
        SolverSummary,
        SourceDecisionEvidence,
        SourcePlantFlowResult,
        SourceResult,
        ValidationCheck,
    )
except ImportError:
    from scenario_data import ScenarioData
    from preprocessing import ModelParameters
    from solved_data import (
        CostBreakdown,
        CostSummary,
        DemandZoneResult,
        FlowResult,
        InflowQualityPlantResult,
        InputScenario,
        InputValidationPolicy,
        LoaderValidation,
        PlantResult,
        PlantZoneFlowResult,
        PreprocessingValidation,
        OutputValidation,
        QualityParameterResult,
        QualityResult,
        SolutionValidation,
        SolvedScenario,
        SolverSummary,
        SourceDecisionEvidence,
        SourcePlantFlowResult,
        SourceResult,
        ValidationCheck,
    )


class PostprocessingError(RuntimeError):
    """Raised when a solved model cannot be converted into a SolvedScenario."""


_EPSILON = 1e-9
# Looser than the preprocessing epsilon: this tolerance absorbs ordinary
# solver noise (e.g. 1e-11 instead of an exact 0.0), not modelling error.
_SOLVER_TOLERANCE = 1e-6
_COST_RECONCILIATION_TOLERANCE = 1e-3

# Hydrogen-ion concentration in the model is expressed in nmol/L. Defining here
# so its easy to tell why the calculation exists as it does.
# pH = -log10(mol/L) = -log10(nmol/L * 1e-9) = 9 - log10(nmol/L)

_NMOL_PER_MOL = 1e9


def hydrogen_concentration_to_ph(concentration_nmol_per_l: float) -> float:
    """Convert hydrogen-ion concentration in nmol/L back into pH.

    Inverse of preprocessing.ph_to_hydrogen_ion.
    """
    if not math.isfinite(concentration_nmol_per_l) or concentration_nmol_per_l <= 0:
        raise PostprocessingError(
            "Hydrogen-ion concentration must be finite and positive, got "
            f"{concentration_nmol_per_l!r}."
        )

    value = math.log10(_NMOL_PER_MOL) - math.log10(concentration_nmol_per_l) # math.log10(1e9) is to convert from nmol/L to mol/L
    if not math.isfinite(value):
        raise PostprocessingError(
            f"Hydrogen-ion concentration {concentration_nmol_per_l!r} could not "
            "be converted back to pH safely."
        )
    return value


_SUPPORTED_QUALITY_INVERSE_TRANSFORMS = {
    "identity": lambda value: value,
    "ph_to_hydrogen_ion": (
        lambda value: (math.log10(_NMOL_PER_MOL) - math.log10(value))
        if value > 0
        else math.nan
    ),
}


@dataclass(frozen=True, slots=True)
class SolverVariables:
    """Class for the decision variable objects produced by the solver.
    Values can be PuLP/Pyomo variable objects or plain numbers (for testing).
    """

    source_active: dict[str, Any]
    plant_active: dict[str, Any]
    source_withdrawal: dict[str, Any]
    source_plant_flow: dict[tuple[str, str], Any]
    plant_zone_flow: dict[tuple[str, str], Any]


def _variable_value(variable: Any, label: str) -> float | None:
    """Extract a raw solved value from either a PuLP/Pyomo variable or a plain number."""
    if isinstance(variable, (int, float)):
        return float(variable)

    value_method = getattr(variable, "value", None)
    if callable(value_method):
        return value_method()
    if isinstance(value_method, (int, float)):
        # Pyomo variables expose ``.value`` as a plain attribute, not a method.
        return float(value_method)

    try:
        import pulp

        return pulp.value(variable)
    except ImportError as exc:
        raise PostprocessingError(
            f"Could not read a solved value for {label}: pulp is not installed "
            "and the variable is not a plain number."
        ) from exc


def _clean_flow_value(
    variable: Any,
    label: str,
    warnings: list[str],
) -> float:
    """Read one solved flow/withdrawal value and apply solver-tolerance cleaning."""
    raw = _variable_value(variable, label)

    if raw is None:
        warnings.append(f"{label} was unset by the solver (None); treated as 0.0.")
        return 0.0

    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise PostprocessingError(
            f"{label} solved value {raw!r} could not be converted to a float."
        ) from exc

    if not math.isfinite(value):
        raise PostprocessingError(f"{label} solved to a non-finite value: {value!r}.")

    if value < -_SOLVER_TOLERANCE:
        raise PostprocessingError(
            f"{label} solved to an invalid negative value ({value:g}); "
            "expected a non-negative flow or withdrawal."
        )
    if value < 0:
        value = 0.0
    if abs(value) < _SOLVER_TOLERANCE:
        value = 0.0

    return value


def _clean_binary_value(
    variable: Any,
    label: str,
    warnings: list[str],
) -> bool:
    """Read one solved activation value and snap it to a boolean within tolerance."""
    raw = _variable_value(variable, label)

    if raw is None:
        warnings.append(f"{label} was unset by the solver (None); treated as inactive.")
        return False

    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise PostprocessingError(
            f"{label} solved value {raw!r} could not be converted to a float."
        ) from exc

    if not math.isfinite(value):
        raise PostprocessingError(f"{label} solved to a non-finite value: {value!r}.")

    if abs(value) <= _SOLVER_TOLERANCE:
        return False
    if abs(value - 1.0) <= _SOLVER_TOLERANCE:
        return True

    raise PostprocessingError(
        f"{label} solved to {value!r}, which is not within tolerance of 0 or 1."
    )


def _extract_solver_status(problem: Any) -> tuple[str, bool | None, bool | None, float | None]:
    """Read the solver status, feasibility/optimality, and objective value.

    Supports both PuLP ``LpProblem`` objects and a simple duck-typed
    fallback (any object exposing ``status``/``objective`` or already-plain
    values), so this keeps working once the solver card lands with Pyomo.
    """
    try:
        import pulp
    except ImportError:
        pulp = None

    if pulp is not None and hasattr(problem, "status") and hasattr(problem, "objective"):
        status = pulp.LpStatus.get(problem.status, str(problem.status))
        objective_value = pulp.value(problem.objective)
        if objective_value is not None:
            objective_value = float(objective_value)
        is_optimal = status == "Optimal"
        # Keep feasibility simple and conservative: only "Optimal" is
        # confidently both feasible and optimal for this toy model. PuLP's
        # other statuses ("Infeasible", "Unbounded", "Not Solved",
        # "Undefined") don't guarantee a usable feasible solution, so we
        # don't try to distinguish "feasible but not optimal" here.
        is_feasible = is_optimal
        return status, is_feasible, is_optimal, objective_value

    raise PostprocessingError(
        "Could not determine solver status: unsupported problem object and "
        "pulp is not installed."
    )


def _build_source_plant_flows(
    parameters: ModelParameters,
    variables: SolverVariables,
    warnings: list[str],
) -> tuple[dict[str, float], tuple[SourcePlantFlowResult, ...]]:
    inflow_by_plant: dict[str, float] = {plant_id: 0.0 for plant_id in parameters.plant_ids}
    results: list[SourcePlantFlowResult] = []

    for source_id, plant_id in parameters.source_plant_arcs:
        key = (source_id, plant_id)
        if key not in variables.source_plant_flow:
            raise PostprocessingError(
                f"No solved flow variable was supplied for source-to-plant arc {key!r}."
            )

        flow = _clean_flow_value(
            variables.source_plant_flow[key],
            f"Source-to-plant flow {source_id} -> {plant_id}",
            warnings,
        )
        inflow_by_plant[plant_id] += flow

        capacity = parameters.source_plant_link_capacity.get(key, 0.0)
        utilisation_percent = (flow / capacity * 100.0) if capacity > _EPSILON else 0.0

        results.append(
            SourcePlantFlowResult(
                source_id=source_id,
                plant_id=plant_id,
                activated=flow > _EPSILON,
                flow_ml_per_day=flow,
                utilisation_percent=utilisation_percent,
            )
        )

    return inflow_by_plant, tuple(results)


def _build_plant_zone_flows(
    parameters: ModelParameters,
    variables: SolverVariables,
    warnings: list[str],
) -> tuple[dict[str, float], dict[str, float], tuple[PlantZoneFlowResult, ...]]:
    outflow_by_plant: dict[str, float] = {plant_id: 0.0 for plant_id in parameters.plant_ids}
    inflow_by_zone: dict[str, float] = {zone_id: 0.0 for zone_id in parameters.zone_ids}
    results: list[PlantZoneFlowResult] = []

    for plant_id, zone_id in parameters.plant_zone_arcs:
        key = (plant_id, zone_id)
        if key not in variables.plant_zone_flow:
            raise PostprocessingError(
                f"No solved flow variable was supplied for plant-to-zone arc {key!r}."
            )

        flow = _clean_flow_value(
            variables.plant_zone_flow[key],
            f"Plant-to-zone flow {plant_id} -> {zone_id}",
            warnings,
        )
        outflow_by_plant[plant_id] += flow
        inflow_by_zone[zone_id] += flow

        capacity = parameters.plant_zone_link_capacity.get(key, 0.0)
        utilisation_percent = (flow / capacity * 100.0) if capacity > _EPSILON else 0.0

        results.append(
            PlantZoneFlowResult(
                plant_id=plant_id,
                zone_id=zone_id,
                activated=flow > _EPSILON,
                flow_ml_per_day=flow,
                utilisation_percent=utilisation_percent,
            )
        )

    return outflow_by_plant, inflow_by_zone, tuple(results)


def _exclusion_reason_code(scenario: ScenarioData, source_id: str) -> str:
    """Best-effort reason a source never made it into the MILP.

    ``ScenarioData``/``ModelParameters`` don't currently expose *why*
    preprocessing dropped a source, so this only distinguishes the reasons
    that are visible on ``SourceInput`` itself; anything else is reported as
    a generic preprocessing exclusion.
    """
    source_input = next(
        (source for source in scenario.sources if source.source_id == source_id),
        None,
    )
    if source_input is None:
        return "UNKNOWN_SOURCE"
    if not source_input.enabled:
        return "DISABLED_BY_CONFIG"
    if source_input.forced_inactive:
        return "FORCED_INACTIVE"
    if not source_input.database_model_ready:
        return "DATA_NOT_MODEL_READY"
    return "EXCLUDED_BY_PREPROCESSING"


def _build_source_results(
    scenario: ScenarioData,
    parameters: ModelParameters,
    variables: SolverVariables,
    total_delivered_ml_per_day: float,
    warnings: list[str],
) -> tuple[tuple[SourceResult, ...], float, float]:
    results: list[SourceResult] = []
    total_fixed_cost = 0.0
    total_variable_cost = 0.0

    included_source_ids = set(parameters.source_ids)
    unit_cost_rank = {
        source_id: rank
        for rank, source_id in enumerate(
            sorted(parameters.source_ids, key=lambda sid: (parameters.source_unit_cost[sid], sid)),
            start=1,
        )
    }

    # Report every configured source, including ones preprocessing excluded
    # from the model entirely, so consumers can see the full picture.
    all_source_ids = list(dict.fromkeys([*(s.source_id for s in scenario.sources), *parameters.source_ids]))

    for source_id in all_source_ids:
        model_included = source_id in included_source_ids

        if not model_included:
            results.append(
                SourceResult(
                    source_id=source_id,
                    model_included=False,
                    activated=False,
                    withdrawal_ml_per_day=0.0,
                    utilisation_percent=0.0,
                    blend_ratio=None,
                    variable_withdrawal_cost=0.0,
                    total_source_cost=0.0,
                    selection_status="EXCLUDED",
                    exclusion_reason_code=_exclusion_reason_code(scenario, source_id),
                    decision_evidence=SourceDecisionEvidence(
                        unit_cost_rank=None,
                        binding_lower=None,
                        binding_upper=None,
                    ),
                )
            )
            continue

        if source_id not in variables.source_withdrawal:
            raise PostprocessingError(
                f"No solved withdrawal variable was supplied for source '{source_id}'."
            )

        withdrawal = _clean_flow_value(
            variables.source_withdrawal[source_id],
            f"Source '{source_id}' withdrawal",
            warnings,
        )

        is_selected = withdrawal > _EPSILON
        if source_id in variables.source_active:
            active_flag = _clean_binary_value(
                variables.source_active[source_id],
                f"Source '{source_id}' activation indicator",
                warnings,
            )
            if active_flag != is_selected:
                warnings.append(
                    f"Source '{source_id}' activation indicator ({active_flag}) "
                    f"disagrees with its solved withdrawal ({withdrawal:g} ML/day); "
                    "the withdrawal value was treated as authoritative."
                )

        fixed_cost_contribution = (
            parameters.source_fixed_cost[source_id] if is_selected else 0.0
        )
        variable_cost_contribution = parameters.source_unit_cost[source_id] * withdrawal
        total_fixed_cost += fixed_cost_contribution
        total_variable_cost += variable_cost_contribution

        max_withdrawal = parameters.source_max_withdrawal[source_id]
        min_withdrawal = parameters.source_min_withdrawal[source_id]
        utilisation_percent = (
            withdrawal / max_withdrawal * 100.0 if max_withdrawal > _EPSILON else 0.0
        )
        blend_ratio = (
            withdrawal / total_delivered_ml_per_day
            if total_delivered_ml_per_day > _EPSILON
            else None
        )

        results.append(
            SourceResult(
                source_id=source_id,
                model_included=True,
                activated=is_selected,
                withdrawal_ml_per_day=withdrawal,
                utilisation_percent=utilisation_percent,
                blend_ratio=blend_ratio,
                variable_withdrawal_cost=variable_cost_contribution,
                total_source_cost=variable_cost_contribution + fixed_cost_contribution,
                selection_status="SELECTED" if is_selected else "NOT_SELECTED",
                exclusion_reason_code=None,
                decision_evidence=SourceDecisionEvidence(
                    unit_cost_rank=unit_cost_rank[source_id],
                    binding_lower=(
                        is_selected
                        and math.isclose(withdrawal, min_withdrawal, abs_tol=_SOLVER_TOLERANCE)
                    ),
                    binding_upper=math.isclose(
                        withdrawal, max_withdrawal, abs_tol=_SOLVER_TOLERANCE
                    ),
                ),
            )
        )

    return tuple(results), total_fixed_cost, total_variable_cost


def _build_plant_results(
    parameters: ModelParameters,
    variables: SolverVariables,
    inflow_by_plant: dict[str, float],
    outflow_by_plant: dict[str, float],
    warnings: list[str],
) -> tuple[tuple[PlantResult, ...], float, float]:
    results: list[PlantResult] = []
    total_fixed_cost = 0.0
    total_variable_cost = 0.0

    for plant_id in parameters.plant_ids:
        inflow = inflow_by_plant[plant_id]
        outflow = outflow_by_plant[plant_id]
        if abs(inflow - outflow) > _SOLVER_TOLERANCE:
            warnings.append(
                f"Plant '{plant_id}' solved inflow ({inflow:g} ML/day) does not match "
                f"outflow ({outflow:g} ML/day); the mismatch exceeds solver tolerance."
            )
        throughput = inflow

        is_active = throughput > _EPSILON
        if plant_id in variables.plant_active:
            active_flag = _clean_binary_value(
                variables.plant_active[plant_id],
                f"Plant '{plant_id}' activation indicator",
                warnings,
            )
            if active_flag != is_active:
                warnings.append(
                    f"Plant '{plant_id}' activation indicator ({active_flag}) "
                    f"disagrees with its solved throughput ({throughput:g} ML/day); "
                    "the throughput value was treated as authoritative."
                )

        fixed_cost_contribution = parameters.plant_fixed_cost[plant_id] if is_active else 0.0
        variable_cost_contribution = parameters.plant_unit_treatment_cost[plant_id] * throughput
        total_fixed_cost += fixed_cost_contribution
        total_variable_cost += variable_cost_contribution

        max_throughput = parameters.plant_max_throughput[plant_id]
        utilisation_percent = (
            throughput / max_throughput * 100.0 if max_throughput > _EPSILON else 0.0
        )

        results.append(
            PlantResult(
                plant_id=plant_id,
                activated=is_active,
                throughput_ml_per_day=throughput,
                utilisation_percent=utilisation_percent,
                variable_treatment_cost=variable_cost_contribution,
                total_plant_cost=variable_cost_contribution + fixed_cost_contribution,
            )
        )

    return tuple(results), total_fixed_cost, total_variable_cost


def _build_demand_zone_results(
    parameters: ModelParameters,
    inflow_by_zone: dict[str, float],
    warnings: list[str],
) -> tuple[DemandZoneResult, ...]:
    results: list[DemandZoneResult] = []

    for zone_id in parameters.zone_ids:
        demand = parameters.demand_by_zone[zone_id]
        delivered = inflow_by_zone[zone_id]
        unmet = max(demand - delivered, 0.0)
        surplus = max(delivered - demand, 0.0)
        # Every demand zone in the current formulation is a hard constraint
        # (see model.demand_satisfaction), so all zones must have their
        # demand met. There is no field on ScenarioData/ModelParameters yet
        # to mark a zone as "nice to have"; if that's introduced later,
        # thread it through here instead of hardcoding True.
        demand_must_be_met = True

        if unmet > _SOLVER_TOLERANCE:
            warnings.append(
                f"Demand zone '{zone_id}' delivered volume ({delivered:g} ML/day) is "
                f"short of its demand ({demand:g} ML/day) by {unmet:g} ML/day."
            )

        results.append(
            DemandZoneResult(
                zone_id=zone_id,
                delivered_ml_per_day=delivered,
                surplus_ml_per_day=surplus,
                demand_satisfied=unmet <= _SOLVER_TOLERANCE,
                demand_must_be_met=demand_must_be_met,
                unmet_demand_ml_per_day=unmet,
            )
        )

    return tuple(results)


def _model_quality_name(
    parameters: ModelParameters,
    raw_name: str,
    transform: str,
) -> str:
    """Resolve the raw quality-parameter name to its model-space identifier."""
    default_model_name = (
        "hydrogen_ion_concentration_nmol_l" if transform == "ph_to_hydrogen_ion" else raw_name
    )
    if default_model_name in parameters.quality_parameter_ids:
        return default_model_name

    for candidate in parameters.quality_parameter_ids:
        if candidate == raw_name:
            return candidate

    raise PostprocessingError(
        f"Could not resolve model-space quality identifier for '{raw_name}'."
    )


def _build_quality_result(
    scenario: ScenarioData,
    parameters: ModelParameters,
    source_plant_flows: tuple[SourcePlantFlowResult, ...],
    inflow_by_plant: dict[str, float],
    warnings: list[str],
) -> QualityResult:
    """Blend source quality by solved flow at each plant's inflow.

    Model-space blending happens in the transformed units used by ModelParameters 
    (e.g. hydrogen-ion concentration in nmol/L) because that is the space in which 
    the transform is linear in volume. The result is then converted back into the 
    scenario's original (reported) units.
    """
    quality_parameters: dict[str, dict[str, Any]] = scenario.quality_limits["parameters"]

    flows_by_plant: dict[str, list[SourcePlantFlowResult]] = {
        plant_id: [] for plant_id in parameters.plant_ids
    }
    for flow in source_plant_flows:
        if flow.activated:
            flows_by_plant[flow.plant_id].append(flow)

    plant_inflow_results: list[InflowQualityPlantResult] = []

    for plant_id in parameters.plant_ids:
        total_inflow = inflow_by_plant[plant_id]
        contributing_flows = flows_by_plant[plant_id]
        parameter_results: list[QualityParameterResult] = []

        for raw_name, specification in quality_parameters.items():
            transform = str(specification.get("transform", "identity"))
            model_name = _model_quality_name(parameters, raw_name, transform)

            if total_inflow <= _EPSILON:
                warnings.append(
                    f"Plant '{plant_id}' has no inflow; blended '{raw_name}' quality "
                    "could not be computed and was omitted."
                )
                continue

            model_value = sum(
                parameters.source_quality[(flow.source_id, model_name)]
                * flow.flow_ml_per_day
                for flow in contributing_flows
            ) / total_inflow

            inverse_transform = _SUPPORTED_QUALITY_INVERSE_TRANSFORMS.get(transform)
            if inverse_transform is None:
                raise PostprocessingError(
                    f"Unsupported quality transform '{transform}' for '{raw_name}'."
                )
            reported_value = inverse_transform(model_value)
            if not math.isfinite(reported_value):
                raise PostprocessingError(
                    f"Blended '{raw_name}' quality at plant '{plant_id}' could not be "
                    "converted back to its reported unit."
                )

            model_min = parameters.quality_lower_bound[model_name]
            model_max = parameters.quality_upper_bound[model_name]
            lower_margin = model_value - model_min
            upper_margin = model_max - model_value

            parameter_results.append(
                QualityParameterResult(
                    parameter_id=raw_name,
                    model_parameter_id=model_name,
                    transform=transform,
                    model_unit=parameters.quality_units[model_name],
                    model_value=model_value,
                    model_min=model_min,
                    model_max=model_max,
                    reported_value=reported_value,
                    reported_unit=str(specification.get("unit", "")),
                    within_limits=(
                        lower_margin >= -_SOLVER_TOLERANCE
                        and upper_margin >= -_SOLVER_TOLERANCE
                    ),
                    binding_lower=lower_margin <= _SOLVER_TOLERANCE,
                    binding_upper=upper_margin <= _SOLVER_TOLERANCE,
                )
            )

        plant_inflow_results.append(
            InflowQualityPlantResult(
                plant_id=plant_id,
                flow_ml_per_day=total_inflow,
                parameters=tuple(parameter_results),
            )
        )

    return QualityResult(
        applies_to="blend_at_plant_inflow",
        plant_inflow=tuple(plant_inflow_results),
    )


def _build_cost_summary(
    parameters: ModelParameters,
    sources: tuple[SourceResult, ...],
    plants: tuple[PlantResult, ...],
    total_delivered: float,
    total_source_fixed_cost: float,
    total_source_variable_cost: float,
    total_plant_fixed_cost: float,
    total_plant_variable_cost: float,
    objective_value: float | None,
    warnings: list[str],
) -> CostSummary:
    reconstructed_total_cost = (
        total_source_fixed_cost
        + total_source_variable_cost
        + total_plant_fixed_cost
        + total_plant_variable_cost
    )

    total_cost = objective_value if objective_value is not None else reconstructed_total_cost
    cost_reconciles = True
    if objective_value is not None:
        cost_reconciles = (
            abs(reconstructed_total_cost - objective_value) <= _COST_RECONCILIATION_TOLERANCE
        )
        if not cost_reconciles:
            warnings.append(
                "Reconstructed total cost "
                f"({reconstructed_total_cost:g}) does not match the solver's "
                f"objective value ({objective_value:g})."
            )

    costs = CostBreakdown(
        total_source_fixed_cost=total_source_fixed_cost,
        total_source_variable_cost=total_source_variable_cost,
        total_plant_fixed_cost=total_plant_fixed_cost,
        total_plant_variable_cost=total_plant_variable_cost,
        reconstructed_total_cost=reconstructed_total_cost,
        total_cost=total_cost,
        cost_reconciles=cost_reconciles,
    )

    return CostSummary(
        total_demand_ml_per_day=sum(parameters.demand_by_zone.values()),
        total_withdrawal_ml_per_day=sum(s.withdrawal_ml_per_day for s in sources),
        total_treated_ml_per_day=sum(p.throughput_ml_per_day for p in plants),
        total_delivered_ml_per_day=total_delivered,
        selected_source_count=sum(1 for s in sources if s.activated),
        active_plant_count=sum(1 for p in plants if p.activated),
        costs=costs,
    )


def _build_binding_constraints_summary(
    sources: tuple[SourceResult, ...],
    quality: QualityResult,
    flows: FlowResult,
) -> tuple[str, ...]:
    """Text summary of constraints that are binding from the solution (e.g. source. withdrawal at upper limit)"""
    summary: list[str] = []

    for source in sources:
        if not source.activated:
            continue
        if source.decision_evidence.binding_lower:
            summary.append(f"Source '{source.source_id}' is at its minimum withdrawal bound.")
        if source.decision_evidence.binding_upper:
            summary.append(f"Source '{source.source_id}' is at its maximum withdrawal bound.")

    for plant_inflow in quality.plant_inflow:
        for parameter in plant_inflow.parameters:
            if parameter.binding_lower:
                summary.append(
                    f"Plant '{plant_inflow.plant_id}' blended '{parameter.parameter_id}' "
                    "is at its lower quality limit."
                )
            if parameter.binding_upper:
                summary.append(
                    f"Plant '{plant_inflow.plant_id}' blended '{parameter.parameter_id}' "
                    "is at its upper quality limit."
                )

    for flow in flows.source_to_plant:
        if flow.activated and flow.utilisation_percent >= 100.0 - _SOLVER_TOLERANCE:
            summary.append(
                f"Source-to-plant link {flow.source_id} -> {flow.plant_id} is at capacity."
            )

    for flow in flows.plant_to_zone:
        if flow.activated and flow.utilisation_percent >= 100.0 - _SOLVER_TOLERANCE:
            summary.append(
                f"Plant-to-zone link {flow.plant_id} -> {flow.zone_id} is at capacity."
            )

    return tuple(dict.fromkeys(summary))


def _check(check: str, passed: bool) -> ValidationCheck:
    return ValidationCheck(check=check, enabled=True, passed=passed)


def _run_output_consistency_checks(
    parameters: ModelParameters,
    sources: tuple[SourceResult, ...],
    plants: tuple[PlantResult, ...],
    demand_zones: tuple[DemandZoneResult, ...],
    flows: FlowResult,
    quality: QualityResult,
    cost_summary: CostSummary,
    warnings: list[str],
) -> OutputValidation:
    """Run the 12 output-consistency checks and assemble an OutputValidation.

    This is the only validation stage postprocessing is responsible for.
    Loader and preprocessing validation are passed through unchanged by the
    caller (see ``postprocess_solution``).
    """
    tol = _SOLVER_TOLERANCE
    checks: list[ValidationCheck] = []

    included_sources = {s.source_id: s for s in sources if s.model_included}

    # 1. decision_values_non_negative
    all_values = (
        [s.withdrawal_ml_per_day for s in sources]
        + [p.throughput_ml_per_day for p in plants]
        + [z.delivered_ml_per_day for z in demand_zones]
        + [f.flow_ml_per_day for f in flows.source_to_plant]
        + [f.flow_ml_per_day for f in flows.plant_to_zone]
    )
    checks.append(
        _check("decision_values_non_negative", all(value >= -tol for value in all_values))
    )

    # 2. source_activation_and_withdrawal_bounds
    source_bounds_ok = True
    for source in included_sources.values():
        withdrawal = source.withdrawal_ml_per_day
        if source.activated:
            lower = parameters.source_min_withdrawal[source.source_id]
            upper = parameters.source_max_withdrawal[source.source_id]
            if not (lower - tol <= withdrawal <= upper + tol):
                source_bounds_ok = False
        elif withdrawal > tol:
            source_bounds_ok = False
    checks.append(_check("source_activation_and_withdrawal_bounds", source_bounds_ok))

    # 3. plant_activation_and_throughput_bounds
    plant_bounds_ok = True
    for plant in plants:
        throughput = plant.throughput_ml_per_day
        if plant.activated:
            lower = parameters.plant_min_throughput[plant.plant_id]
            upper = parameters.plant_max_throughput[plant.plant_id]
            if not (lower - tol <= throughput <= upper + tol):
                plant_bounds_ok = False
        elif throughput > tol:
            plant_bounds_ok = False
    checks.append(_check("plant_activation_and_throughput_bounds", plant_bounds_ok))

    # 4. source_flow_conservation
    outflow_by_source: dict[str, float] = {source_id: 0.0 for source_id in parameters.source_ids}
    for flow in flows.source_to_plant:
        outflow_by_source[flow.source_id] = outflow_by_source.get(flow.source_id, 0.0) + flow.flow_ml_per_day
    source_flow_ok = all(
        abs(outflow_by_source.get(source.source_id, 0.0) - source.withdrawal_ml_per_day) <= tol
        for source in included_sources.values()
    )
    checks.append(_check("source_flow_conservation", source_flow_ok))

    # 5. plant_flow_conservation
    inflow_by_plant: dict[str, float] = {plant_id: 0.0 for plant_id in parameters.plant_ids}
    outflow_by_plant: dict[str, float] = {plant_id: 0.0 for plant_id in parameters.plant_ids}
    for flow in flows.source_to_plant:
        inflow_by_plant[flow.plant_id] = inflow_by_plant.get(flow.plant_id, 0.0) + flow.flow_ml_per_day
    for flow in flows.plant_to_zone:
        outflow_by_plant[flow.plant_id] = outflow_by_plant.get(flow.plant_id, 0.0) + flow.flow_ml_per_day
    plant_flow_ok = all(
        abs(inflow_by_plant[plant_id] - outflow_by_plant[plant_id]) <= tol
        for plant_id in parameters.plant_ids
    )
    checks.append(_check("plant_flow_conservation", plant_flow_ok))

    # 6. source_to_plant_link_capacity
    s2p_capacity_ok = all(
        flow.flow_ml_per_day
        <= parameters.source_plant_link_capacity.get((flow.source_id, flow.plant_id), 0.0) + tol
        for flow in flows.source_to_plant
    )
    checks.append(_check("source_to_plant_link_capacity", s2p_capacity_ok))

    # 7. plant_to_zone_link_capacity
    p2z_capacity_ok = all(
        flow.flow_ml_per_day
        <= parameters.plant_zone_link_capacity.get((flow.plant_id, flow.zone_id), 0.0) + tol
        for flow in flows.plant_to_zone
    )
    checks.append(_check("plant_to_zone_link_capacity", p2z_capacity_ok))

    # 8. demand_satisfaction
    demand_ok = all(
        (not zone.demand_must_be_met) or zone.unmet_demand_ml_per_day <= tol
        for zone in demand_zones
    )
    checks.append(_check("demand_satisfaction", demand_ok))

    # 9. plant_inflow_quality_limits
    quality_ok = all(
        parameter.within_limits
        for plant_inflow in quality.plant_inflow
        for parameter in plant_inflow.parameters
    )
    checks.append(_check("plant_inflow_quality_limits", quality_ok))

    # 10. objective_cost_reconciliation
    checks.append(_check("objective_cost_reconciliation", cost_summary.costs.cost_reconciles))

    # 11. summary_total_reconciliation
    summary_ok = (
        math.isclose(
            cost_summary.total_withdrawal_ml_per_day,
            sum(s.withdrawal_ml_per_day for s in sources),
            abs_tol=tol,
        )
        and math.isclose(
            cost_summary.total_treated_ml_per_day,
            sum(p.throughput_ml_per_day for p in plants),
            abs_tol=tol,
        )
        and math.isclose(
            cost_summary.total_delivered_ml_per_day,
            sum(z.delivered_ml_per_day for z in demand_zones),
            abs_tol=tol,
        )
        and cost_summary.selected_source_count == sum(1 for s in sources if s.activated)
        and cost_summary.active_plant_count == sum(1 for p in plants if p.activated)
    )
    checks.append(_check("summary_total_reconciliation", summary_ok))

    # 12. reported_ids_and_arcs_match_model_parameters
    ids_ok = (
        {s.source_id for s in sources if s.model_included} == set(parameters.source_ids)
        and {p.plant_id for p in plants} == set(parameters.plant_ids)
        and {z.zone_id for z in demand_zones} == set(parameters.zone_ids)
        and {(f.source_id, f.plant_id) for f in flows.source_to_plant}
        == set(parameters.source_plant_arcs)
        and {(f.plant_id, f.zone_id) for f in flows.plant_to_zone}
        == set(parameters.plant_zone_arcs)
    )
    checks.append(_check("reported_ids_and_arcs_match_model_parameters", ids_ok))

    for failed_check in (c for c in checks if c.passed is False):
        warnings.append(f"Output-consistency check '{failed_check.check}' failed.")

    status = "PASSED" if all(c.passed for c in checks) else "FAILED"
    return OutputValidation(status=status, tolerance=tol, checks=tuple(checks))


def postprocess_solution(
    scenario: ScenarioData,
    parameters: ModelParameters,
    problem: Any,
    variables: SolverVariables,
    *,
    input_scenario: InputScenario,
    input_validation_policy: InputValidationPolicy,
    loader_validation: LoaderValidation,
    preprocessing_validation: PreprocessingValidation,
    schema_version: str = "1.0",
    run_id: str | None = None,
    solver_version: str = "0.1",
) -> SolvedScenario:
    """Convert one solved model into a validated SolvedScenario.

    ``input_scenario``, ``input_validation_policy``, ``loader_validation``,
    and ``preprocessing_validation`` are built by earlier pipeline stages
    (data loading and preprocessing) and are passed through into the result
    unchanged. Postprocessing only computes and validates the *output*
    (solver-stage) results - see ``_run_output_consistency_checks``.
    """
    warnings: list[str] = []

    solver_status, is_feasible, is_optimal, objective_value = _extract_solver_status(problem)

    inflow_by_plant, source_plant_flows = _build_source_plant_flows(parameters, variables, warnings)
    outflow_by_plant, inflow_by_zone, plant_zone_flows = _build_plant_zone_flows(
        parameters, variables, warnings
    )
    flows = FlowResult(source_to_plant=source_plant_flows, plant_to_zone=plant_zone_flows)

    total_delivered = sum(inflow_by_zone.values())

    sources, total_source_fixed_cost, total_source_variable_cost = _build_source_results(
        scenario, parameters, variables, total_delivered, warnings
    )
    plants, total_plant_fixed_cost, total_plant_variable_cost = _build_plant_results(
        parameters, variables, inflow_by_plant, outflow_by_plant, warnings
    )
    demand_zones = _build_demand_zone_results(parameters, inflow_by_zone, warnings)
    quality = _build_quality_result(scenario, parameters, source_plant_flows, inflow_by_plant, warnings)

    summary = _build_cost_summary(
        parameters,
        sources,
        plants,
        total_delivered,
        total_source_fixed_cost,
        total_source_variable_cost,
        total_plant_fixed_cost,
        total_plant_variable_cost,
        objective_value,
        warnings,
    )

    output_consistency = _run_output_consistency_checks(
        parameters, sources, plants, demand_zones, flows, quality, summary, warnings
    )

    solver = SolverSummary(
        status=solver_status,
        is_feasible=is_feasible,
        is_optimal=is_optimal,
        objective_value=objective_value,
        version=solver_version,
    )

    validation = SolutionValidation(
        input_policy=input_validation_policy,
        loader=loader_validation,
        preprocessing=preprocessing_validation,
        output_consistency=output_consistency,
    )

    binding_constraints_summary = _build_binding_constraints_summary(sources, quality, flows)

    return SolvedScenario(
        schema_version=schema_version,
        run_id=run_id,
        scenario=input_scenario,
        validation=validation,
        solver=solver,
        summary=summary,
        sources=sources,
        plants=plants,
        demand_zones=demand_zones,
        flows=flows,
        quality=quality,
        binding_constraints_summary=binding_constraints_summary,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def print_solution_summary(solved: SolvedScenario) -> None:
    """Print a solved-scenario summary - for dev/test only, not needed in the pipeline."""
    print(f"\nSolved scenario: {solved.scenario.scenario_id}")
    print(f"Solver status: {solved.solver.status}")
    print(
        "Objective value: "
        f"{solved.solver.objective_value:g}" if solved.solver.objective_value is not None else "n/a"
    )

    print("\nSelected sources:")
    for source in solved.selected_sources:
        ratio = f"{source.blend_ratio:.2%}" if source.blend_ratio is not None else "n/a"
        print(f"- {source.source_id}: {source.withdrawal_ml_per_day:g} ML/day ({ratio})")

    if solved.unused_sources:
        print("\nUnused/excluded sources:")
        for source in solved.unused_sources:
            print(f"- {source.source_id} ({source.selection_status})")

    print("\nCost breakdown:")
    costs = solved.summary.costs
    print(f"- Source fixed cost: {costs.total_source_fixed_cost:g}")
    print(f"- Source variable cost: {costs.total_source_variable_cost:g}")
    print(f"- Plant fixed cost: {costs.total_plant_fixed_cost:g}")
    print(f"- Plant variable cost: {costs.total_plant_variable_cost:g}")
    print(f"- Reconstructed total: {costs.reconstructed_total_cost:g}")
    print(f"- Reconciles with solver objective: {costs.cost_reconciles}")

    print(f"\nOutput validation: {solved.validation.output_consistency.status}")

    if solved.binding_constraints_summary:
        print("\nBinding constraints:")
        for line in solved.binding_constraints_summary:
            print(f"- {line}")

    if solved.warnings:
        print("\nPostprocessing warnings:")
        for warning in solved.warnings:
            print(f"- {warning}")


__all__ = [
    "PostprocessingError",
    "SolverVariables",
    "hydrogen_concentration_to_ph",
    "postprocess_solution",
    "print_solution_summary",
]