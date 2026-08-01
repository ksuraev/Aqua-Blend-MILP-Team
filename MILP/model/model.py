"""AquaBlend toy MILP model builder.

Implements the toy model exactly as specified in the formulation document:
- Section 4: Decision Variables
- Section 5: Objective Function
- Section 7: Constraints (full set, validated final form)

This module builds and solves the toy model with PuLP + HiGHS. It exists to
validate the MILP formulation (linearised pH handling, activation linking,
dosing gating, batch counting) on a small, hand-inspectable instance before
the same patterns are scaled up to the full source/plant/zone network model
in model_builder.py.

Dosing is cost-only in this toy model (see formulation Section 6): it does
not alter blended pH, alkalinity, or turbidity in the constraints.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pulp


class ModelBuildError(RuntimeError):
    """Raised when the toy model cannot be constructed from the given inputs."""


class ModelSolveError(RuntimeError):
    """Raised when no solver capable of solving the model is available."""


# ---------------------------------------------------------------------------
# Input data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToyModelInputs:
    """All numeric inputs for the toy model (Sections 4, 5, 7)."""

    reservoir_ids: tuple[str, ...]
    chemical_ids: tuple[str, ...]

    # Per-reservoir data, keyed by reservoir_id.
    capacity: dict[str, float]              # Capacity_i (ML)
    source_cost: dict[str, float]           # Cost_i ($/ML)
    hydrogen_ion: dict[str, float]          # [H+]_i (mol/L), pre-converted from pH
    alkalinity: dict[str, float]            # Alkalinity_i (mg/L CaCO3)
    turbidity: dict[str, float]             # Turbidity_i (NTU)

    # Per-chemical data, keyed by chemical_id.
    chemical_cost: dict[str, float]         # Cost_c ($/kg)

    demand: float                           # Demand (ML)

    hydrogen_ion_min: float                 # [H+]_min_allowed
    hydrogen_ion_max: float                 # [H+]_max_allowed
    alkalinity_min: float                   # Alk_min
    alkalinity_max: float                   # Alk_max
    turbidity_min: float                    # Turb_min
    turbidity_max: float                    # Turb_max

    big_m_dosing: float                     # BigM_dosing (kg)
    batch_capacity: float                   # BatchCapacity (ML per batch)
    energy_rate: float                      # EnergyRate ($/ML)

    def validate(self) -> None:
        """Defensive checks so a malformed instance fails before model build."""
        issues: list[str] = []

        if not self.reservoir_ids:
            issues.append("At least one reservoir is required.")
        if not self.chemical_ids:
            issues.append("At least one chemical is required.")

        for group_name, mapping, keys in (
            ("capacity", self.capacity, self.reservoir_ids),
            ("source_cost", self.source_cost, self.reservoir_ids),
            ("hydrogen_ion", self.hydrogen_ion, self.reservoir_ids),
            ("alkalinity", self.alkalinity, self.reservoir_ids),
            ("turbidity", self.turbidity, self.reservoir_ids),
            ("chemical_cost", self.chemical_cost, self.chemical_ids),
        ):
            for key in keys:
                if key not in mapping:
                    issues.append(f"{group_name} is missing a value for '{key}'.")

        for label, value in (
            ("demand", self.demand),
            ("hydrogen_ion_min", self.hydrogen_ion_min),
            ("hydrogen_ion_max", self.hydrogen_ion_max),
            ("alkalinity_min", self.alkalinity_min),
            ("alkalinity_max", self.alkalinity_max),
            ("turbidity_min", self.turbidity_min),
            ("turbidity_max", self.turbidity_max),
            ("big_m_dosing", self.big_m_dosing),
            ("batch_capacity", self.batch_capacity),
            ("energy_rate", self.energy_rate),
        ):
            if value is None or not math.isfinite(value):
                issues.append(f"'{label}' must be a finite number.")

        if self.hydrogen_ion_min > self.hydrogen_ion_max:
            issues.append("hydrogen_ion_min must be <= hydrogen_ion_max.")
        if self.alkalinity_min > self.alkalinity_max:
            issues.append("alkalinity_min must be <= alkalinity_max.")
        if self.turbidity_min > self.turbidity_max:
            issues.append("turbidity_min must be <= turbidity_max.")
        if self.batch_capacity <= 0:
            issues.append("batch_capacity must be strictly positive.")

        if issues:
            raise ModelBuildError(
                "Toy model inputs are invalid:\n" + "\n".join(f"- {i}" for i in issues)
            )


def ph_to_hydrogen_ion(ph: float) -> float:
    """Convert pH to hydrogen-ion concentration in mol/L: [H+] = 10^-pH."""
    return 10.0 ** (-ph)


# ---------------------------------------------------------------------------
# Variable container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToyModelVariables:
    """PuLP variables, grouped exactly as listed in Section 4."""

    x: dict[str, pulp.LpVariable]           # x_i: continuous >= 0
    y: dict[str, pulp.LpVariable]           # y_i: binary
    d: dict[str, pulp.LpVariable]           # d_c: continuous >= 0
    y_facility: pulp.LpVariable             # binary
    b: pulp.LpVariable                      # integer >= 0


# ---------------------------------------------------------------------------
# Model construction (Sections 4, 5, 7)
# ---------------------------------------------------------------------------


def build_toy_model(
    inputs: ToyModelInputs,
    *,
    include_redundant_capacity_constraints: bool = False,
) -> tuple[pulp.LpProblem, ToyModelVariables]:
    """Build the toy MILP exactly as specified in Sections 4, 5, and 7.

    Constraint 2 (individual source capacity, x_i <= Capacity_i) is
    mathematically redundant once constraint 3 exists, as noted in the
    formulation. It is left out by default and can be switched on with
    `include_redundant_capacity_constraints=True` purely to inspect it.
    """
    inputs.validate()
    reservoirs = inputs.reservoir_ids
    chemicals = inputs.chemical_ids

    prob = pulp.LpProblem("AquaBlend_Toy_Model", pulp.LpMinimize)

    # --- Section 4: Decision Variables ---
    x = {i: pulp.LpVariable(f"x_{i}", lowBound=0, cat="Continuous") for i in reservoirs}
    y = {i: pulp.LpVariable(f"y_{i}", cat="Binary") for i in reservoirs}
    d = {c: pulp.LpVariable(f"d_{c}", lowBound=0, cat="Continuous") for c in chemicals}
    y_facility = pulp.LpVariable("y_facility", cat="Binary")
    b = pulp.LpVariable("b", lowBound=0, cat="Integer")

    variables = ToyModelVariables(x=x, y=y, d=d, y_facility=y_facility, b=b)

    total_draw = pulp.lpSum(x[i] for i in reservoirs)

    # --- Section 5: Objective Function ---
    # Minimise: sum(x_i * Cost_i) + sum(d_c * Cost_c) + (sum x_i) * EnergyRate
    source_draw_cost = pulp.lpSum(x[i] * inputs.source_cost[i] for i in reservoirs)
    dosing_cost = pulp.lpSum(d[c] * inputs.chemical_cost[c] for c in chemicals)
    energy_cost = total_draw * inputs.energy_rate
    prob += source_draw_cost + dosing_cost + energy_cost, "Total_Cost"

    # --- Section 7, Constraint 1: Demand satisfaction ---
    prob += total_draw >= inputs.demand, "Demand_Satisfaction"

    # --- Section 7, Constraint 2: Source capacity (redundant; optional) ---
    if include_redundant_capacity_constraints:
        for i in reservoirs:
            prob += x[i] <= inputs.capacity[i], f"Source_Capacity_{i}"

    # --- Section 7, Constraint 3: Source activation linking ---
    for i in reservoirs:
        prob += x[i] <= inputs.capacity[i] * y[i], f"Activation_Linking_{i}"

    # --- Section 7, Constraint 4: pH range (via [H+]) ---
    blended_hydrogen_ion = pulp.lpSum(x[i] * inputs.hydrogen_ion[i] for i in reservoirs)
    prob += (
        blended_hydrogen_ion >= inputs.hydrogen_ion_min * total_draw,
        "pH_Lower_Bound",
    )
    prob += (
        blended_hydrogen_ion <= inputs.hydrogen_ion_max * total_draw,
        "pH_Upper_Bound",
    )

    # --- Section 7, Constraint 5: Alkalinity range ---
    blended_alkalinity = pulp.lpSum(x[i] * inputs.alkalinity[i] for i in reservoirs)
    prob += (
        blended_alkalinity >= inputs.alkalinity_min * total_draw,
        "Alkalinity_Lower_Bound",
    )
    prob += (
        blended_alkalinity <= inputs.alkalinity_max * total_draw,
        "Alkalinity_Upper_Bound",
    )

    # --- Section 7, Constraint 6: Turbidity range ---
    blended_turbidity = pulp.lpSum(x[i] * inputs.turbidity[i] for i in reservoirs)
    prob += (
        blended_turbidity >= inputs.turbidity_min * total_draw,
        "Turbidity_Lower_Bound",
    )
    prob += (
        blended_turbidity <= inputs.turbidity_max * total_draw,
        "Turbidity_Upper_Bound",
    )

    # --- Section 7, Constraint 7: Facility activation linking ---
    for c in chemicals:
        prob += d[c] <= inputs.big_m_dosing * y_facility, f"Facility_Activation_{c}"

    # --- Section 7, Constraint 8: Batch sufficiency ---
    prob += total_draw <= inputs.batch_capacity * b, "Batch_Sufficiency"

    # --- Section 7, Constraint 9: Domain and non-negativity ---
    # Enforced directly via variable construction above (lowBound=0, cat=...).

    return prob, variables


# ---------------------------------------------------------------------------
# Solving
# ---------------------------------------------------------------------------


def solve_toy_model(prob: pulp.LpProblem, *, msg: bool = False) -> str:
    """Solve with HiGHS. Raises ModelSolveError if HiGHS is unavailable."""
    available = pulp.listSolvers(onlyAvailable=True)
    if "HiGHS" not in available:
        raise ModelSolveError(
            "The HiGHS solver is not available in this environment. "
            "Install it with: pip install highspy. "
            f"Solvers currently available: {available}"
        )

    solver = pulp.HiGHS(msg=msg)
    prob.solve(solver)
    return pulp.LpStatus[prob.status]
