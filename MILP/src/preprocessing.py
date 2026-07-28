from __future__ import annotations

import argparse
import math
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Hashable, Iterable

try:
    # Package import used when the module is called through MILP.src.
    from .data_loader import DataLoadError, ScenarioData, load_scenario
except ImportError:  # pragma: no cover - supports direct script execution.
    from data_loader import DataLoadError, ScenarioData, load_scenario


class PreprocessingError(RuntimeError):
    """Raised when validated input cannot be converted into MILP parameters."""


# Canonical quality identifiers used by the model-facing parameter dictionaries.
PH_PARAMETER = "hydrogen_ion_concentration_mol_l"
ALKALINITY_PARAMETER = "alkalinity_mg_l_caco3"
TURBIDITY_PARAMETER = "turbidity_ntu"

QUALITY_PARAMETER_ORDER = (
    PH_PARAMETER,
    ALKALINITY_PARAMETER,
    TURBIDITY_PARAMETER,
)

_EPSILON = 1e-9


@dataclass(frozen=True)
class ModelParameters:
    """Complete, non-null parameter set required by the MILP formulation.

    The field names are descriptive Python equivalents of the notation used in
    the formulation document. This object contains no decision variables,
    objective terms, constraints, solver objects, or optimisation logic.
    """

    # Sets: S, T, Z and P.
    source_ids: tuple[str, ...]
    plant_ids: tuple[str, ...]
    zone_ids: tuple[str, ...]
    quality_parameter_ids: tuple[str, ...]

    # Existing network arcs over which the model may create flows.
    source_plant_arcs: tuple[tuple[str, str], ...]
    plant_zone_arcs: tuple[tuple[str, str], ...]

    # Formulation parameters D_z, F_s, F_t, C_s, C_t, W_s and V_t.
    demand_by_zone: dict[str, float]
    source_fixed_cost: dict[str, float]
    plant_fixed_cost: dict[str, float]
    source_unit_cost: dict[str, float]
    plant_unit_treatment_cost: dict[str, float]
    source_max_withdrawal: dict[str, float]
    plant_max_throughput: dict[str, float]

    # Link capacities L_st and L_tz.
    source_plant_link_capacity: dict[tuple[str, str], float]
    plant_zone_link_capacity: dict[tuple[str, str], float]

    # Water-quality values Q_sp and the lower and upper bounds for each p.
    source_quality: dict[tuple[str, str], float]
    quality_lower_bound: dict[str, float]
    quality_upper_bound: dict[str, float]
    quality_units: dict[str, str]

    # Informational notes that do not prevent model construction.
    warnings: tuple[str, ...]

    def as_formulation_dict(self) -> dict[str, Any]:
        """Expose the parameter object using the formulation's mathematical names."""
        return {
            "S": self.source_ids,
            "T": self.plant_ids,
            "Z": self.zone_ids,
            "P": self.quality_parameter_ids,
            "A_ST": self.source_plant_arcs,
            "A_TZ": self.plant_zone_arcs,
            "D_z": self.demand_by_zone,
            "F_s": self.source_fixed_cost,
            "F_t": self.plant_fixed_cost,
            "C_s": self.source_unit_cost,
            "C_t": self.plant_unit_treatment_cost,
            "W_s": self.source_max_withdrawal,
            "V_t": self.plant_max_throughput,
            "L_st": self.source_plant_link_capacity,
            "L_tz": self.plant_zone_link_capacity,
            "Q_sp": self.source_quality,
            "Q_lower_p": self.quality_lower_bound,
            "Q_upper_p": self.quality_upper_bound,
        }


@dataclass(frozen=True)
class _QualityRule:
    """Normalised quality-limit definition used during transformation."""

    raw_name: str
    model_name: str
    unit: str
    minimum: float
    maximum: float
    transform: str


@dataclass
class _FlowEdge:
    to: int
    reverse_index: int
    capacity: float


