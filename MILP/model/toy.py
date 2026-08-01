import pulp
from MILP.model.vars import (
    sources, plants, zones,
    s_active, s_vol, t_active, s_t_active, s_t_vol, t_z_active, t_z_vol,
    source_activation_cost, source_draw_cost,
    plant_activation_cost, plant_treatment_cost,
    demand,
    source_min_withdrawal, source_max_withdrawal,
    plant_min_throughput, plant_max_throughput,
    source_plant_link_capacity, plant_zone_link_capacity,
    quality_parameters, source_quality,
    quality_lower_limit, quality_upper_limit,
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
# Read in constraints

# Demand satisfaction: total volume delivered to each zone must meet demand.
#   sum_t c_tz >= D_z, for all z
for z in zones:
    problem += (
        pulp.lpSum(t_z_vol_vars[(t, z)] for t in plants) >= demand[z]
    ), f"Demand satisfaction - {z}"
 
# Source capacity and activation: draw is zero if inactive, else between
# min/max withdrawal.
#   W_s_min * alpha_s <= a_s <= W_s_max * alpha_s, for all s
for s in sources:
    problem += (
        s_vol_vars[s] >= source_min_withdrawal[s] * s_active_vars[s]
    ), f"Source min withdrawal - {s}"
    problem += (
        s_vol_vars[s] <= source_max_withdrawal[s] * s_active_vars[s]
    ), f"Source max withdrawal - {s}"
 
# Plant capacity and activation: throughput is zero if inactive, else between
# min/max throughput.
#   V_t_min * beta_t <= sum_s b_st <= V_t_max * beta_t, for all t
for t in plants:
    incoming_volume = pulp.lpSum(s_t_vol_vars[(s, t)] for s in sources)
    problem += (
        incoming_volume >= plant_min_throughput[t] * t_active_vars[t]
    ), f"Plant min throughput - {t}"
    problem += (
        incoming_volume <= plant_max_throughput[t] * t_active_vars[t]
    ), f"Plant max throughput - {t}"
 
# Plant flow conservation: outflow to zones equals inflow from sources.
#   sum_z c_tz == sum_s b_st, for all t
for t in plants:
    problem += (
        pulp.lpSum(t_z_vol_vars[(t, z)] for z in zones)
        == pulp.lpSum(s_t_vol_vars[(s, t)] for s in sources)
    ), f"Plant flow conservation - {t}"
 
# Source flow conservation: volume drawn equals volume sent to all plants.
#   a_s == sum_t b_st, for all s
for s in sources:
    problem += (
        s_vol_vars[s] == pulp.lpSum(s_t_vol_vars[(s, t)] for t in plants)
    ), f"Source flow conservation - {s}"
 
# Link capacity: source-plant links capped and zero unless active.
#   b_st <= L_st * gamma_st, for all s, t
for s in sources:
    for t in plants:
        problem += (
            s_t_vol_vars[(s, t)]
            <= source_plant_link_capacity[(s, t)] * s_t_active_vars[(s, t)]
        ), f"Source-plant link capacity - {s} -> {t}"
 
# Link capacity: plant-zone links capped and zero unless active.
#   c_tz <= L_tz * delta_tz, for all t, z
for t in plants:
    for z in zones:
        problem += (
            t_z_vol_vars[(t, z)]
            <= plant_zone_link_capacity[(t, z)] * t_z_active_vars[(t, z)]
        ), f"Plant-zone link capacity - {t} -> {z}"
 
# Links require active nodes: a link can only be active if its upstream
# node is active.
#   gamma_st <= alpha_s, for all s, t
for s in sources:
    for t in plants:
        problem += (
            s_t_active_vars[(s, t)] <= s_active_vars[s]
        ), f"Source-plant link requires active source - {s} -> {t}"
 
#   delta_tz <= beta_t, for all t, z
for t in plants:
    for z in zones:
        problem += (
            t_z_active_vars[(t, z)] <= t_active_vars[t]
        ), f"Plant-zone link requires active plant - {t} -> {z}"
 
# Water quality: blended value of each parameter at each plant must stay
# within regulatory limits.
#   Q_p_min * sum_s b_st <= sum_s Q_sp * b_st <= Q_p_max * sum_s b_st,
#   for all t, p
for t in plants:
    incoming_volume = pulp.lpSum(s_t_vol_vars[(s, t)] for s in sources)
    for p in quality_parameters:
        quality_load = pulp.lpSum(
            source_quality[(s, p)] * s_t_vol_vars[(s, t)] for s in sources
        )
        problem += (
            quality_load >= quality_lower_limit[p] * incoming_volume
        ), f"Water quality lower bound - {t} - {p}"
        problem += (
            quality_load <= quality_upper_limit[p] * incoming_volume
        ), f"Water quality upper bound - {t} - {p}"

# Write problem data to lp file
problem.writeLP("AquablendModel.lp")

# Solve problem (using built in cbc - use pip install "pulp[cbc]")
problem.solve()
print("Status:", pulp.LpStatus[problem.status])

# Print optimal variable values
for v in problem.variables():
    print(v.name, "=", v.varValue)

# Print objective output
print("Total Cost of Water Delivery = ", pulp.value(problem.objective))
