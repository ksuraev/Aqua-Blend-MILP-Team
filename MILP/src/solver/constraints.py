"""Demand, source-activation, and source-capacity constraints.

This module implements the source-side constraints in the AquaBlend Sprint 1
toy MILP. It accepts either the formulation-ready ``ModelParameters`` object
created by ``preprocessing.py`` or the simplified ``SolverInput`` interface
used by the solver modules.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from numbers import Real
from typing import Any

import pulp


VariableGroup = Mapping[str, pulp.LpVariable]


def add_constraints(
    model: pulp.LpProblem,
    data: Any,
    variables: Any,
) -> None:
    """Add demand and source withdrawal/activation constraints to ``model``.

    The variables must contain ``flow`` and ``active`` groups, either as
    mapping entries or object attributes. Existing variables are reused; this
    function never creates a second set of decision variables.
    """

    if not isinstance(model, pulp.LpProblem):
        raise TypeError("model must be a pulp.LpProblem instance.")

    source_names, demand, minimums, maximums = _normalise_input(data)
    flow = _get_variable_group(variables, "flow")
    active = _get_variable_group(variables, "active")

    _validate_variable_coverage(flow, source_names, "flow")
    _validate_variable_coverage(active, source_names, "active")

    model += (
        pulp.lpSum(flow[source] for source in source_names) >= demand,
        "Demand_Satisfaction",
    )

    for source in source_names:
        safe_source = _safe_name(source)
        model += (
            flow[source] >= minimums[source] * active[source],
            f"Source_Min_Withdrawal_{safe_source}",
        )
        model += (
            flow[source] <= maximums[source] * active[source],
            f"Source_Max_Withdrawal_{safe_source}",
        )


def _normalise_input(
    data: Any,
) -> tuple[list[str], float, dict[str, float], dict[str, float]]:
    """Convert either supported processed-input shape into common values."""

    if hasattr(data, "source_ids") and hasattr(data, "demand_by_zone"):
        return _from_model_parameters(data)

    if hasattr(data, "reservoirs") and hasattr(data, "demand"):
        return _from_solver_input(data)

    raise TypeError(
        "Unsupported solver input. Expected ModelParameters or SolverInput."
    )


def _from_model_parameters(
    data: Any,
) -> tuple[list[str], float, dict[str, float], dict[str, float]]:
    """Read Archit's formulation-ready ``ModelParameters`` interface."""

    source_names = _validate_source_names(data.source_ids)
    demand_by_zone = _validate_numeric_mapping(
        data.demand_by_zone,
        "demand_by_zone",
    )
    maximums = _validate_source_mapping(
        data.source_max_withdrawal,
        source_names,
        "source_max_withdrawal",
    )

    # The current preprocessing branch does not yet expose W_s lower bounds.
    # Zero is the formulation's documented default. Once the field is added,
    # it is consumed automatically without another constraints change.
    raw_minimums = getattr(data, "source_min_withdrawal", None)
    minimums = (
        {source: 0.0 for source in source_names}
        if raw_minimums is None
        else _validate_source_mapping(
            raw_minimums,
            source_names,
            "source_min_withdrawal",
        )
    )

    _validate_bounds(source_names, minimums, maximums)
    return source_names, sum(demand_by_zone.values()), minimums, maximums


def _from_solver_input(
    data: Any,
) -> tuple[list[str], float, dict[str, float], dict[str, float]]:
    """Read Meet's simplified reservoir-based solver input interface."""

    reservoirs: Sequence[Any] = data.reservoirs
    if not reservoirs:
        raise ValueError("At least one source is required.")

    source_names = _validate_source_names(
        [reservoir.name for reservoir in reservoirs]
    )
    demand = _nonnegative_number(
        data.demand.required_volume,
        "demand.required_volume",
    )
    minimums = {
        reservoir.name: _nonnegative_number(
            reservoir.minimum_flow_if_active,
            f"{reservoir.name}.minimum_flow_if_active",
        )
        for reservoir in reservoirs
    }
    maximums = {
        reservoir.name: _nonnegative_number(
            reservoir.max_flow,
            f"{reservoir.name}.max_flow",
        )
        for reservoir in reservoirs
    }

    _validate_bounds(source_names, minimums, maximums)
    return source_names, demand, minimums, maximums


def _get_variable_group(variables: Any, group_name: str) -> VariableGroup:
    if isinstance(variables, Mapping):
        group = variables.get(group_name)
    else:
        group = getattr(variables, group_name, None)

    if group is None:
        raise KeyError(f"Missing decision-variable group: {group_name}.")
    if not isinstance(group, Mapping):
        raise TypeError(f"Decision-variable group '{group_name}' must be a mapping.")
    return group


def _validate_source_names(names: Sequence[str]) -> list[str]:
    source_names = list(names)
    if not source_names:
        raise ValueError("At least one source is required.")
    if any(not isinstance(name, str) or not name.strip() for name in source_names):
        raise ValueError("Every source must have a non-empty string identifier.")
    if len(source_names) != len(set(source_names)):
        raise ValueError("Source identifiers must be unique.")
    if len({_safe_name(name) for name in source_names}) != len(source_names):
        raise ValueError("Source identifiers produce duplicate constraint names.")
    return source_names


def _validate_numeric_mapping(
    values: Mapping[str, Any],
    field_name: str,
) -> dict[str, float]:
    if not isinstance(values, Mapping):
        raise TypeError(f"{field_name} must be a mapping.")
    return {
        key: _nonnegative_number(value, f"{field_name}[{key!r}]")
        for key, value in values.items()
    }


def _validate_source_mapping(
    values: Mapping[str, Any],
    source_names: Sequence[str],
    field_name: str,
) -> dict[str, float]:
    validated = _validate_numeric_mapping(values, field_name)
    missing = sorted(set(source_names).difference(validated))
    if missing:
        raise KeyError(f"{field_name} is missing: {', '.join(missing)}.")
    return {source: validated[source] for source in source_names}


def _validate_bounds(
    source_names: Sequence[str],
    minimums: Mapping[str, float],
    maximums: Mapping[str, float],
) -> None:
    for source in source_names:
        if minimums[source] > maximums[source]:
            raise ValueError(
                f"{source}: minimum withdrawal cannot exceed maximum withdrawal."
            )


def _validate_variable_coverage(
    variables: VariableGroup,
    source_names: Sequence[str],
    group_name: str,
) -> None:
    missing = sorted(set(source_names).difference(variables))
    if missing:
        raise KeyError(f"Missing {group_name} variables for: {', '.join(missing)}.")
    for source in source_names:
        if not isinstance(variables[source], pulp.LpVariable):
            raise TypeError(f"{group_name}[{source!r}] must be a PuLP variable.")


def _nonnegative_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{field_name} must be numeric.")
    numeric_value = float(value)
    if not math.isfinite(numeric_value):
        raise ValueError(f"{field_name} must be finite.")
    if numeric_value < 0:
        raise ValueError(f"{field_name} cannot be negative.")
    return numeric_value


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", name.strip()).strip("_")