class _Dinic:
    """Small maximum-flow implementation used for pre-solver feasibility checks."""

    def __init__(self) -> None:
        self._node_index: dict[Hashable, int] = {}
        self._graph: list[list[_FlowEdge]] = []

    def _index(self, node: Hashable) -> int:
        if node not in self._node_index:
            self._node_index[node] = len(self._graph)
            self._graph.append([])
        return self._node_index[node]

    def add_edge(self, start: Hashable, end: Hashable, capacity: float) -> None:
        start_index = self._index(start)
        end_index = self._index(end)
        forward = _FlowEdge(end_index, len(self._graph[end_index]), capacity)
        reverse = _FlowEdge(start_index, len(self._graph[start_index]), 0.0)
        self._graph[start_index].append(forward)
        self._graph[end_index].append(reverse)

    def maximum_flow(self, source: Hashable, sink: Hashable) -> float:
        source_index = self._index(source)
        sink_index = self._index(sink)
        total_flow = 0.0

        while True:
            levels = [-1] * len(self._graph)
            levels[source_index] = 0
            queue: deque[int] = deque([source_index])

            while queue:
                current = queue.popleft()
                for edge in self._graph[current]:
                    if edge.capacity > _EPSILON and levels[edge.to] < 0:
                        levels[edge.to] = levels[current] + 1
                        queue.append(edge.to)

            if levels[sink_index] < 0:
                return total_flow

            next_edge = [0] * len(self._graph)

            def send_flow(node: int, available: float) -> float:
                if node == sink_index:
                    return available

                while next_edge[node] < len(self._graph[node]):
                    edge_index = next_edge[node]
                    edge = self._graph[node][edge_index]

                    if (
                        edge.capacity > _EPSILON
                        and levels[edge.to] == levels[node] + 1
                    ):
                        sent = send_flow(edge.to, min(available, edge.capacity))
                        if sent > _EPSILON:
                            edge.capacity -= sent
                            reverse = self._graph[edge.to][edge.reverse_index]
                            reverse.capacity += sent
                            return sent

                    next_edge[node] += 1

                return 0.0

            while True:
                sent = send_flow(source_index, math.inf)
                if sent <= _EPSILON:
                    break
                total_flow += sent


def ph_to_hydrogen_ion(ph: float) -> float:
    """Convert pH into hydrogen-ion concentration in mol/L."""
    value = 10.0 ** (-ph)
    if not math.isfinite(value) or value <= 0:
        raise PreprocessingError(f"pH {ph!r} could not be transformed safely.")
    return value


def _require_finite(value: float | None, label: str) -> float:
    if value is None:
        raise PreprocessingError(f"{label} is missing after data loading.")

    converted = float(value)
    if not math.isfinite(converted):
        raise PreprocessingError(f"{label} must be finite.")

    return converted


def _require_non_negative(value: float | None, label: str) -> float:
    converted = _require_finite(value, label)
    if converted < 0:
        raise PreprocessingError(f"{label} must be greater than or equal to zero.")
    return converted


def _ensure_unique(values: Iterable[str], label: str) -> tuple[str, ...]:
    ordered = tuple(values)
    if len(ordered) != len(set(ordered)):
        raise PreprocessingError(f"Duplicate {label} were detected during preprocessing.")
    return ordered


def _quality_entry(
    parameters: dict[str, Any],
    aliases: tuple[str, ...],
    label: str,
) -> dict[str, Any]:
    for alias in aliases:
        value = parameters.get(alias)
        if isinstance(value, dict):
            return value
    raise PreprocessingError(f"Quality limits for {label} are missing.")


