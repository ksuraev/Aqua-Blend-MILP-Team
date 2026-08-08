"""Demand, source-activation, and source-capacity constraints.

The constraints in this module use the formulation-ready ModelParameters
object produced by preprocessing.py. Decision variables are created by the
model-building layer and passed into add_constraints().
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Protocol

import pulp

from ..preprocessing import ModelParameters


Arc = tuple[str, str]
ArcVariableGroup = Mapping[Arc, pulp.LpVariable]
SourceVariableGroup = Mapping[str, pulp.LpVariable]


class ModelVariables(Protocol):
    """Decision-variable groups required by these constraints."""

    source_to_plant_flow: ArcVariableGroup
    plant_to_zone_flow: ArcVariableGroup
    source_active: SourceVariableGroup


def add_constraints(
    model: pulp.LpProblem,
    parameters: ModelParameters,
    variables: ModelVariables,
) -> None:
    """Add demand, source-activation and source-capacity constraints.

    Demand is enforced separately for every demand zone. Source withdrawal
    is calculated as the sum of all outgoing source-to-plant flows. The
    binary source-activation variable links each source to its optional
    minimum withdrawal and maximum capacity.

    This function reuses decision variables created by the model-building
    layer and does not create duplicate variables.

    Args:
        model: PuLP optimisation model receiving the constraints.
        parameters: Validated parameters produced by preprocessing.
        variables: Existing source-to-plant flow, plant-to-zone flow and
            source-activation variables.

    Raises:
        TypeError: If model or variable groups have an invalid type.
        KeyError: If a required decision variable is missing.
    """
    if not isinstance(model, pulp.LpProblem):
        raise TypeError("model must be a pulp.LpProblem instance.")

    source_to_plant_flow = _get_variable_group(
        variables,
        "source_to_plant_flow",
    )
    plant_to_zone_flow = _get_variable_group(
        variables,
        "plant_to_zone_flow",
    )
    source_active = _get_variable_group(
        variables,
        "source_active",
    )

    _validate_arc_variable_coverage(
        source_to_plant_flow,
        parameters.source_plant_arcs,
        "source_to_plant_flow",
    )
    _validate_arc_variable_coverage(
        plant_to_zone_flow,
        parameters.plant_zone_arcs,
        "plant_to_zone_flow",
    )
    _validate_source_variable_coverage(
        source_active,
        parameters.source_ids,
        "source_active",
    )

    _add_zone_demand_constraints(
        model,
        parameters,
        plant_to_zone_flow,
    )
    _add_source_activation_constraints(
        model,
        parameters,
        source_to_plant_flow,
        source_active,
    )


def _add_zone_demand_constraints(
    model: pulp.LpProblem,
    parameters: ModelParameters,
    plant_to_zone_flow: ArcVariableGroup,
) -> None:
    """Add one demand-satisfaction constraint for each demand zone."""
    for zone_index, zone_id in enumerate(parameters.zone_ids):
        incoming_arcs = tuple(
            arc for arc in parameters.plant_zone_arcs if arc[1] == zone_id
        )

        model += (
            pulp.lpSum(plant_to_zone_flow[arc] for arc in incoming_arcs)
            >= parameters.demand_by_zone[zone_id],
            f"Demand_Satisfaction_{zone_index}_{_safe_name(zone_id)}",
        )


def _add_source_activation_constraints(
    model: pulp.LpProblem,
    parameters: ModelParameters,
    source_to_plant_flow: ArcVariableGroup,
    source_active: SourceVariableGroup,
) -> None:
    """Link total source withdrawal to source activation and capacity."""
    for source_index, source_id in enumerate(parameters.source_ids):
        outgoing_arcs = tuple(
            arc for arc in parameters.source_plant_arcs if arc[0] == source_id
        )
        total_withdrawal = pulp.lpSum(
            source_to_plant_flow[arc] for arc in outgoing_arcs
        )
        safe_source = _safe_name(source_id)

        model += (
            total_withdrawal
            >= parameters.source_min_withdrawal[source_id]
            * source_active[source_id],
            f"Source_Min_Withdrawal_{source_index}_{safe_source}",
        )

        model += (
            total_withdrawal
            <= parameters.source_max_withdrawal[source_id]
            * source_active[source_id],
            f"Source_Max_Withdrawal_{source_index}_{safe_source}",
        )


def _get_variable_group(
    variables: ModelVariables,
    group_name: str,
) -> Mapping[object, pulp.LpVariable]:
    """Return a required variable group from the model variable container."""
    group = getattr(variables, group_name, None)

    if group is None:
        raise KeyError(f"Missing decision-variable group: {group_name}.")

    if not isinstance(group, Mapping):
        raise TypeError(
            f"Decision-variable group '{group_name}' must be a mapping."
        )

    return group


def _validate_arc_variable_coverage(
    variables: Mapping[object, pulp.LpVariable],
    required_arcs: tuple[Arc, ...],
    group_name: str,
) -> None:
    """Check that every enabled network arc has a PuLP variable."""
    missing = [arc for arc in required_arcs if arc not in variables]

    if missing:
        raise KeyError(
            f"Missing {group_name} variables for arcs: "
            + ", ".join(str(arc) for arc in missing)
        )

    for arc in required_arcs:
        if not isinstance(variables[arc], pulp.LpVariable):
            raise TypeError(
                f"{group_name}[{arc!r}] must be a PuLP variable."
            )


def _validate_source_variable_coverage(
    variables: Mapping[object, pulp.LpVariable],
    source_ids: tuple[str, ...],
    group_name: str,
) -> None:
    """Check that every source has a binary activation variable."""
    missing = [source_id for source_id in source_ids if source_id not in variables]

    if missing:
        raise KeyError(
            f"Missing {group_name} variables for sources: "
            + ", ".join(missing)
        )

    for source_id in source_ids:
        if not isinstance(variables[source_id], pulp.LpVariable):
            raise TypeError(
                f"{group_name}[{source_id!r}] must be a PuLP variable."
            )


def _safe_name(name: str) -> str:
    """Convert an identifier into a PuLP-safe constraint-name component."""
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", name.strip())
    return safe.strip("_") or "unnamed"