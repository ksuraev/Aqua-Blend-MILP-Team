"""
This script is used for post processing after the solver is run.

- Extracts the solver status, objective value, active sources and draw values
- Cleans results and checks for Nones, near-zero values and invalid values.
- Calculates the total delivered volume and source level cost contributions
- Calculates blended turbidity and blended alkalinity after solving
- Calculates blended hydrogen-ion concentration and recovers the final blended pH
- performs validition on solver output to ensure that the objective matches the source/plant/zone output

For the purpose of the toy model, we are assuming linear blending.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

try:
    from .contracts import (
        BlendedQualityResult,
        CostBreakdown,
        DemandZoneResult,
        PlantResult,
        PlantZoneFlowResult,
        SolvedScenario,
        SourcePlantFlowResult,
        SourceResult,
    )
    from .preprocessing import ModelParameters
except ImportError:
    from contracts import (
        BlendedQualityResult,
        CostBreakdown,
        DemandZoneResult,
        PlantResult,
        PlantZoneFlowResult,
        SolvedScenario,
        SourcePlantFlowResult,
        SourceResult,
    )
    from preprocessing import ModelParameters


class PostprocessingError(RuntimeError):
    """Raised when a solved PuLP model cannot be converted into a SolvedScenario."""


_EPSILON = 1e-9
# Looser than the preprocessing epsilon: this tolerance absorbs ordinary
# solver noise (e.g. 1e-11 instead of an exact 0.0), not modelling error.
_SOLVER_TOLERANCE = 1e-6
_COST_RECONCILIATION_TOLERANCE = 1e-3

_SUPPORTED_QUALITY_INVERSE_TRANSFORMS = {
    "identity": lambda value: value,
    "ph_to_hydrogen_ion": lambda value: -math.log10(value) if value > 0 else math.nan,
}


@dataclass(frozen=True, slots=True)
class SolverVariables:
    """Class for the decision variable objects produced by the solver.
    Values can be PuLP ``LpVariable`` objects or plain numbers (for testing purposes).
    """

    source_active: dict[str, Any]
    plant_active: dict[str, Any]
    source_withdrawal: dict[str, Any]
    source_plant_flow: dict[tuple[str, str], Any]
    plant_zone_flow: dict[tuple[str, str], Any]


def hydrogen_concentration_to_ph(concentration: float) -> float:
    """Convert hydrogen-ion concentration in mol/L back into pH.

    Inverse of ``preprocessing.ph_to_hydrogen_ion``.
    """
    if not math.isfinite(concentration) or concentration <= 0:
        raise PostprocessingError(
            f"Hydrogen-ion concentration must be finite and positive, got {concentration!r}."
        )

    value = -math.log10(concentration)
    if not math.isfinite(value):
        raise PostprocessingError(
            f"Hydrogen-ion concentration {concentration!r} could not be converted "
            "back to pH safely."
        )
    return value


def _variable_value(variable: Any, label: str) -> float | None:
    """Extract a raw solved value from either a PuLP variable or a plain number."""
    if isinstance(variable, (int, float)):
        return float(variable)

    value_method = getattr(variable, "value", None)
    if callable(value_method):
        return value_method()

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
        # Within tolerance of zero but not exactly zero: solver noise.
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


def _extract_solver_status(problem: Any) -> tuple[str, float | None]:
    """Read the solver status label and objective value from a PuLP problem."""
    try:
        import pulp
    except ImportError as exc:
        raise PostprocessingError(
            'Missing dependency "pulp". Install it with: pip install pulp'
        ) from exc

    status = pulp.LpStatus.get(problem.status, str(problem.status))
    objective_value = pulp.value(problem.objective)
    if objective_value is not None:
        objective_value = float(objective_value)
    return status, objective_value


def _build_source_results(
    parameters: ModelParameters,
    variables: SolverVariables,
    total_delivered_ml_per_day: float,
    warnings: list[str],
) -> tuple[tuple[SourceResult, ...], float, float]:
    results: list[SourceResult] = []
    total_fixed_cost = 0.0
    total_withdrawal_cost = 0.0

    for source_id in parameters.source_ids:
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
        withdrawal_cost_contribution = (
            parameters.source_unit_cost[source_id] * withdrawal
        )
        total_fixed_cost += fixed_cost_contribution
        total_withdrawal_cost += withdrawal_cost_contribution

        blend_ratio = (
            withdrawal / total_delivered_ml_per_day
            if total_delivered_ml_per_day > _EPSILON
            else None
        )

        results.append(
            SourceResult(
                source_id=source_id,
                is_selected=is_selected,
                withdrawal_ml_per_day=withdrawal,
                blend_ratio=blend_ratio,
                fixed_cost_contribution=fixed_cost_contribution,
                withdrawal_cost_contribution=withdrawal_cost_contribution,
            )
        )

    return tuple(results), total_fixed_cost, total_withdrawal_cost


def _build_source_plant_flow_results(
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
        results.append(
            SourcePlantFlowResult(
                source_id=source_id,
                plant_id=plant_id,
                is_active=flow > _EPSILON,
                flow_ml_per_day=flow,
            )
        )

    return inflow_by_plant, tuple(results)


def _build_plant_zone_flow_results(
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
        results.append(
            PlantZoneFlowResult(
                plant_id=plant_id,
                zone_id=zone_id,
                is_active=flow > _EPSILON,
                flow_ml_per_day=flow,
            )
        )

    return outflow_by_plant, inflow_by_zone, tuple(results)


def _build_plant_results(
    parameters: ModelParameters,
    variables: SolverVariables,
    inflow_by_plant: dict[str, float],
    outflow_by_plant: dict[str, float],
    warnings: list[str],
) -> tuple[tuple[PlantResult, ...], float, float]:
    results: list[PlantResult] = []
    total_fixed_cost = 0.0
    total_treatment_cost = 0.0

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

        fixed_cost_contribution = (
            parameters.plant_fixed_cost[plant_id] if is_active else 0.0
        )
        treatment_cost_contribution = (
            parameters.plant_unit_treatment_cost[plant_id] * throughput
        )
        total_fixed_cost += fixed_cost_contribution
        total_treatment_cost += treatment_cost_contribution

        results.append(
            PlantResult(
                plant_id=plant_id,
                is_active=is_active,
                throughput_ml_per_day=throughput,
                fixed_cost_contribution=fixed_cost_contribution,
                treatment_cost_contribution=treatment_cost_contribution,
            )
        )

    return tuple(results), total_fixed_cost, total_treatment_cost


def _build_demand_zone_results(
    parameters: ModelParameters,
    inflow_by_zone: dict[str, float],
    warnings: list[str],
) -> tuple[DemandZoneResult, ...]:
    results: list[DemandZoneResult] = []

    for zone_id in parameters.zone_ids:
        demand = parameters.demand_by_zone[zone_id]
        delivered = inflow_by_zone[zone_id]
        if abs(delivered - demand) > _SOLVER_TOLERANCE:
            warnings.append(
                f"Demand zone '{zone_id}' delivered volume ({delivered:g} ML/day) does "
                f"not match its demand ({demand:g} ML/day)."
            )
        results.append(
            DemandZoneResult(
                zone_id=zone_id,
                demand_ml_per_day=demand,
                delivered_ml_per_day=delivered,
            )
        )

    return tuple(results)


def _build_blended_quality_results(
    parameters: ModelParameters,
    source_plant_flows: tuple[SourcePlantFlowResult, ...],
    inflow_by_plant: dict[str, float],
    warnings: list[str],
) -> tuple[BlendedQualityResult, ...]:
    """Blend source quality by solved flow and recover raw (reported) units.

    Model-space blending happens in the transformed units used by
    ``ModelParameters`` (e.g. hydrogen-ion concentration), because that is the
    space in which the transform is linear in volume. The result is then
    converted back into the scenario's original units for reporting.
    """
    quality_parameters: dict[str, dict[str, Any]] = scenario.quality_limits["parameters"]
    inflow_by_plant_source: dict[str, list[SourcePlantFlowResult]] = {
        plant_id: [] for plant_id in parameters.plant_ids
    }
    for flow in source_plant_flows:
        if flow.is_active:
            inflow_by_plant_source[flow.plant_id].append(flow)

    results: list[BlendedQualityResult] = []

    for plant_id in parameters.plant_ids:
        total_inflow = inflow_by_plant[plant_id]
        contributing_flows = inflow_by_plant_source[plant_id]

        for raw_name, specification in quality_parameters.items():
            transform = str(specification.get("transform", "identity"))
            model_name = _model_quality_name(parameters, raw_name, transform)

            if total_inflow <= _EPSILON:
                warnings.append(
                    f"Plant '{plant_id}' has no inflow; blended '{raw_name}' quality "
                    "could not be computed and was omitted."
                )
                continue

            blended_model_value = sum(
                parameters.source_quality[(flow.source_id, model_name)]
                * flow.flow_ml_per_day
                for flow in contributing_flows
            ) / total_inflow

            inverse_transform = _SUPPORTED_QUALITY_INVERSE_TRANSFORMS.get(transform)
            if inverse_transform is None:
                raise PostprocessingError(
                    f"Unsupported quality transform '{transform}' for '{raw_name}'."
                )
            blended_raw_value = inverse_transform(blended_model_value)
            if not math.isfinite(blended_raw_value):
                raise PostprocessingError(
                    f"Blended '{raw_name}' quality at plant '{plant_id}' could not be "
                    "converted back to its reported unit."
                )

            lower_limit = float(specification["min"])
            upper_limit = float(specification["max"])
            lower_margin = blended_raw_value - lower_limit
            upper_margin = upper_limit - blended_raw_value
            is_binding = (
                lower_margin <= _SOLVER_TOLERANCE or upper_margin <= _SOLVER_TOLERANCE
            )

            results.append(
                BlendedQualityResult(
                    plant_id=plant_id,
                    parameter_id=raw_name,
                    unit=str(specification.get("unit", "")),
                    blended_value=blended_raw_value,
                    lower_limit=lower_limit,
                    upper_limit=upper_limit,
                    lower_margin=lower_margin,
                    upper_margin=upper_margin,
                    is_binding=is_binding,
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
        "hydrogen_ion_concentration_mol_l" if transform == "ph_to_hydrogen_ion" else raw_name
    )
    if default_model_name in parameters.quality_parameter_ids:
        return default_model_name

    # Fall back to a direct match, covering scenarios with a custom model_name.
    for candidate in parameters.quality_parameter_ids:
        if candidate == raw_name:
            return candidate

    raise PostprocessingError(
        f"Could not resolve model-space quality identifier for '{raw_name}'."
    )


def _build_cost_breakdown(
    total_source_fixed_cost: float,
    total_source_withdrawal_cost: float,
    total_plant_fixed_cost: float,
    total_plant_treatment_cost: float,
    objective_value: float | None,
    warnings: list[str],
) -> CostBreakdown:
    reconstructed_total_cost = (
        total_source_fixed_cost
        + total_source_withdrawal_cost
        + total_plant_fixed_cost
        + total_plant_treatment_cost
    )

    cost_reconciles = True
    if objective_value is not None:
        cost_reconciles = (
            abs(reconstructed_total_cost - objective_value)
            <= _COST_RECONCILIATION_TOLERANCE
        )
        if not cost_reconciles:
            warnings.append(
                "Reconstructed total cost "
                f"({reconstructed_total_cost:g}) does not match the solver's "
                f"objective value ({objective_value:g})."
            )

    return CostBreakdown(
        total_source_fixed_cost=total_source_fixed_cost,
        total_source_withdrawal_cost=total_source_withdrawal_cost,
        total_plant_fixed_cost=total_plant_fixed_cost,
        total_plant_treatment_cost=total_plant_treatment_cost,
        reconstructed_total_cost=reconstructed_total_cost,
        solver_objective_value=objective_value,
        cost_reconciles=cost_reconciles,
    )


def postprocess_solution(
    parameters: ModelParameters,
    problem: Any,
    variables: SolverVariables,
) -> SolvedScenario:
    """Convert one solved PuLP model into a validated SolvedScenario."""
    warnings: list[str] = []

    solver_status, objective_value = _extract_solver_status(problem)

    inflow_by_plant, source_plant_flows = _build_source_plant_flow_results(
        parameters, variables, warnings
    )
    outflow_by_plant, inflow_by_zone, plant_zone_flows = _build_plant_zone_flow_results(
        parameters, variables, warnings
    )

    total_delivered = sum(inflow_by_zone.values())

    sources, total_source_fixed_cost, total_source_withdrawal_cost = _build_source_results(
        parameters, variables, total_delivered, warnings
    )
    plants, total_plant_fixed_cost, total_plant_treatment_cost = _build_plant_results(
        parameters, variables, inflow_by_plant, outflow_by_plant, warnings
    )
    demand_zones = _build_demand_zone_results(
        parameters, inflow_by_zone, warnings
    )
    blended_quality = _build_blended_quality_results(
        parameters, source_plant_flows, inflow_by_plant, warnings
    )
    cost_breakdown = _build_cost_breakdown(
        total_source_fixed_cost,
        total_source_withdrawal_cost,
        total_plant_fixed_cost,
        total_plant_treatment_cost,
        objective_value,
        warnings,
    )

    return SolvedScenario(
        scenario_id=parameters.scenario_id,
        solver_status=solver_status,
        objective_value=objective_value,
        sources=sources,
        plants=plants,
        demand_zones=demand_zones,
        source_to_plant_flows=source_plant_flows,
        plant_to_zone_flows=plant_zone_flows,
        blended_quality=blended_quality,
        cost_breakdown=cost_breakdown,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def print_solution_summary(solved: SolvedScenario) -> None:
    """Print a solved-scenario summary - for dev/test only, not needed in the pipeline."""
    print(f"\nSolved scenario: {solved.scenario_id}")
    print(f"Solver status: {solved.solver_status}")
    print(
        "Objective value: "
        f"{solved.objective_value:g}" if solved.objective_value is not None else "n/a"
    )

    print("\nSelected sources:")
    for source in solved.selected_sources:
        ratio = f"{source.blend_ratio:.2%}" if source.blend_ratio is not None else "n/a"
        print(f"- {source.source_id}: {source.withdrawal_ml_per_day:g} ML/day ({ratio})")

    if solved.unused_sources:
        print("\nUnused sources:")
        for source in solved.unused_sources:
            print(f"- {source.source_id}")

    print("\nCost breakdown:")
    breakdown = solved.cost_breakdown
    print(f"- Source fixed cost: {breakdown.total_source_fixed_cost:g}")
    print(f"- Source withdrawal cost: {breakdown.total_source_withdrawal_cost:g}")
    print(f"- Plant fixed cost: {breakdown.total_plant_fixed_cost:g}")
    print(f"- Plant treatment cost: {breakdown.total_plant_treatment_cost:g}")
    print(f"- Reconstructed total: {breakdown.reconstructed_total_cost:g}")
    print(f"- Reconciles with solver objective: {breakdown.cost_reconciles}")

    if solved.binding_quality_limits:
        print("\nBinding quality limits:")
        for result in solved.binding_quality_limits:
            print(
                f"- {result.plant_id}.{result.parameter_id}: "
                f"{result.blended_value:g} {result.unit} "
                f"(limits {result.lower_limit:g}-{result.upper_limit:g})"
            )

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