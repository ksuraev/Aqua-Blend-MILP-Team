from __future__ import annotations

import argparse
import math
from collections import deque
from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

try:
    # Package imports used when the module is imported through src.
    from .contracts import ScenarioData
    from .data_loader import DataLoadError, load_scenario
except ImportError:  # pragma: no cover - supports direct script execution.
    from contracts import ScenarioData
    from data_loader import DataLoadError, load_scenario


class PreprocessingError(RuntimeError):
    """Raised when validated input cannot be converted into MILP parameters."""


_EPSILON = 1e-9
_SUPPORTED_QUALITY_TRANSFORMS = {
    "identity",
    "ph_to_hydrogen_ion",
}

_MILP_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_SCENARIO_PATH = _MILP_ROOT / "config" / "scenarios" / "base_scenarios_v1.json"


@dataclass(frozen=True, slots=True)
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

    # Existing enabled network arcs over which the model may create flows.
    source_plant_arcs: tuple[tuple[str, str], ...]
    plant_zone_arcs: tuple[tuple[str, str], ...]

    # Formulation parameters D_z, F_s, F_t, C_s, C_t, W_s and V_t.
    demand_by_zone: dict[str, float]
    source_fixed_cost: dict[str, float]
    plant_fixed_cost: dict[str, float]
    source_unit_cost: dict[str, float]
    plant_unit_treatment_cost: dict[str, float]
    source_min_withdrawal: dict[str, float]
    source_max_withdrawal: dict[str, float]
    plant_min_throughput: dict[str, float]
    plant_max_throughput: dict[str, float]

    # Upper link capacities for source-to-plant and plant-to-zone arcs.
    source_plant_link_capacity: dict[tuple[str, str], float]
    plant_zone_link_capacity: dict[tuple[str, str], float]

    # Transformed water-quality values Q_sp and bounds for each model parameter p.
    source_quality: dict[tuple[str, str], float]
    quality_lower_bound: dict[str, float]
    quality_upper_bound: dict[str, float]
    quality_units: dict[str, str]

    # Informational notes that do not prevent model construction.
    warnings: tuple[str, ...]

    def as_formulation_dict(self) -> dict[str, Any]:
        """Expose parameters using stable mathematical-name equivalents."""
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
            "W_lower_s": self.source_min_withdrawal,
            "W_upper_s": self.source_max_withdrawal,
            "V_lower_t": self.plant_min_throughput,
            "V_upper_t": self.plant_max_throughput,
            # Keep the historical keys for existing callers and expose the
            # explicit upper-bound aliases used in the revised documentation.
            "L_st": self.source_plant_link_capacity,
            "L_tz": self.plant_zone_link_capacity,
            "L_upper_st": self.source_plant_link_capacity,
            "L_upper_tz": self.plant_zone_link_capacity,
            "Q_sp": self.source_quality,
            "Q_lower_p": self.quality_lower_bound,
            "Q_upper_p": self.quality_upper_bound,
        }


@dataclass(frozen=True, slots=True)
class _QualityRule:
    """Normalised quality definition used for source and bound transformation."""

    raw_name: str
    model_name: str
    raw_unit: str
    model_unit: str
    minimum: float
    maximum: float
    transform: str


@dataclass(slots=True)
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

            def send_flow(
                node: int,
                available: float,
                levels: list[int],
                next_edge: list[int],
            ) -> float:
                if node == sink_index:
                    return available

                while next_edge[node] < len(self._graph[node]):
                    edge_index = next_edge[node]
                    edge = self._graph[node][edge_index]

                    if edge.capacity > _EPSILON and levels[edge.to] == levels[node] + 1:
                        sent = send_flow(
                            edge.to,
                            min(available, edge.capacity),
                            levels,
                            next_edge,
                        )
                        if sent > _EPSILON:
                            edge.capacity -= sent
                            reverse = self._graph[edge.to][edge.reverse_index]
                            reverse.capacity += sent
                            return sent

                    next_edge[node] += 1

                return 0.0

            while True:
                sent = send_flow(
                    source_index,
                    math.inf,
                    levels,
                    next_edge,
                )
                if sent <= _EPSILON:
                    break
                total_flow += sent


def ph_to_hydrogen_ion(ph: float) -> float:
    """Convert pH into hydrogen-ion concentration in mol/L."""
    if not math.isfinite(ph) or not 0.0 <= ph <= 14.0:
        raise PreprocessingError("pH must be finite and between 0 and 14.")

    value = 10.0 ** (-ph)
    if not math.isfinite(value) or value <= 0:
        raise PreprocessingError(f"pH {ph!r} could not be transformed safely.")
    return value


