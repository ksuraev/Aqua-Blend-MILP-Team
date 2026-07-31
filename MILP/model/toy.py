import pulp
from MILP.model.vars import (
    sources, plants, zones,
    s_active, s_vol, t_active, s_t_active, s_t_vol, t_z_active, t_z_vol,
    source_activation_cost, source_draw_cost,
    plant_activation_cost, plant_treatment_cost,
)

# Define the problem
problem = pulp.LpProblem("Aquablend Toy", pulp.LpMinimize)

# Variables
s_active_vars = problem.add_variable_dict(
    "Active sources", (s_active,), 0, None, pulp.LpBinary
    )
s_vol_vars = problem.add_variable_dict(
    "Source volumes", (s_vol,), 0, None, pulp.LpContinuous
    )
t_active_vars = problem.add_variable_dict(
    "Active plants", (t_active,), 0, None, pulp.LpBinary
    )
s_t_active_vars = problem.add_variable_dict(
    "Active source-plant links", (s_t_active,), 0, None, pulp.LpBinary
    )
s_t_vol_vars = problem.add_variable_dict(
    "Source-plant volumes", (s_t_vol,), 0, None, pulp.LpContinuous
    )
t_z_active_vars = problem.add_variable_dict(
    "Active plant-demand zone links", (t_z_active,), 0, None, pulp.LpBinary
    )
t_z_vol_vars = problem.add_variable_dict(
    "Plant-demand zone volumes", (t_z_vol,), 0, None, pulp.LpContinuous
    )

# Objective
source_activation_term = pulp.lpSum(
    source_activation_cost[s] * s_active_vars[s] for s in sources
)

source_draw_term = pulp.lpSum(
    source_draw_cost[s] * s_vol_vars[s] for s in sources
)

plant_activation_term = pulp.lpSum(
    plant_activation_cost[p] * t_active_vars[p] for p in plants
)

plant_treatment_term = pulp.lpSum(
    plant_treatment_cost[p]
    * pulp.lpSum(s_t_vol_vars[(s, p)] for s in sources) # assuming conservation
    for p in plants
)

problem += (
    source_activation_term
    + source_draw_term
    + plant_activation_term
    + plant_treatment_term
), "Total cost"

# Constraints


# Write problem data to lp file
problem.writeLP("WhiskasModel.lp")

# Solve problem (using built in cbc - use pip install "pulp[cbc]")
problem.solve()
print("Status:", pulp.LpStatus[problem.status])

# Print optimal variable values
for v in problem.variables():
    print(v.name, "=", v.varValue)
# END print_var_value

# Print objective output
print("Total Cost of Ingredients per can = ", pulp.value(problem.objective))