def _normalise_quality_rules(
    quality_limits: dict[str, Any],
) -> tuple[tuple[_QualityRule, ...], list[str]]:
    """Normalise the current contract while retaining limited legacy support."""
    if not isinstance(quality_limits, dict):
        raise PreprocessingError("quality_limits must be a JSON object.")

    warnings: list[str] = []
    nested_parameters = quality_limits.get("parameters")

    if isinstance(nested_parameters, dict):
        applies_to = str(quality_limits.get("applies_to", "")).strip()
        if applies_to != "blend_at_plant_inflow":
            raise PreprocessingError(
                'quality_limits.applies_to must equal "blend_at_plant_inflow".'
            )
        parameters = nested_parameters
        uses_legacy_schema = False
    else:
        # This branch supports older ScenarioData objects while the JSON contract
        # and loader are being aligned. New scenarios should use parameters.<p>.
        parameters = quality_limits
        uses_legacy_schema = True
        warnings.append(
            "Legacy flat quality_limits schema detected; migrate to "
            "quality_limits.parameters before the interface is finalised."
        )

    specifications = (
        (
            ("pH", "ph"),
            "pH",
            PH_PARAMETER,
            "ph_to_hydrogen_ion",
            "mol/L",
        ),
        (
            ("alkalinity", "alkalinity_mg_l_caco3"),
            "alkalinity",
            ALKALINITY_PARAMETER,
            "identity",
            "mg/L CaCO3",
        ),
        (
            ("turbidity", "turbidity_ntu"),
            "turbidity",
            TURBIDITY_PARAMETER,
            "identity",
            "NTU",
        ),
    )

    rules: list[_QualityRule] = []

    for aliases, label, model_name, expected_transform, transformed_unit in specifications:
        entry = _quality_entry(parameters, aliases, label)

        minimum_key = "min" if "min" in entry else "minimum"
        maximum_key = "max" if "max" in entry else "maximum"
        minimum = _require_finite(entry.get(minimum_key), f"{label} minimum limit")
        maximum = _require_finite(entry.get(maximum_key), f"{label} maximum limit")

        if minimum > maximum:
            raise PreprocessingError(
                f"{label} minimum quality limit exceeds its maximum."
            )

        if label == "pH":
            if not 0 <= minimum <= 10 or not 0 <= maximum <= 10:
                raise PreprocessingError("pH quality limits must be between 0 and 10.")
        elif minimum < 0 or maximum < 0:
            raise PreprocessingError(f"{label} quality limits must be non-negative.")

        if uses_legacy_schema:
            transform = expected_transform
            unit = str(entry.get("unit", transformed_unit)).strip() or transformed_unit
        else:
            transform = str(entry.get("transform", "")).strip()
            unit = str(entry.get("unit", "")).strip()
            if not unit:
                raise PreprocessingError(f"{label} quality-limit unit is required.")
            if transform != expected_transform:
                raise PreprocessingError(
                    f'{label} transform must be "{expected_transform}".'
                )

        rules.append(
            _QualityRule(
                raw_name=label,
                model_name=model_name,
                unit=transformed_unit if label == "pH" else unit,
                minimum=minimum,
                maximum=maximum,
                transform=transform,
            )
        )

    return tuple(rules), warnings


def _transform_quality_bounds(
    rules: tuple[_QualityRule, ...],
) -> tuple[dict[str, float], dict[str, float], dict[str, str]]:
    lower: dict[str, float] = {}
    upper: dict[str, float] = {}
    units: dict[str, str] = {}

    for rule in rules:
        if rule.transform == "ph_to_hydrogen_ion":
            # pH decreases as [H+] increases, so the transformed bounds reverse.
            lower_value = ph_to_hydrogen_ion(rule.maximum)
            upper_value = ph_to_hydrogen_ion(rule.minimum)
        elif rule.transform == "identity":
            lower_value = rule.minimum
            upper_value = rule.maximum
        else:
            raise PreprocessingError(
                f'Unsupported quality transform "{rule.transform}" for {rule.raw_name}.'
            )

        if lower_value > upper_value:
            raise PreprocessingError(
                f"Transformed {rule.raw_name} limits are not correctly ordered."
            )

        lower[rule.model_name] = lower_value
        upper[rule.model_name] = upper_value
        units[rule.model_name] = rule.unit

    return lower, upper, units