def _require_finite(value: float | None, label: str) -> float:
    if value is None:
        raise PreprocessingError(f"{label} is missing after data loading.")
    if isinstance(value, bool):
        raise PreprocessingError(f"{label} must be numeric, not boolean.")

    try:
        converted = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PreprocessingError(f"{label} must be numeric.") from exc

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
    if any(not value.strip() for value in ordered):
        raise PreprocessingError(f"Blank {label} are not allowed.")
    if len(ordered) != len(set(ordered)):
        raise PreprocessingError(
            f"Duplicate {label} were detected during preprocessing."
        )
    return ordered


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PreprocessingError(f"{label} must be a mapping.")
    return value


def _normalise_quality_rules(
    quality_limits: dict[str, Any],
) -> tuple[_QualityRule, ...]:
    """Convert loader-validated quality limits into generic model-facing rules."""
    parameters = _require_mapping(
        quality_limits.get("parameters"),
        "quality_limits.parameters",
    )
    if not parameters:
        raise PreprocessingError("quality_limits.parameters must not be empty.")

    rules: list[_QualityRule] = []
    model_names: set[str] = set()

    for raw_parameter_id, raw_specification in parameters.items():
        raw_name = str(raw_parameter_id).strip()
        if not raw_name:
            raise PreprocessingError("Quality parameter identifiers must not be blank.")

        specification = _require_mapping(
            raw_specification,
            f"quality_limits.parameters.{raw_name}",
        )
        minimum = _require_finite(
            specification.get("min"),
            f"Quality parameter '{raw_name}' minimum",
        )
        maximum = _require_finite(
            specification.get("max"),
            f"Quality parameter '{raw_name}' maximum",
        )
        if minimum > maximum:
            raise PreprocessingError(
                f"Quality parameter '{raw_name}' minimum exceeds its maximum."
            )

        raw_unit = str(specification.get("unit", "")).strip()
        if not raw_unit:
            raise PreprocessingError(f"Quality parameter '{raw_name}' requires a unit.")

        transform = str(specification.get("transform", "identity")).strip()
        if transform not in _SUPPORTED_QUALITY_TRANSFORMS:
            supported = ", ".join(sorted(_SUPPORTED_QUALITY_TRANSFORMS))
            raise PreprocessingError(
                f"Unsupported quality transform '{transform}' for '{raw_name}'. "
                f"Supported transforms: {supported}."
            )

        default_model_name = (
            "hydrogen_ion_concentration_mol_l"
            if transform == "ph_to_hydrogen_ion"
            else raw_name
        )
        model_name = str(specification.get("model_name", default_model_name)).strip()
        if not model_name:
            raise PreprocessingError(
                f"Quality parameter '{raw_name}' requires a model_name."
            )
        if model_name in model_names:
            raise PreprocessingError(
                f"Quality model identifier '{model_name}' is used more than once."
            )
        model_names.add(model_name)

        default_model_unit = "mol/L" if transform == "ph_to_hydrogen_ion" else raw_unit
        model_unit = str(specification.get("model_unit", default_model_unit)).strip()
        if not model_unit:
            raise PreprocessingError(
                f"Quality parameter '{raw_name}' requires a model_unit."
            )

        rules.append(
            _QualityRule(
                raw_name=raw_name,
                model_name=model_name,
                raw_unit=raw_unit,
                model_unit=model_unit,
                minimum=minimum,
                maximum=maximum,
                transform=transform,
            )
        )

    return tuple(rules)


def _transform_quality_value(rule: _QualityRule, value: float, label: str) -> float:
    raw_value = _require_finite(value, label)

    if rule.transform == "identity":
        transformed = raw_value
    elif rule.transform == "ph_to_hydrogen_ion":
        transformed = ph_to_hydrogen_ion(raw_value)
    else:  # Defensive: rules are validated before this function is called.
        raise PreprocessingError(
            f"Unsupported quality transform '{rule.transform}' for '{rule.raw_name}'."
        )

    if not math.isfinite(transformed):
        raise PreprocessingError(
            f"Transformed quality value for {label} must be finite."
        )
    return transformed


