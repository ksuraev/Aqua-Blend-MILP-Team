"""
Model-construction logic for the AquaBlend Sprint 1 toy MILP.

This module validates the optimisation-ready input, creates the PuLP
model, creates the decision variables, adds the objective function,
and adds the model constraints.
"""

from typing import Tuple

import pulp
import math
from numbers import Real
from .constraints import add_constraints
from .input_schema import SolverInput
from .objective import add_objective
from .variables import ModelVariables, create_variables


def validate_solver_input(data: SolverInput) -> None:
    """
    Validate optimisation-ready data before constructing the MILP.

    Raw-file parsing and detailed schema validation should remain in
    data_loader.py and preprocessing.py. This function checks only
    conditions required to safely construct the mathematical model.

    Parameters
    ----------
    data:
        Clean, preprocessed AquaBlend solver input.

    Raises
    ------
    TypeError
        If data is not provided.

    ValueError
        If the processed input contains invalid or inconsistent values.
    """

    if data is None:
        raise TypeError("Solver input cannot be None.")

    if not data.reservoirs:
        raise ValueError(
            "At least one reservoir is required."
        )

    reservoir_names = [
        reservoir.name for reservoir in data.reservoirs
    ]

    if any(not name or not name.strip() for name in reservoir_names):
        raise ValueError(
            "Every reservoir must have a non-empty name."
        )

    if len(reservoir_names) != len(set(reservoir_names)):
        raise ValueError(
            "Reservoir names must be unique."
        )

    _validate_demand(data)
    _validate_plant(data)
    _validate_activation_limit(data)
    _validate_reservoirs(data)
    _validate_basic_feasibility(data)


def _validate_number(
    value: object,
    field_name: str,
    *,
    minimum: float | None = None,
) -> float:
    """Validate that a model input is a finite numeric value."""

    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{field_name} must be a numeric value.")

    numeric_value = float(value)

    if not math.isfinite(numeric_value):
        raise ValueError(f"{field_name} must be finite.")

    if minimum is not None and numeric_value < minimum:
        raise ValueError(
            f"{field_name} must be greater than or equal to {minimum}."
        )

    return numeric_value

def _validate_demand(data: SolverInput) -> None:
    """
    Validate the required demand.
    """

    required_volume = data.demand.required_volume

    if required_volume < 0:
        raise ValueError(
            "Required demand cannot be negative."
        )


def _validate_plant(data: SolverInput) -> None:
    """
    Validate the treatment-plant capacity.
    """

    if data.plant.capacity < 0:
        raise ValueError(
            "Treatment plant capacity cannot be negative."
        )


def _validate_activation_limit(data: SolverInput) -> None:
    """
    Validate the maximum number of active reservoirs.
    """

    maximum_active = data.maximum_active_reservoirs
    reservoir_count = len(data.reservoirs)

    if maximum_active < 0:
        raise ValueError(
            "Maximum active reservoirs cannot be negative."
        )

    if maximum_active > reservoir_count:
        raise ValueError(
            "Maximum active reservoirs cannot exceed the total "
            "number of reservoirs."
        )

    if (
        data.demand.required_volume > 0
        and maximum_active == 0
    ):
        raise ValueError(
            "At least one active reservoir must be allowed when "
            "demand is greater than zero."
        )


def _validate_reservoirs(data: SolverInput) -> None:
    """
    Validate capacities, minimum flows, costs, and quality values
    for every reservoir.
    """

    for reservoir in data.reservoirs:
        name = reservoir.name

        if reservoir.max_flow < 0:
            raise ValueError(
                f"{name}: maximum flow cannot be negative."
            )

        if reservoir.minimum_flow_if_active < 0:
            raise ValueError(
                f"{name}: minimum flow cannot be negative."
            )

        if (
            reservoir.minimum_flow_if_active
            > reservoir.max_flow
        ):
            raise ValueError(
                f"{name}: minimum active flow cannot exceed "
                "maximum flow."
            )

        if reservoir.variable_cost < 0:
            raise ValueError(
                f"{name}: variable cost cannot be negative."
            )

        if reservoir.fixed_activation_cost < 0:
            raise ValueError(
                f"{name}: fixed activation cost cannot be negative."
            )

        if not reservoir.quality:
            raise ValueError(
                f"{name}: water-quality data is required."
            )

        for quality_name, quality_value in reservoir.quality.items():
            if not quality_name:
                raise ValueError(
                    f"{name}: quality parameter name cannot be empty."
                )

            if quality_value is None:
                raise ValueError(
                    f"{name}: quality value '{quality_name}' "
                    "cannot be None."
                )


def _validate_basic_feasibility(data: SolverInput) -> None:
    """
    Perform simple capacity-based feasibility checks.

    These checks do not replace the optimiser. They only detect clear
    inconsistencies before the model is sent to HiGHS.
    """

    required_volume = data.demand.required_volume
    plant_capacity = data.plant.capacity

    if required_volume > plant_capacity:
        raise ValueError(
            "Required demand exceeds treatment plant capacity."
        )

    total_source_capacity = sum(
        reservoir.max_flow
        for reservoir in data.reservoirs
    )

    if required_volume > total_source_capacity:
        raise ValueError(
            "Required demand exceeds the combined reservoir capacity."
        )

    maximum_active = data.maximum_active_reservoirs

    capacities_in_descending_order = sorted(
        (
            reservoir.max_flow
            for reservoir in data.reservoirs
        ),
        reverse=True,
    )

    maximum_capacity_with_activation_limit = sum(
        capacities_in_descending_order[:maximum_active]
    )

    if (
        required_volume
        > maximum_capacity_with_activation_limit
    ):
        raise ValueError(
            "Demand cannot be met under the current maximum-active-"
            "reservoir limit."
        )


def build_model(
    data: SolverInput,
) -> Tuple[pulp.LpProblem, ModelVariables]:
    """
    Build the complete AquaBlend Sprint 1 PuLP model.

    Construction order:

    1. Validate solver input.
    2. Create a minimisation problem.
    3. Create decision variables.
    4. Add the total-cost objective.
    5. Add all mathematical constraints.
    6. Return the model and variables.

    Parameters
    ----------
    data:
        Clean, optimisation-ready data produced by preprocessing.py.

    Returns
    -------
    tuple
        The complete PuLP model and the decision-variable dictionary.
    """

    validate_solver_input(data)

    model = pulp.LpProblem(
        name="AquaBlend_Sprint_1_Toy_Model",
        sense=pulp.LpMinimize,
    )

    variables = create_variables(data)

    add_objective(
        model=model,
        data=data,
        variables=variables,
    )

    add_constraints(
        model=model,
        data=data,
        variables=variables,
    )

    return model, variables