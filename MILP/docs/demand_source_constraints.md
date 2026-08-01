# Demand, Source Activation and Source Capacity

## Purpose

This component implements the source-side constraints for the AquaBlend
Sprint 1 toy MILP using PuLP. It adds constraints to the model assembled in
`MILP/src/solver/model.py` and reuses the `flow` and `active` variables created
by `MILP/src/solver/variables.py`.

## Mathematical formulation

For sources `s` and demand zones `z`, the implemented constraints are:

```text
sum(flow[s]) >= sum(demand[z])

minimum_withdrawal[s] * active[s] <= flow[s]
flow[s] <= maximum_withdrawal[s] * active[s]
```

The upper activation constraint forces source flow to zero when the source is
inactive. When activated, its withdrawal remains between the configured lower
and upper limits.

## Files

- `MILP/src/solver/constraints.py`: constraint construction and validation.
- `MILP/tests/test_constraints.py`: valid, invalid, boundary and infeasible tests.

## Compatibility

The entry point is:

```python
add_constraints(model, data, variables)
```

It supports both solver input structures currently present in the team work:

1. The formulation-ready `ModelParameters` produced by `preprocessing.py`.
2. The reservoir-based `SolverInput` proposed by the modular solver branch.

Variables may be supplied as a dictionary or as a dataclass/object containing
`flow` and `active` attributes. No decision variables are recreated.

The current preprocessing branch does not expose `source_min_withdrawal` in
`ModelParameters`. Until that field is added, the documented default of zero
is used. The input contract already contains
`minimum_withdrawal_ml_per_day`, so preprocessing should eventually carry it
through as `source_min_withdrawal`.

## Scope boundaries

This component does not implement the objective, decision-variable creation,
plant throughput, water quality, pH transformation, solver execution or result
formatting. Those remain separate components.

## Environment

Use the team-standard Python version and a project-specific environment:

```bash
py -3.11 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install pulp highspy pytest
```

On macOS or Linux, activate with:

```bash
source .venv/bin/activate
```

## Run the tests

From the repository root:

```bash
python -m pytest MILP/tests/test_constraints.py -v
```

Expected result: all tests pass. HiGHS must be available through the
`highspy` package.

## Integration checklist

- Use the final formulation in `MILP/docs/formulation.pdf`.
- Keep the existing solver filenames and imports unchanged.
- Confirm that `flow` and `active` use the same source identifiers as the
  processed input.
- Carry minimum source withdrawal from the input contract into preprocessing.
- Do not create duplicate variables or objective terms.
- Run this component's tests and the complete team test suite.
- Check the HiGHS status before reporting results.
- Record assumptions, tests and limitations in the pull request description.