def _transform_quality_bounds(
    rules: tuple[_QualityRule, ...],
) -> tuple[dict[str, float], dict[str, float], dict[str, str]]:
    lower: dict[str, float] = {}
    upper: dict[str, float] = {}
    units: dict[str, str] = {}

    for rule in rules:
        minimum_transformed = _transform_quality_value(
            rule,
            rule.minimum,
            f"Quality parameter '{rule.raw_name}' minimum",
        )
        maximum_transformed = _transform_quality_value(
            rule,
            rule.maximum,
            f"Quality parameter '{rule.raw_name}' maximum",
        )

        # Monotonically decreasing transforms such as pH -> [H+] reverse bounds.
        lower_value = min(minimum_transformed, maximum_transformed)
        upper_value = max(minimum_transformed, maximum_transformed)

        lower[rule.model_name] = lower_value
        upper[rule.model_name] = upper_value
        units[rule.model_name] = rule.model_unit

    return lower, upper, units


def _build_source_parameters(
    scenario: ScenarioData,
    quality_rules: tuple[_QualityRule, ...],
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    dict[str, float],
    dict[str, float],
    dict[str, float],
    dict[str, float],
    dict[tuple[str, str], float],
    list[str],
]:
    _ensure_unique((source.source_id for source in scenario.sources), "source IDs")

    excluded_source_ids = tuple(
        source.source_id
        for source in scenario.sources
        if not source.enabled or source.forced_inactive
    )
    active_sources = tuple(
        source
        for source in scenario.sources
        if source.enabled and not source.forced_inactive
    )
    source_ids = tuple(source.source_id for source in active_sources)

    if not source_ids:
        raise PreprocessingError(
            "The scenario must contain at least one usable source."
        )

    minimum_withdrawal: dict[str, float] = {}
    maximum_withdrawal: dict[str, float] = {}
    fixed_cost: dict[str, float] = {}
    unit_cost: dict[str, float] = {}
    quality: dict[tuple[str, str], float] = {}
    expected_quality_keys = {rule.raw_name for rule in quality_rules}
    warnings = [
        (
            f"Source '{source_id}' was excluded from S and its outgoing arcs "
            "because it is disabled or forced_inactive."
        )
        for source_id in excluded_source_ids
    ]

    for source in active_sources:
        minimum_value = _require_non_negative(
            source.minimum_withdrawal_ml_per_day,
            f"Source '{source.source_id}' minimum withdrawal W_lower_s",
        )
        maximum_value = _require_non_negative(
            source.maximum_withdrawal_ml_per_day,
            f"Source '{source.source_id}' maximum withdrawal W_upper_s",
        )
        if minimum_value > maximum_value:
            raise PreprocessingError(
                f"Source '{source.source_id}' minimum withdrawal exceeds its maximum."
            )

        minimum_withdrawal[source.source_id] = minimum_value
        maximum_withdrawal[source.source_id] = maximum_value
        fixed_cost[source.source_id] = _require_non_negative(
            source.fixed_activation_cost,
            f"Source '{source.source_id}' activation cost F_s",
        )
        unit_cost[source.source_id] = _require_non_negative(
            source.cost_per_ml,
            f"Source '{source.source_id}' unit cost C_s",
        )

        actual_quality_keys = set(source.quality)
        if actual_quality_keys != expected_quality_keys:
            missing = sorted(expected_quality_keys - actual_quality_keys)
            unexpected = sorted(actual_quality_keys - expected_quality_keys)
            details: list[str] = []
            if missing:
                details.append("missing=" + ", ".join(missing))
            if unexpected:
                details.append("unexpected=" + ", ".join(unexpected))
            raise PreprocessingError(
                f"Source '{source.source_id}' quality keys must exactly match "
                "quality_limits.parameters (" + "; ".join(details) + ")."
            )

        for rule in quality_rules:
            raw_value = source.quality[rule.raw_name]
            quality[(source.source_id, rule.model_name)] = _transform_quality_value(
                rule,
                raw_value,
                f"Source '{source.source_id}' quality '{rule.raw_name}'",
            )

        if not source.database_model_ready:
            warnings.append(
                f"Source '{source.source_id}' is not marked model_ready in the "
                "source data; required scenario overrides and field-level validation "
                "were still applied."
            )

    return (
        source_ids,
        excluded_source_ids,
        minimum_withdrawal,
        maximum_withdrawal,
        fixed_cost,
        unit_cost,
        quality,
        warnings,
    )


