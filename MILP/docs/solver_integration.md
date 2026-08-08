# AquaBlend HiGHS Solver Integration

## Scope

This component provides the processed-input adapter, HiGHS configuration,
solver execution, status validation and structured result extraction required
to connect the modular AquaBlend toy model.

## Files

- `MILP/src/solver/config.py` defines validated HiGHS settings.
- `MILP/src/solver/input_schema.py` defines the simplified solver input and
  adapts the preprocessing `ModelParameters` object.
- `MILP/src/solver/solve.py` runs HiGHS and safely returns results.
- `MILP/tests/test_solver_integration.py` tests configuration, feasible and
  infeasible solves, status handling and input adaptation.

## Dependencies

Use Python 3.11.9 in a project-specific environment:

```bash
python -m pip install pulp highspy pytest
```

## Testing

From the repository root:

```bash
python -m pytest MILP/tests/test_solver_integration.py -v
```

## Integration usage

```python
from MILP.src.solver.config import SolverConfig
from MILP.src.solver.solve import solve_model

model, variables = build_model(data)
result = solve_model(
    model,
    variables,
    SolverConfig(message=False, time_limit_seconds=60),
)

if result.has_solution:
    print(result.objective_value)
    print(result.source_flows)
else:
    print(result.status)
```

## Coordination requirements

Before end-to-end integration, the decision-variable branch must define a
consistent `ModelVariables` container and correct its safe-name helper call.
The preprocessing component should eventually expose
`source_min_withdrawal`; until then the documented default of zero is used.

The implementation does not replace the demand/source constraints, objective,
decision variables, plant/water-quality constraints or post-processing owned
by other components.
