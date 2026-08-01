# Libraries
from typing import Dict, Tuple

# Resources
sources = [
    "Reservoir A", 
    "Reservoir B", 
    "Reservoir C"
    ]

plants = [
    "Plant 1"
]

zones = [
    "Demand Zone 1"
]

# Costs (mock units)
# Cost of activating each source
source_activation_cost: Dict[str, float] = {
    "Reservoir A": 10,
    "Reservoir B": 15,
    "Reservoir C": 12,
}
 
# Cost of activating each plant
plant_activation_cost: Dict[str, float] = {
    "Plant 1": 45,
}
 
# Cost of drawing water from each source
source_draw_cost: Dict[str, float] = {
    "Reservoir A": 1,
    "Reservoir B": 0.8,
    "Reservoir C": 0.9,
}
 
# Cost of treating water at each plant
plant_treatment_cost: Dict[str, float] = {
    "Plant 1": 1,
}

# Demand (D_z)
demand: Dict[str, float] = {
    "Demand Zone 1": 5.0,
}

# Source withdrawal limits (W_s underbar/overbar)
source_min_withdrawal: Dict[str, float] = {
    "Reservoir A": 0.0,
    "Reservoir B": 0.0,
    "Reservoir C": 0.0,
}
source_max_withdrawal: Dict[str, float] = {
    "Reservoir A": 10.0,
    "Reservoir B": 8.0,
    "Reservoir C": 6.0,
}

# Plant throughput limits (V_t underbar/overbar)
plant_min_throughput: Dict[str, float] = {
    "Plant 1": 0.0,
}
plant_max_throughput: Dict[str, float] = {
    "Plant 1": 20.0,
}

# Link capacities (L_st, L_tz overbar)
source_plant_link_capacity: Dict[Tuple[str, str], float] = {
    (s, p): 10.0 for s in sources for p in plants
}
plant_zone_link_capacity: Dict[Tuple[str, str], float] = {
    (p, z): 20.0 for p in plants for z in zones
}

# Water quality (mock)
# Quality parameters tracked in the model
quality_parameters = ["turbidity", "alkalinity", "pH"]
 
# Q_sp: value of quality parameter p in source s.
# pH is stored pre-transformed as 10^-pH (hydrogen ion concentration) so it
# blends linearly, per the formulation's pH transformation.
source_quality: Dict[Tuple[str, str], float] = {
    ("Reservoir A", "turbidity"): 2.5,
    ("Reservoir A", "alkalinity"): 120.0,
    ("Reservoir A", "pH"): 10 ** -7.2,
    ("Reservoir B", "turbidity"): 4.0,
    ("Reservoir B", "alkalinity"): 95.0,
    ("Reservoir B", "pH"): 10 ** -6.8,
    ("Reservoir C", "turbidity"): 1.8,
    ("Reservoir C", "alkalinity"): 110.0,
    ("Reservoir C", "pH"): 10 ** -7.5,
}
 
# Regulatory limits per quality parameter (Q_p underbar/overbar).
# For pH the raw acceptable range is 6.5-8.5; per the pH bound transformation
# (eq. ph_bound_transformation) this flips and becomes 10^-8.5 to 10^-6.5.
quality_lower_limit: Dict[str, float] = {
    "turbidity": 0.0,
    "alkalinity": 20.0,
    "pH": 10 ** -8.5,
}
quality_upper_limit: Dict[str, float] = {
    "turbidity": 5.0,
    "alkalinity": 200.0,
    "pH": 10 ** -6.5,
}

# Decision variables
s_active: Dict[str, str] = {s: f"{s} active" for s in sources}
s_vol: Dict[str, str] = {s: f"{s} vol" for s in sources}
t_active: Dict[str, str] = {p: f"{p} active" for p in plants}
s_t_active: Dict[Tuple[str, str], str] = {
    (s, p): f"{s} -> {p} active" for s in sources for p in plants
}
s_t_vol: Dict[Tuple[str, str], str] = {
    (s, p): f"{s} -> {p} volume" for s in sources for p in plants
}
t_z_active: Dict[Tuple[str, str], str] = {
    (p, z): f"{p} -> {z} active" for p in plants for z in zones
}
t_z_vol: Dict[Tuple[str, str], str] = {
    (p, z): f"{p} -> {z} volume" for p in plants for z in zones
}