def _build_plant_parameters(
    scenario: ScenarioData,
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    dict[str, float],
    dict[str, float],
    dict[str, float],
    dict[str, float],
]:
    _ensure_unique((plant.plant_id for plant in scenario.plants), "plant IDs")

    disabled_plant_ids = tuple(
        plant.plant_id for plant in scenario.plants if not plant.enabled
    )
    active_plants = tuple(plant for plant in scenario.plants if plant.enabled)
    plant_ids = tuple(plant.plant_id for plant in active_plants)

    if not plant_ids:
        raise PreprocessingError(
            "The scenario must contain at least one enabled plant."
        )

    minimum_throughput: dict[str, float] = {}
    maximum_throughput: dict[str, float] = {}
    fixed_cost: dict[str, float] = {}
    treatment_cost: dict[str, float] = {}

    for plant in active_plants:
        minimum_flow = _require_non_negative(
            plant.minimum_processing_capacity_ml_per_day,
            f"Plant '{plant.plant_id}' minimum processing capacity V_lower_t",
        )
        maximum_flow = _require_non_negative(
            plant.maximum_processing_capacity_ml_per_day,
            f"Plant '{plant.plant_id}' maximum processing capacity V_upper_t",
        )
        if minimum_flow > maximum_flow:
            raise PreprocessingError(
                f"Plant '{plant.plant_id}' minimum flow exceeds its maximum throughput."
            )

        minimum_throughput[plant.plant_id] = minimum_flow
        maximum_throughput[plant.plant_id] = maximum_flow
        fixed_cost[plant.plant_id] = _require_non_negative(
            plant.fixed_activation_cost,
            f"Plant '{plant.plant_id}' activation cost F_t",
        )
        treatment_cost[plant.plant_id] = _require_non_negative(
            plant.treatment_cost_per_ml,
            f"Plant '{plant.plant_id}' treatment cost C_t",
        )

    return (
        plant_ids,
        disabled_plant_ids,
        minimum_throughput,
        maximum_throughput,
        fixed_cost,
        treatment_cost,
    )


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
        demand[zone.zone_id] = _require_non_negative(
            zone.demand_ml_per_day,
            f"Demand zone '{zone.zone_id}' demand D_z",
        )

    return zone_ids, demand


def _build_link_parameters(
    scenario: ScenarioData,
    source_ids: tuple[str, ...],
    excluded_source_ids: tuple[str, ...],
    plant_ids: tuple[str, ...],
    disabled_plant_ids: tuple[str, ...],
    zone_ids: tuple[str, ...],
) -> tuple[
    tuple[tuple[str, str], ...],
    tuple[tuple[str, str], ...],
    dict[tuple[str, str], float],
    dict[tuple[str, str], float],
]:
    source_set = set(source_ids)
    excluded_source_set = set(excluded_source_ids)
    plant_set = set(plant_ids)
    disabled_plant_set = set(disabled_plant_ids)
    zone_set = set(zone_ids)

    source_plant_capacity: dict[tuple[str, str], float] = {}
    for link in scenario.source_to_plant_links:
        if not link.enabled:
            continue

        key = (link.source_id, link.plant_id)
        if link.source_id in excluded_source_set or link.plant_id in disabled_plant_set:
            continue
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
            f"Source-to-plant capacity L_upper_st for "
            f"{link.source_id} -> {link.plant_id}",
        )

    plant_zone_capacity: dict[tuple[str, str], float] = {}
    for link in scenario.plant_to_zone_links:
        if not link.enabled:
            continue

        key = (link.plant_id, link.zone_id)
        if link.plant_id in disabled_plant_set:
            continue
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
            f"Plant-to-zone capacity L_upper_tz for {link.plant_id} -> {link.zone_id}",
        )

    if not source_plant_capacity:
        raise PreprocessingError("At least one usable source-to-plant arc is required.")
    if not plant_zone_capacity:
        raise PreprocessingError("At least one plant-to-zone arc is required.")

    return (
        tuple(source_plant_capacity),
        tuple(plant_zone_capacity),
        source_plant_capacity,
        plant_zone_capacity,
    )


