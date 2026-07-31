import pulp
from vars_dict import s_active, s_vol, t_active, s_t_active, \
    s_t_vol, t_z_active, t_z_vol

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