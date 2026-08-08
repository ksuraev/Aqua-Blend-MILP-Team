"""
Objective-function definition for the AquaBlend Sprint 1 toy MILP.

The objective minimises:

1. Variable extraction costs.
2. Fixed reservoir activation costs.

Mathematically:

    minimise sum(
        variable_cost[source] * flow[source]
        + fixed_cost[source] * active[source]
    )
"""

from typing import Mapping

import pulp

from .input_schema import SolverInput
from .variables import ModelVariables


def add_objective(
    model: pulp.LpProblem,
    data: SolverInput,
    variables: ModelVariables,
) -> None:
    """
    Add the total-cost minimisation objective to the PuLP model.

    Parameters
    ----------
    model:
        PuLP model to which the objective will be added.

    data:
        Clean, optimisation-ready input containing reservoir costs.

    variables:
        Decision-variable dictionary created by create_variables().

        Expected structure:

        {
            "flow": {
                reservoir_name: pulp.LpVariable
            },
            "active": {
                reservoir_name: pulp.LpVariable
            }
        }

    Raises
    ------
    KeyError
        If required variable groups or reservoir variables are missing.

    ValueError
        If any cost value is negative.
    """

    _validate_variable_groups(variables)

    flow = variables["flow"]
    active = variables["active"]

    objective_terms = []

    for reservoir in data.reservoirs:
        name = reservoir.name

        if name not in flow:
            raise KeyError(
                f"Missing flow variable for reservoir '{name}'."
            )

        if name not in active:
            raise KeyError(
                f"Missing activation variable for reservoir '{name}'."
            )

        variable_cost = reservoir.variable_cost
        fixed_cost = reservoir.fixed_activation_cost

        if variable_cost < 0:
            raise ValueError(
                f"{name}: variable cost cannot be negative."
            )

        if fixed_cost < 0:
            raise ValueError(
                f"{name}: fixed activation cost cannot be negative."
            )

        variable_cost_term = variable_cost * flow[name]
        fixed_cost_term = fixed_cost * active[name]

        objective_terms.append(
            variable_cost_term + fixed_cost_term
        )

    model += (
        pulp.lpSum(objective_terms),
        "Minimise_Total_Cost",
    )


def calculate_variable_cost_expression(
    data: SolverInput,
    variables: ModelVariables,
) -> pulp.LpAffineExpression:
    """
    Return only the variable extraction-cost component.

    This helper is useful for debugging, reporting, and unit tests.
    """

    _validate_variable_groups(variables)

    flow = variables["flow"]

    return pulp.lpSum(
        reservoir.variable_cost * flow[reservoir.name]
        for reservoir in data.reservoirs
    )


def calculate_fixed_cost_expression(
    data: SolverInput,
    variables: ModelVariables,
) -> pulp.LpAffineExpression:
    """
    Return only the fixed activation-cost component.

    This helper is useful for debugging, reporting, and unit tests.
    """

    _validate_variable_groups(variables)

    active = variables["active"]

    return pulp.lpSum(
        reservoir.fixed_activation_cost
        * active[reservoir.name]
        for reservoir in data.reservoirs
    )


def _validate_variable_groups(
    variables: Mapping[str, Mapping[str, pulp.LpVariable]],
) -> None:
    """
    Validate that the required decision-variable groups exist.
    """

    required_groups = {"flow", "active"}
    missing_groups = required_groups.difference(variables.keys())

    if missing_groups:
        missing_text = ", ".join(sorted(missing_groups))

        raise KeyError(
            f"Missing decision-variable groups: {missing_text}."
        )