def _validate_capacity_feasibility(parameters: ModelParameters) -> None:
    """Reject networks that cannot route all demand before quality is applied."""
    total_demand = sum(parameters.demand_by_zone.values())
    if total_demand <= _EPSILON:
        return

    flow = _Dinic()
    super_source = ("system", "source")
    super_sink = ("system", "sink")

    for source_id, capacity in parameters.source_max_withdrawal.items():
        flow.add_edge(super_source, ("source", source_id), capacity)

    for plant_id, capacity in parameters.plant_max_throughput.items():
        flow.add_edge(("plant_in", plant_id), ("plant_out", plant_id), capacity)

    for (
        source_id,
        plant_id,
    ), capacity in parameters.source_plant_link_capacity.items():
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
    # for each individual parameter. This is a safe necessary condition only.
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
    """Apply safe necessary checks without pretending to solve blend constraints."""
    usable_sources = [
        source_id
        for source_id, capacity in parameters.source_max_withdrawal.items()
        if capacity > _EPSILON
    ]
    if not usable_sources:
        if sum(parameters.demand_by_zone.values()) > _EPSILON:
            raise PreprocessingError("No usable source remains after exclusion rules.")
        return

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

    connected_sources = {source_id for source_id, _ in parameters.source_plant_arcs}
    for source_id in parameters.source_ids:
        if source_id not in connected_sources:
            warnings.append(
                f"Source '{source_id}' has no outgoing source-to-plant arc and "
                "cannot be used."
            )

    incoming_plants = {plant_id for _, plant_id in parameters.source_plant_arcs}
    outgoing_plants = {plant_id for plant_id, _ in parameters.plant_zone_arcs}
    for plant_id in parameters.plant_ids:
        if plant_id not in incoming_plants or plant_id not in outgoing_plants:
            warnings.append(
                f"Plant '{plant_id}' is disconnected on at least one side "
                "of the network."
            )

    incoming_zones = {zone_id for _, zone_id in parameters.plant_zone_arcs}
    for zone_id, demand in parameters.demand_by_zone.items():
        if demand > _EPSILON and zone_id not in incoming_zones:
            warnings.append(
                f"Demand zone '{zone_id}' has positive demand but no incoming arc."
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

    quality_rules = _normalise_quality_rules(scenario.quality_limits)
    quality_lower, quality_upper, quality_units = _transform_quality_bounds(
        quality_rules
    )

    (
        source_ids,
        excluded_source_ids,
        source_minimum,
        source_maximum,
        source_fixed,
        source_unit,
        source_quality,
        warnings,
    ) = _build_source_parameters(scenario, quality_rules)
    (
        plant_ids,
        disabled_plant_ids,
        plant_minimum,
        plant_maximum,
        plant_fixed,
        treatment_cost,
    ) = _build_plant_parameters(scenario)
    zone_ids, demand = _build_demand_parameters(scenario)

    (
        source_plant_arcs,
        plant_zone_arcs,
        source_plant_capacity,
        plant_zone_capacity,
    ) = _build_link_parameters(
        scenario,
        source_ids,
        excluded_source_ids,
        plant_ids,
        disabled_plant_ids,
        zone_ids,
    )

    parameters = ModelParameters(
        source_ids=source_ids,
        plant_ids=plant_ids,
        zone_ids=zone_ids,
        quality_parameter_ids=tuple(rule.model_name for rule in quality_rules),
        source_plant_arcs=source_plant_arcs,
        plant_zone_arcs=plant_zone_arcs,
        demand_by_zone=demand,
        source_fixed_cost=source_fixed,
        plant_fixed_cost=plant_fixed,
        source_unit_cost=source_unit,
        plant_unit_treatment_cost=treatment_cost,
        source_min_withdrawal=source_minimum,
        source_max_withdrawal=source_maximum,
        plant_min_throughput=plant_minimum,
        plant_max_throughput=plant_maximum,
        source_plant_link_capacity=source_plant_capacity,
        plant_zone_link_capacity=plant_zone_capacity,
        source_quality=source_quality,
        quality_lower_bound=quality_lower,
        quality_upper_bound=quality_upper,
        quality_units=quality_units,
        warnings=tuple(warnings),
    )

    if validate_capacity_feasibility:
        _validate_capacity_feasibility(parameters)
    if validate_quality_feasibility:
        _validate_quality_feasibility(parameters)

    return replace(parameters, warnings=_build_warnings(parameters))


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
        "Total source minimum withdrawal: "
        f"{sum(parameters.source_min_withdrawal.values()):g} ML/day"
    )
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
        description="Load and preprocess an AquaBlend scenario into MILP parameters."
    )
    parser.add_argument(
        "scenario",
        nargs="?",
        default=_DEFAULT_SCENARIO_PATH,
        help=(f"Path to the scenario JSON file. Defaults to {_DEFAULT_SCENARIO_PATH}."),
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
