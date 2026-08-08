"""
Decision-variable definitions for the AquaBlend Sprint 1 toy MILP.

The model contains two decision-variable groups:

1. flow[source]
   Continuous volume drawn from each reservoir in ML/day.

2. active[source]
   Binary variable equal to 1 when a reservoir is selected
   and 0 when it is not selected.
"""

from typing import Dict
import re
import pulp

from .input_schema import SolverInput



def create_variables(data: SolverInput) -> ModelVariables:
    flow: dict[str, pulp.LpVariable] = {}
    active: dict[str, pulp.LpVariable] = {}

    original_names: set[str] = set()
    safe_names: set[str] = set()

    for reservoir in data.reservoirs:
        name = reservoir.name.strip()

        if not name:
            raise ValueError("Reservoir name cannot be empty.")

        if name in original_names:
            raise ValueError(f"Duplicate reservoir name: {name}")

        safe_name = _make_safe_name(name)

        if safe_name in safe_names:
            raise ValueError(
                "Reservoir names produce duplicate PuLP variable names. "
                f"Conflict found for '{name}'."
            )

        original_names.add(name)
        safe_names.add(safe_name)

        flow[name] = pulp.LpVariable(
            name=f"flow_{safe_name}",
            lowBound=0,
            cat=pulp.LpContinuous,
        )

        active[name] = pulp.LpVariable(
            name=f"active_{safe_name}",
            cat=pulp.LpBinary,
        )

    return ModelVariables(
        flow=flow,
        active=active,
    )

def _make_safe_variable_name(name: str) -> str:
    """
    Convert a reservoir name into a safe PuLP variable-name component.

    Spaces and unsupported characters are replaced with underscores.

    Examples
    --------
    "Reservoir A" becomes "Reservoir_A"
    "Source-1" becomes "Source_1"
    """

    if not isinstance(name, str):
        raise TypeError("Reservoir name must be a string.")

    cleaned_name = name.strip()

    if not cleaned_name:
        raise ValueError("Reservoir name cannot be empty.")

    return "".join(
        character if character.isalnum() else "_"
        for character in cleaned_name
    )