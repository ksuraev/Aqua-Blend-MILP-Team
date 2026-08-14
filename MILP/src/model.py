"""Build the AquaBlend deterministic network MILP with Pyomo.

The model is assembled from the canonical :class:`ModelParameters` produced by
``src.preprocessing``.  Sets are defined first, followed by decision variables,
constraints, and the total-cost objective.  Constraint and solver sections are
left for their assigned contributors; this file establishes the shared model
components and their construction order.

The shared decision variables are:

``alpha[s]``
    Binary source-activation decision.

``a[s]``
    Total water withdrawn from source ``s`` in ML/day.

``beta[t]``
    Binary treatment-plant activation decision.

``gamma[s, t]`` and ``b[s, t]``
    Binary link activation and continuous flow from source ``s`` to plant ``t``.

``delta[t, z]`` and ``c[t, z]``
    Binary link activation and continuous flow from plant ``t`` to zone ``z``.

The objective minimises source and plant activation costs, source withdrawal
costs, and plant treatment costs.
"""

from __future__ import annotations

import pyomo.environ as pyo

from src.preprocessing import ModelParameters

SOLVER = "appsi_highs"


def define_sets(m: pyo.ConcreteModel, p: ModelParameters) -> None:
    """Define the source, plant, zone, quality, and network-arc index sets."""
    m.S = pyo.Set(initialize=p.source_ids)
    m.T = pyo.Set(initialize=p.plant_ids)
    m.Z = pyo.Set(initialize=p.zone_ids)
    m.P = pyo.Set(initialize=p.quality_parameter_ids)
    m.ST = pyo.Set(initialize=p.source_plant_arcs, dimen=2)
    m.TZ = pyo.Set(initialize=p.plant_zone_arcs, dimen=2)


def define_variables(m: pyo.ConcreteModel) -> None:
    """Define the shared activation, withdrawal, and network-flow variables.

    Parameters
    ----------
    m:
        Pyomo model containing the index sets created by :func:`define_sets`.
    """
    m.alpha = pyo.Var(m.S, domain=pyo.Binary)
    m.a = pyo.Var(m.S, domain=pyo.NonNegativeReals)
    m.beta = pyo.Var(m.T, domain=pyo.Binary)
    m.gamma = pyo.Var(m.ST, domain=pyo.Binary)
    m.b = pyo.Var(m.ST, domain=pyo.NonNegativeReals)
    m.delta = pyo.Var(m.TZ, domain=pyo.Binary)
    m.c = pyo.Var(m.TZ, domain=pyo.NonNegativeReals)


def set_constraints(m: pyo.ConcreteModel, p: ModelParameters) -> None:
    """Owner: shared? One block per constraint family.

    The pattern for each family is a rule function plus one Constraint declaration over the family's index set. Please see docs here https://pyomo.readthedocs.io/en/stable/explanation/modeling/math_programming/constraints.html.
    """
    raise NotImplementedError


def set_objective(m: pyo.ConcreteModel, p: ModelParameters) -> None:
    """Add the total operating-cost minimisation objective.

    Parameters
    ----------
    m:
        Pyomo model containing the decision variables created by
        :func:`define_variables`.
    p:
        Canonical preprocessing output containing source and plant costs.

    Notes
    -----
    Source unit cost is charged on total source withdrawal ``a``. Treatment
    cost is charged on plant inflow ``b``. Fixed activation costs are charged
    through ``alpha`` and ``beta``. The link-activation variables ``gamma`` and
    ``delta`` have no direct objective term because the current parameter
    contract contains no link fixed costs.
    """
    source_withdrawal_cost = pyo.quicksum(
        p.source_unit_cost[source_id] * m.a[source_id] for source_id in m.S
    )

    source_activation_cost = pyo.quicksum(
        p.source_fixed_cost[source_id] * m.alpha[source_id] for source_id in m.S
    )

    plant_activation_cost = pyo.quicksum(
        p.plant_fixed_cost[plant_id] * m.beta[plant_id] for plant_id in m.T
    )

    plant_treatment_cost = pyo.quicksum(
        p.plant_unit_treatment_cost[plant_id] * m.b[source_id, plant_id]
        for source_id, plant_id in m.ST
    )

    m.total_cost = pyo.Objective(
        expr=(
            source_withdrawal_cost
            + source_activation_cost
            + plant_activation_cost
            + plant_treatment_cost
        ),
        sense=pyo.minimize,
    )


def build_model(p: ModelParameters) -> pyo.ConcreteModel:
    """Assemble and return the complete, unsolved AquaBlend model."""
    if p is None:
        raise TypeError("ModelParameters cannot be None.")

    model = pyo.ConcreteModel(name="aquablend")
    define_sets(model, p)
    define_variables(model)
    set_constraints(model, p)
    set_objective(model, p)
    return model


def solve(p: ModelParameters, tee: bool = False) -> tuple[pyo.ConcreteModel, object]:
    """Owner: solver card. Need to consider what we are passing to postprocessing.py. 'tee' is a Pyomo option to print the solver log to stdout."""
    raise NotImplementedError