def _build_source_parameters(
    scenario: ScenarioData,
) -> tuple[
    tuple[str, ...],
    dict[str, float],
    dict[str, float],
    dict[str, float],
    dict[tuple[str, str], float],
    list[str],
]:
    source_ids = _ensure_unique(
        (source.source_id for source in scenario.sources),
        "source IDs",
    )
    if not source_ids:
        raise PreprocessingError("The scenario must contain at least one enabled source.")

    capacity: dict[str, float] = {}
    fixed_cost: dict[str, float] = {}
    unit_cost: dict[str, float] = {}
    quality: dict[tuple[str, str], float] = {}
    warnings: list[str] = []

    for source in scenario.sources:
        available = _require_non_negative(
            source.max_available_ml_per_day,
            f"Source '{source.source_id}' maximum withdrawal W_s",
        )
        fixed_cost[source.source_id] = _require_non_negative(
            source.fixed_activation_cost,
            f"Source '{source.source_id}' activation cost F_s",
        )
        unit_cost[source.source_id] = _require_non_negative(
            source.cost_per_ml,
            f"Source '{source.source_id}' unit cost C_s",
        )

        # A forced-inactive source remains visible in S but cannot supply water.
        capacity[source.source_id] = 0.0 if source.forced_inactive else available

        ph = _require_finite(source.ph, f"Source '{source.source_id}' pH")
        if not 0 <= ph <= 10:
            raise PreprocessingError(
                f"Source '{source.source_id}' pH must be between 0 and 10."
            )

        alkalinity = _require_non_negative(
            source.alkalinity_mg_l_caco3,
            f"Source '{source.source_id}' alkalinity",
        )
        turbidity = _require_non_negative(
            source.turbidity_ntu,
            f"Source '{source.source_id}' turbidity",
        )

        quality[(source.source_id, PH_PARAMETER)] = ph_to_hydrogen_ion(ph)
        quality[(source.source_id, ALKALINITY_PARAMETER)] = alkalinity
        quality[(source.source_id, TURBIDITY_PARAMETER)] = turbidity

        if not source.database_model_ready:
            warnings.append(
                f"Source '{source.source_id}' is not marked model_ready by the database; "
                "required scenario overrides and field-level validation were still applied."
            )

    return source_ids, capacity, fixed_cost, unit_cost, quality, warnings


def _build_plant_parameters(
    scenario: ScenarioData,
) -> tuple[
    tuple[str, ...],
    dict[str, float],
    dict[str, float],
    dict[str, float],
]:
    plant_ids = _ensure_unique(
        (plant.plant_id for plant in scenario.plants),
        "plant IDs",
    )
    if not plant_ids:
        raise PreprocessingError("The scenario must contain at least one enabled plant.")

    capacity: dict[str, float] = {}
    fixed_cost: dict[str, float] = {}
    treatment_cost: dict[str, float] = {}

    for plant in scenario.plants:
        minimum_flow = _require_non_negative(
            plant.minimum_operating_flow_ml_per_day,
            f"Plant '{plant.plant_id}' minimum operating flow",
        )
        maximum_flow = _require_non_negative(
            plant.maximum_processing_capacity_ml_per_day,
            f"Plant '{plant.plant_id}' maximum throughput V_t",
        )

        if minimum_flow > maximum_flow:
            raise PreprocessingError(
                f"Plant '{plant.plant_id}' minimum flow exceeds its maximum throughput."
            )

        # The current formulation has no semi-continuous minimum-throughput rule.
        if minimum_flow > _EPSILON:
            raise PreprocessingError(
                f"Plant '{plant.plant_id}' has a non-zero minimum operating flow, "
                "but this parameter is not represented in the current formulation."
            )

        capacity[plant.plant_id] = maximum_flow
        fixed_cost[plant.plant_id] = _require_non_negative(
            plant.fixed_activation_cost,
            f"Plant '{plant.plant_id}' activation cost F_t",
        )
        treatment_cost[plant.plant_id] = _require_non_negative(
            plant.treatment_cost_per_ml,
            f"Plant '{plant.plant_id}' treatment cost C_t",
        )

    return plant_ids, capacity, fixed_cost, treatment_cost


def _build_demand_parameters(
    scenario: ScenarioData,
) -> tuple[tuple[str, ...], dict[str, float]]:
    zone_ids = _ensure_unique(
        (zone.zone_id for zone in scenario.demand_zones),
        "demand-zone IDs",
    )
    if not zone_ids:
        raise PreprocessingError("The scenario must contain at least one demand zone.")

    demand: dict[str, float] = {}
    for zone in scenario.demand_zones:
        if not zone.demand_must_be_met:
            raise PreprocessingError(
                f"Demand zone '{zone.zone_id}' allows unmet demand, but the current "
                "formulation requires D_z to be fully satisfied."
            )
        demand[zone.zone_id] = _require_non_negative(
            zone.demand_ml_per_day,
            f"Demand zone '{zone.zone_id}' demand D_z",
        )

    return zone_ids, demand


