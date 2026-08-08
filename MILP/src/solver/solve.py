"""HiGHS execution and status-safe result extraction for AquaBlend."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pulp

from .config import SolverConfig


@dataclass(frozen=True)
class SolverResult:
    """Structured result returned after a HiGHS solve attempt."""

    status: str
    status_code: int
    has_solution: bool
    objective_value: float | None
    source_flows: dict[str, float] = field(default_factory=dict)
    source_activation: dict[str, int] = field(default_factory=dict)


def solve_model(
    model: pulp.LpProblem,
    variables: Any,
    config: SolverConfig | None = None,
) -> SolverResult:
    """Solve ``model`` with HiGHS and return values only for a valid optimum."""

    if not isinstance(model, pulp.LpProblem):
        raise TypeError("model must be a pulp.LpProblem instance.")

    selected_config = config or SolverConfig()
    model.solve(selected_config.create_solver())

    status_code = int(model.status)
    status = pulp.LpStatus.get(status_code, "Unknown")
    if status != "Optimal":
        return SolverResult(
            status=status,
            status_code=status_code,
            has_solution=False,
            objective_value=None,
        )

    flow = _get_variable_group(variables, "flow")
    active = _get_variable_group(variables, "active")
    objective_value = pulp.value(model.objective)
    if objective_value is None or not math.isfinite(float(objective_value)):
        raise RuntimeError("HiGHS reported Optimal without a finite objective value.")

    return SolverResult(
        status=status,
        status_code=status_code,
        has_solution=True,
        objective_value=float(objective_value),
        source_flows={name: _variable_value(variable, name) for name, variable in flow.items()},
        source_activation={
            name: int(round(_variable_value(variable, name)))
            for name, variable in active.items()
        },
    )


def _get_variable_group(variables: Any, group_name: str) -> Mapping[str, pulp.LpVariable]:
    if isinstance(variables, Mapping):
        group = variables.get(group_name)
    else:
        group = getattr(variables, group_name, None)
    if group is None:
        raise KeyError(f"Missing decision-variable group: {group_name}.")
    if not isinstance(group, Mapping):
        raise TypeError(f"Decision-variable group '{group_name}' must be a mapping.")
    return group


def _variable_value(variable: pulp.LpVariable, name: str) -> float:
    if not isinstance(variable, pulp.LpVariable):
        raise TypeError(f"Variable {name!r} must be a PuLP variable.")
    value = pulp.value(variable)
    if value is None or not math.isfinite(float(value)):
        raise RuntimeError(f"No finite solution value is available for {name!r}.")
    return float(value)
