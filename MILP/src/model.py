"""Build the AquaBlend deterministic network MILP with Pyomo.

The model is assembled from the canonical :class:`ModelParameters` produced by
``src.preprocessing``.  Sets are defined first, followed by decision variables,
constraints, and the total-cost objective. The solver integration is maintained
in the stacked solver contribution; this file establishes the shared model
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
    """Add demand, capacity, flow-balance, link and quality constraints."""

    incoming_to_plant = {plant_id: [] for plant_id in p.plant_ids}
    outgoing_from_source = {source_id: [] for source_id in p.source_ids}
    outgoing_from_plant = {plant_id: [] for plant_id in p.plant_ids}
    incoming_to_zone = {zone_id: [] for zone_id in p.zone_ids}

    for source_id, plant_id in p.source_plant_arcs:
        arc = (source_id, plant_id)
        outgoing_from_source[source_id].append(arc)
        incoming_to_plant[plant_id].append(arc)

    for plant_id, zone_id in p.plant_zone_arcs:
        arc = (plant_id, zone_id)
        outgoing_from_plant[plant_id].append(arc)
        incoming_to_zone[zone_id].append(arc)

    def demand_satisfaction_rule(
        model: pyo.ConcreteModel,
        zone_id: str,
    ):
        incoming_arcs = incoming_to_zone[zone_id]

        if not incoming_arcs:
            if p.demand_by_zone[zone_id] == 0:
                return pyo.Constraint.Skip
            return pyo.Constraint.Infeasible

        incoming_flow = pyo.quicksum(model.c[arc] for arc in incoming_arcs)

        return incoming_flow >= p.demand_by_zone[zone_id]

    m.demand_satisfaction = pyo.Constraint(
        m.Z,
        rule=demand_satisfaction_rule,
    )

    def source_min_withdrawal_rule(
        model: pyo.ConcreteModel,
        source_id: str,
    ):
        return (
            model.a[source_id]
            >= p.source_min_withdrawal[source_id] * model.alpha[source_id]
        )

    m.source_min_withdrawal = pyo.Constraint(
        m.S,
        rule=source_min_withdrawal_rule,
    )

    def source_max_withdrawal_rule(
        model: pyo.ConcreteModel,
        source_id: str,
    ):
        return (
            model.a[source_id]
            <= p.source_max_withdrawal[source_id] * model.alpha[source_id]
        )

    m.source_max_withdrawal = pyo.Constraint(
        m.S,
        rule=source_max_withdrawal_rule,
    )

    def source_flow_balance_rule(
        model: pyo.ConcreteModel,
        source_id: str,
    ):
        outgoing_arcs = outgoing_from_source[source_id]

        outgoing_flow = pyo.quicksum(model.b[arc] for arc in outgoing_arcs)

        return model.a[source_id] == outgoing_flow

    m.source_flow_balance = pyo.Constraint(
        m.S,
        rule=source_flow_balance_rule,
    )

    def plant_min_throughput_rule(
        model: pyo.ConcreteModel,
        plant_id: str,
    ):
        incoming_arcs = incoming_to_plant[plant_id]

        inflow = pyo.quicksum(model.b[arc] for arc in incoming_arcs)

        return inflow >= p.plant_min_throughput[plant_id] * model.beta[plant_id]

    m.plant_min_throughput = pyo.Constraint(
        m.T,
        rule=plant_min_throughput_rule,
    )

    def plant_max_throughput_rule(
        model: pyo.ConcreteModel,
        plant_id: str,
    ):
        incoming_arcs = incoming_to_plant[plant_id]

        inflow = pyo.quicksum(model.b[arc] for arc in incoming_arcs)

        return inflow <= p.plant_max_throughput[plant_id] * model.beta[plant_id]

    m.plant_max_throughput = pyo.Constraint(
        m.T,
        rule=plant_max_throughput_rule,
    )

    def plant_flow_balance_rule(
        model: pyo.ConcreteModel,
        plant_id: str,
    ):
        incoming_arcs = incoming_to_plant[plant_id]

        outgoing_arcs = outgoing_from_plant[plant_id]

        if not incoming_arcs and not outgoing_arcs:
            return pyo.Constraint.Skip

        inflow = pyo.quicksum(model.b[arc] for arc in incoming_arcs)

        outflow = pyo.quicksum(model.c[arc] for arc in outgoing_arcs)

        return inflow == outflow

    m.plant_flow_balance = pyo.Constraint(
        m.T,
        rule=plant_flow_balance_rule,
    )

    def source_plant_link_capacity_rule(
        model: pyo.ConcreteModel,
        source_id: str,
        plant_id: str,
    ):
        return (
            model.b[source_id, plant_id]
            <= p.source_plant_link_capacity[source_id, plant_id]
            * model.gamma[source_id, plant_id]
        )

    m.source_plant_link_capacity = pyo.Constraint(
        m.ST,
        rule=source_plant_link_capacity_rule,
    )

    def plant_zone_link_capacity_rule(
        model: pyo.ConcreteModel,
        plant_id: str,
        zone_id: str,
    ):
        return (
            model.c[plant_id, zone_id]
            <= p.plant_zone_link_capacity[plant_id, zone_id]
            * model.delta[plant_id, zone_id]
        )

    m.plant_zone_link_capacity = pyo.Constraint(
        m.TZ,
        rule=plant_zone_link_capacity_rule,
    )

    def source_link_activation_rule(
        model: pyo.ConcreteModel,
        source_id: str,
        plant_id: str,
    ):
        return model.gamma[source_id, plant_id] <= model.alpha[source_id]

    m.source_link_activation = pyo.Constraint(
        m.ST,
        rule=source_link_activation_rule,
    )

    def plant_link_activation_rule(
        model: pyo.ConcreteModel,
        plant_id: str,
        zone_id: str,
    ):
        return model.delta[plant_id, zone_id] <= model.beta[plant_id]

    m.plant_link_activation = pyo.Constraint(
        m.TZ,
        rule=plant_link_activation_rule,
    )

    def quality_lower_bound_rule(
        model: pyo.ConcreteModel,
        plant_id: str,
        parameter_id: str,
    ):
        incoming_arcs = incoming_to_plant[plant_id]

        if not incoming_arcs:
            return pyo.Constraint.Skip

        inflow = pyo.quicksum(model.b[arc] for arc in incoming_arcs)

        quality_load = pyo.quicksum(
            p.source_quality[source_id, parameter_id] * model.b[source_id, plant_id]
            for source_id, _ in incoming_arcs
        )

        return quality_load >= p.quality_lower_bound[parameter_id] * inflow

    m.quality_lower_bound = pyo.Constraint(
        m.T,
        m.P,
        rule=quality_lower_bound_rule,
    )

    def quality_upper_bound_rule(
        model: pyo.ConcreteModel,
        plant_id: str,
        parameter_id: str,
    ):
        incoming_arcs = incoming_to_plant[plant_id]

        if not incoming_arcs:
            return pyo.Constraint.Skip

        inflow = pyo.quicksum(model.b[arc] for arc in incoming_arcs)

        quality_load = pyo.quicksum(
            p.source_quality[source_id, parameter_id] * model.b[source_id, plant_id]
            for source_id, _ in incoming_arcs
        )

        return quality_load <= p.quality_upper_bound[parameter_id] * inflow

    m.quality_upper_bound = pyo.Constraint(
        m.T,
        m.P,
        rule=quality_upper_bound_rule,
    )


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