def _build_link_parameters(
    scenario: ScenarioData,
    source_ids: tuple[str, ...],
    plant_ids: tuple[str, ...],
    zone_ids: tuple[str, ...],
) -> tuple[
    tuple[tuple[str, str], ...],
    tuple[tuple[str, str], ...],
    dict[tuple[str, str], float],
    dict[tuple[str, str], float],
]:
    source_set = set(source_ids)
    plant_set = set(plant_ids)
    zone_set = set(zone_ids)

    source_plant_capacity: dict[tuple[str, str], float] = {}
    for link in scenario.source_to_plant_links:
        key = (link.source_id, link.plant_id)
        if key in source_plant_capacity:
            raise PreprocessingError(f"Duplicate source-to-plant arc {key!r}.")
        if link.source_id not in source_set:
            raise PreprocessingError(
                f"Source-to-plant arc {key!r} references an unknown source."
            )
        if link.plant_id not in plant_set:
            raise PreprocessingError(
                f"Source-to-plant arc {key!r} references an unknown plant."
            )
        source_plant_capacity[key] = _require_non_negative(
            link.maximum_flow_ml_per_day,
            f"Source-to-plant capacity L_st for {link.source_id} -> {link.plant_id}",
        )

    plant_zone_capacity: dict[tuple[str, str], float] = {}
    for link in scenario.plant_to_zone_links:
        key = (link.plant_id, link.zone_id)
        if key in plant_zone_capacity:
            raise PreprocessingError(f"Duplicate plant-to-zone arc {key!r}.")
        if link.plant_id not in plant_set:
            raise PreprocessingError(
                f"Plant-to-zone arc {key!r} references an unknown plant."
            )
        if link.zone_id not in zone_set:
            raise PreprocessingError(
                f"Plant-to-zone arc {key!r} references an unknown zone."
            )
        plant_zone_capacity[key] = _require_non_negative(
            link.maximum_flow_ml_per_day,
            f"Plant-to-zone capacity L_tz for {link.plant_id} -> {link.zone_id}",
        )

    if not source_plant_capacity:
        raise PreprocessingError("At least one source-to-plant arc is required.")
    if not plant_zone_capacity:
        raise PreprocessingError("At least one plant-to-zone arc is required.")

    return (
        tuple(source_plant_capacity),
        tuple(plant_zone_capacity),
        source_plant_capacity,
        plant_zone_capacity,
    )


def _validate_capacity_feasibility(parameters: ModelParameters) -> None:
    """Reject networks that cannot route all demand even before quality is applied."""
    total_demand = sum(parameters.demand_by_zone.values())
    if total_demand <= _EPSILON:
        return

    flow = _Dinic()
    super_source = ("system", "source")
    super_sink = ("system", "sink")

    for source_id, capacity in parameters.source_max_withdrawal.items():
        flow.add_edge(super_source, ("source", source_id), capacity)

    for plant_id, capacity in parameters.plant_max_throughput.items():
        # Splitting the plant node applies V_t to all water processed by that plant.
        flow.add_edge(("plant_in", plant_id), ("plant_out", plant_id), capacity)

    for (source_id, plant_id), capacity in (
        parameters.source_plant_link_capacity.items()
    ):
        flow.add_edge(("source", source_id), ("plant_in", plant_id), capacity)

    for (plant_id, zone_id), capacity in parameters.plant_zone_link_capacity.items():
        flow.add_edge(("plant_out", plant_id), ("zone", zone_id), capacity)

    for zone_id, demand in parameters.demand_by_zone.items():
        flow.add_edge(("zone", zone_id), super_sink, demand)

    maximum_routable = flow.maximum_flow(super_source, super_sink)
    if maximum_routable + _EPSILON < total_demand:
        raise PreprocessingError(
            "The enabled network cannot route the full demand before water-quality "
            f"constraints are applied: routable={maximum_routable:g} ML/day, "
            f"required={total_demand:g} ML/day."
        )


