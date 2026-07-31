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