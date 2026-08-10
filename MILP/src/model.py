"""
Assembly order for the Pyomo model. Functions are left as stubs to be filled in by the owner of each part of the formulation.

All docstrings including this one must be replaced with a description of the model and its formulation. The docstring for each function must be replaced with a description of the function's purpose and its arguments. Current docstrings are placeholders to be filled in by the owner of each part of the formulation.

This file exists so the variable, constraint and objective work can be written against the same names instead of each guessing. It declares the sets and the order of assembly, but leaves the details to be filled in by the owner of each part of the formulation.

Pyomo notes:

  - Variables are attributes of the model. There is no dict of variables to pass
    around: m.b[s, t] is reachable from anywhere that has m. https://pyomo.readthedocs.io/en/stable/explanation/modeling/math_programming/variables.html
  - A Var is declared over Pyomo Sets, so the sets have to exist first.
  - Constraints are declared as a family over an index set with a rule function
    returning one expression per index. https://pyomo.readthedocs.io/en/stable/explanation/modeling/math_programming/constraints.html
  - Names are taken from the declaration, so m.Demand is automatically named
    "Demand" in the solver log. No need for safe name functions or strings.

Parameter values are read straight out of ModelParameters rather than copied into pyo.Param objects. Pyomo does not require Params, and duplicating the data would give us two sources of truth for the same numbers.
"""

from __future__ import annotations

import pyomo.environ as pyo
from src.contracts import ModelParameters

SOLVER = "appsi_highs"


def define_sets(m: pyo.ConcreteModel, p: ModelParameters) -> None:
    """Index sets. Everything else is declared over these."""
    m.S = pyo.Set(initialize=p.source_ids)
    m.T = pyo.Set(initialize=p.plant_ids)
    m.Z = pyo.Set(initialize=p.zone_ids)
    m.P = pyo.Set(initialize=p.quality_parameter_ids)
    m.ST = pyo.Set(initialize=p.source_plant_arcs, dimen=2)
    m.TZ = pyo.Set(initialize=p.plant_zone_arcs, dimen=2)


def define_variables(m: pyo.ConcreteModel) -> None:
    """Owner: variables card.

    Constraint and objective code refers to these attributes, so they need to be established first. Must map directly to the formulation document so it is clear what each variable is.

    For example,
        m.alpha = pyo.Var(m.S, domain=pyo.Binary)          # source activated
        m.a     = pyo.Var(m.S, domain=pyo.NonNegativeReals)   # volume drawn
    """
    raise NotImplementedError


def set_constraints(m: pyo.ConcreteModel, p: ModelParameters) -> None:
    """Owner: shared? One block per constraint family.

    The pattern for each family is a rule function plus one Constraint declaration over the family's index set. Please see docs here https://pyomo.readthedocs.io/en/stable/explanation/modeling/math_programming/constraints.html.
    """
    raise NotImplementedError


def set_objective(m: pyo.ConcreteModel, p: ModelParameters) -> None:
    """Owner: objective card."""
    raise NotImplementedError


def build_model(p: ModelParameters) -> pyo.ConcreteModel:
    """Assemble the model. This is the only place the order is decided."""
    model = pyo.ConcreteModel(name="aquablend")
    define_sets(model, p)
    define_variables(model)
    set_constraints(model, p)
    set_objective(model, p)
    return model


def solve(p: ModelParameters, tee: bool = False) -> tuple[pyo.ConcreteModel, object]:
    """Owner: solver card. Need to consider what we are passing to postprocessing.py. 'tee' is a Pyomo option to print the solver log to stdout."""
    raise NotImplementedError