def _plant_is_potentially_quality_capable(
    parameters: ModelParameters,
    plant_id: str,
) -> bool:
    connected_sources = [
        source_id
        for (source_id, linked_plant), link_capacity in (
            parameters.source_plant_link_capacity.items()
        )
        if (
            linked_plant == plant_id
            and link_capacity > _EPSILON
            and parameters.source_max_withdrawal[source_id] > _EPSILON
        )
    ]

    if not connected_sources:
        return False

    # A volume-weighted blend lies inside the connected sources' convex range
    # for each individual parameter. This is a necessary screening condition.
    for parameter_id in parameters.quality_parameter_ids:
        values = [
            parameters.source_quality[(source_id, parameter_id)]
            for source_id in connected_sources
        ]
        if max(values) + _EPSILON < parameters.quality_lower_bound[parameter_id]:
            return False
        if min(values) - _EPSILON > parameters.quality_upper_bound[parameter_id]:
            return False

    return True


def _validate_quality_feasibility(parameters: ModelParameters) -> None:
    """Apply safe necessary checks without attempting to solve Equation 13."""
    usable_sources = [
        source_id
        for source_id, capacity in parameters.source_max_withdrawal.items()
        if capacity > _EPSILON
    ]
    if not usable_sources and sum(parameters.demand_by_zone.values()) > _EPSILON:
        raise PreprocessingError("No usable source remains after forced-inactive rules.")

    for parameter_id in parameters.quality_parameter_ids:
        values = [
            parameters.source_quality[(source_id, parameter_id)]
            for source_id in usable_sources
        ]
        lower = parameters.quality_lower_bound[parameter_id]
        upper = parameters.quality_upper_bound[parameter_id]

        if max(values) + _EPSILON < lower or min(values) - _EPSILON > upper:
            raise PreprocessingError(
                f"No available source blend can satisfy the global bounds for "
                f"'{parameter_id}'."
            )

    capable_plants = {
        plant_id
        for plant_id in parameters.plant_ids
        if (
            parameters.plant_max_throughput[plant_id] > _EPSILON
            and _plant_is_potentially_quality_capable(parameters, plant_id)
        )
    }

    for zone_id, demand in parameters.demand_by_zone.items():
        if demand <= _EPSILON:
            continue
        candidate_plants = {
            plant_id
            for (plant_id, linked_zone), capacity in (
                parameters.plant_zone_link_capacity.items()
            )
            if linked_zone == zone_id and capacity > _EPSILON
        }
        if not candidate_plants.intersection(capable_plants):
            raise PreprocessingError(
                f"Demand zone '{zone_id}' has no upstream plant with a "
                "potentially compliant source blend."
            )


def _build_warnings(parameters: ModelParameters) -> tuple[str, ...]:
    warnings = list(parameters.warnings)

    connected_sources = {
        source_id for source_id, _ in parameters.source_plant_arcs
    }
    for source_id in parameters.source_ids:
        if source_id not in connected_sources:
            warnings.append(
                f"Source '{source_id}' has no outgoing source-to-plant arc and cannot be used."
            )

    incoming_plants = {plant_id for _, plant_id in parameters.source_plant_arcs}
    outgoing_plants = {plant_id for plant_id, _ in parameters.plant_zone_arcs}
    for plant_id in parameters.plant_ids:
        if plant_id not in incoming_plants or plant_id not in outgoing_plants:
            warnings.append(
                f"Plant '{plant_id}' is disconnected on at least one side of the network."
            )

    return tuple(dict.fromkeys(warnings))


