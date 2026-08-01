from __future__ import annotations

import math

import pulp

from model import (
    ModelSolveError,
    ToyModelInputs,
    build_toy_model,
    ph_to_hydrogen_ion,
    solve_toy_model,
)

def sample_inputs(*, demand: float = 12.0) -> ToyModelInputs:
    """A hand-inspectable 3-reservoir / 2-chemical toy scenario.

    Reservoir R2 is deliberately the cheapest but has the worst turbidity, so
    the quality constraints should visibly limit how much of it gets used.
    """
    reservoir_ids = ("R1", "R2", "R3")
    chemical_ids = ("C1", "C2")

    ph_raw = {"R1": 7.4, "R2": 6.6, "R3": 8.0}

    return ToyModelInputs(
        reservoir_ids=reservoir_ids,
        chemical_ids=chemical_ids,
        capacity={"R1": 8.0, "R2": 10.0, "R3": 6.0},
        source_cost={"R1": 140.0, "R2": 90.0, "R3": 160.0},
        hydrogen_ion={i: ph_to_hydrogen_ion(ph_raw[i]) for i in reservoir_ids},
        alkalinity={"R1": 140.0, "R2": 60.0, "R3": 210.0},
        turbidity={"R1": 1.8, "R2": 6.5, "R3": 0.9},
        chemical_cost={"C1": 25.0, "C2": 40.0},
        demand=demand,
        hydrogen_ion_min=ph_to_hydrogen_ion(8.5),  # pH upper -> [H+] lower
        hydrogen_ion_max=ph_to_hydrogen_ion(6.5),  # pH lower -> [H+] upper
        alkalinity_min=50.0,
        alkalinity_max=250.0,
        turbidity_min=0.0,
        turbidity_max=4.0,
        big_m_dosing=50.0,
        batch_capacity=5.0,
        energy_rate=15.0,
    )


def print_formulation(prob: pulp.LpProblem) -> None:
    print("FULL LP FORMULATION (as built by PuLP)")


def print_solution(prob: pulp.LpProblem, variables, inputs: ToyModelInputs, status: str) -> None:
    print(f"SOLVE STATUS: {status}")

    if status != "Optimal":
        print("No optimal solution to report. Check status above.")
        if status == "Infeasible":
            print(
                "Infeasible: try widening the quality bounds, raising capacities, "
                "or lowering demand, then re-run."
            )
        return

    x_vals = {i: variables.x[i].value() for i in inputs.reservoir_ids}
    y_vals = {i: variables.y[i].value() for i in inputs.reservoir_ids}
    d_vals = {c: variables.d[c].value() for c in inputs.chemical_ids}
    total_draw = sum(x_vals.values())

    print("\n-- Source draws (x_i) and activation (y_i) --")
    for i in inputs.reservoir_ids:
        print(
            f"  {i}: x_{i} = {x_vals[i]:8.4f} ML   "
            f"(capacity {inputs.capacity[i]:6.2f})   "
            f"y_{i} = {int(round(y_vals[i]))}"
        )
    print(f"  Total draw (sum x_i)          = {total_draw:.4f} ML")
    print(f"  Demand required               = {inputs.demand:.4f} ML")

    print("\n-- Dosing (d_c) and facility activation --")
    for c in inputs.chemical_ids:
        print(f"  {c}: d_{c} = {d_vals[c]:8.4f} kg  (BigM = {inputs.big_m_dosing:.2f})")
    print(f"  y_facility = {int(round(variables.y_facility.value()))}")

    print("\n-- Batch count --")
    b_val = variables.b.value()
    print(
        f"  b = {b_val:.0f}   "
        f"(BatchCapacity = {inputs.batch_capacity:.2f} -> "
        f"max routable = {inputs.batch_capacity * b_val:.2f} ML)"
    )

    print("\n-- Blended water quality (computed from the solution) --")
    if total_draw > 1e-9:
        blended_h = sum(x_vals[i] * inputs.hydrogen_ion[i] for i in inputs.reservoir_ids) / total_draw
        blended_ph = -math.log10(blended_h)
        blended_alk = sum(x_vals[i] * inputs.alkalinity[i] for i in inputs.reservoir_ids) / total_draw
        blended_turb = sum(x_vals[i] * inputs.turbidity[i] for i in inputs.reservoir_ids) / total_draw
        print(f"  Blended pH          = {blended_ph:.3f}  (allowed 6.5 - 8.5)")
        print(f"  Blended alkalinity  = {blended_alk:.2f} mg/L CaCO3  (allowed {inputs.alkalinity_min:.0f} - {inputs.alkalinity_max:.0f})")
        print(f"  Blended turbidity   = {blended_turb:.3f} NTU  (allowed {inputs.turbidity_min:.0f} - {inputs.turbidity_max:.0f})")
    else:
        print("  No water drawn; blended quality is undefined.")

    print("\n-- Objective decomposition --")
    source_cost = sum(x_vals[i] * inputs.source_cost[i] for i in inputs.reservoir_ids)
    dosing_cost = sum(d_vals[c] * inputs.chemical_cost[c] for c in inputs.chemical_ids)
    energy_cost = total_draw * inputs.energy_rate
    print(f"  Source draw cost   = {source_cost:10.2f}")
    print(f"  Dosing cost        = {dosing_cost:10.2f}")
    print(f"  Energy cost        = {energy_cost:10.2f}")
    print(f"  TOTAL OBJECTIVE    = {pulp.value(prob.objective):10.2f}")

    print("\n-- Constraint slacks (0.0000 means binding / active) --")
    for name, constraint in prob.constraints.items():
        # PuLP stores each constraint as expr <op> 0 internally; .value() is
        # the LHS-minus-RHS value, so its magnitude tells us how far from
        # binding each constraint currently is.
        print(f"  {name:28s} slack = {-constraint.value():10.4f}")


def run_scenario(label: str, inputs: ToyModelInputs) -> None:
    print(f"SCENARIO: {label}")

    prob, variables = build_toy_model(inputs)
    print_formulation(prob)

    try:
        status = solve_toy_model(prob, msg=False)
    except ModelSolveError as exc:
        print(f"\nSolver error: {exc}")
        return

    print_solution(prob, variables, inputs, status)


def main() -> None:
    # Baseline: demand comfortably inside what the network can deliver.
    run_scenario("Baseline (demand = 12 ML)", sample_inputs(demand=12.0))

    # Stress test: push demand up near total capacity to see constraints bind.
    run_scenario("High demand (demand = 22 ML)", sample_inputs(demand=22.0))

    # Infeasible: demand exceeds total capacity (8 + 10 + 6 = 24 ML).
    run_scenario("Infeasible (demand = 30 ML)", sample_inputs(demand=30.0))


if __name__ == "__main__":
    main()