def preprocess_scenario(
    scenario: ScenarioData,
    *,
    validate_capacity_feasibility: bool = True,
    validate_quality_feasibility: bool = True,
) -> ModelParameters:
    """Convert one strictly loaded scenario into formulation-ready parameters."""
    if scenario.validation_issues:
        issues = "\n".join(f"- {issue}" for issue in scenario.validation_issues)
        raise PreprocessingError(
            "Preprocessing requires a scenario with no loader validation issues:\n"
            + issues
        )

    source_ids, source_capacity, source_fixed, source_unit, source_quality, warnings = (
        _build_source_parameters(scenario)
    )
    plant_ids, plant_capacity, plant_fixed, treatment_cost = (
        _build_plant_parameters(scenario)
    )
    zone_ids, demand = _build_demand_parameters(scenario)

    (
        source_plant_arcs,
        plant_zone_arcs,
        source_plant_capacity,
        plant_zone_capacity,
    ) = _build_link_parameters(scenario, source_ids, plant_ids, zone_ids)

    quality_rules, quality_warnings = _normalise_quality_rules(
        scenario.quality_limits
    )
    quality_lower, quality_upper, quality_units = _transform_quality_bounds(
        quality_rules
    )

    parameters = ModelParameters(
        source_ids=source_ids,
        plant_ids=plant_ids,
        zone_ids=zone_ids,
        quality_parameter_ids=QUALITY_PARAMETER_ORDER,
        source_plant_arcs=source_plant_arcs,
        plant_zone_arcs=plant_zone_arcs,
        demand_by_zone=demand,
        source_fixed_cost=source_fixed,
        plant_fixed_cost=plant_fixed,
        source_unit_cost=source_unit,
        plant_unit_treatment_cost=treatment_cost,
        source_max_withdrawal=source_capacity,
        plant_max_throughput=plant_capacity,
        source_plant_link_capacity=source_plant_capacity,
        plant_zone_link_capacity=plant_zone_capacity,
        source_quality=source_quality,
        quality_lower_bound=quality_lower,
        quality_upper_bound=quality_upper,
        quality_units=quality_units,
        warnings=tuple(warnings + quality_warnings),
    )

    if validate_capacity_feasibility:
        _validate_capacity_feasibility(parameters)
    if validate_quality_feasibility:
        _validate_quality_feasibility(parameters)

    # Add non-blocking structural notes only after all model-readiness checks pass.
    return ModelParameters(
        **{
            **parameters.__dict__,
            "warnings": _build_warnings(parameters),
        }
    )


def print_parameter_summary(parameters: ModelParameters) -> None:
    """Print a compact model-parameter summary for local verification."""
    print("\nAquaBlend preprocessing completed")
    print(f"Sources (S): {len(parameters.source_ids)}")
    print(f"Plants (T): {len(parameters.plant_ids)}")
    print(f"Demand zones (Z): {len(parameters.zone_ids)}")
    print(f"Quality parameters (P): {len(parameters.quality_parameter_ids)}")
    print(f"Source-to-plant arcs: {len(parameters.source_plant_arcs)}")
    print(f"Plant-to-zone arcs: {len(parameters.plant_zone_arcs)}")
    print(f"Total demand: {sum(parameters.demand_by_zone.values()):g} ML/day")
    print(
        "Total usable source capacity: "
        f"{sum(parameters.source_max_withdrawal.values()):g} ML/day"
    )

    if parameters.warnings:
        print("\nPreprocessing warnings:")
        for warning in parameters.warnings:
            print(f"- {warning}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Load and preprocess an AquaBlend scenario into MILP parameters."
        )
    )
    parser.add_argument(
        "scenario",
        nargs="?",
        default="config/scenarios/base_scenarios_v1.json",
        help="Path to the scenario JSON file.",
    )
    parser.add_argument(
        "--skip-capacity-check",
        action="store_true",
        help="Skip the pre-solver network-capacity feasibility check.",
    )
    parser.add_argument(
        "--skip-quality-check",
        action="store_true",
        help="Skip necessary pre-solver quality-feasibility checks.",
    )
    args = parser.parse_args()

    try:
        scenario = load_scenario(Path(args.scenario), strict=True)
        parameters = preprocess_scenario(
            scenario,
            validate_capacity_feasibility=not args.skip_capacity_check,
            validate_quality_feasibility=not args.skip_quality_check,
        )
        print_parameter_summary(parameters)
    except (DataLoadError, PreprocessingError) as exc:
        raise SystemExit(f"\nAquaBlend preprocessing error:\n{exc}") from exc


if __name__ == "__main__":
    main()