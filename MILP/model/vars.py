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

# Decision variables
s_active = [source + " active" for source in sources]
s_vol = [source + " vol" for source in sources]
t_active = [plant + " active" for plant in plants]
s_t_active = [source + plant + " active" for plant in plants for source in sources]
s_t_vol = [source + plant + " volume" for plant in plants for source in sources]
t_z_active = [plant + zone + " active" for zone in zones for plant in plants]
t_z_vol = [plant + zone + " volume" for zone in zones for plant in plants]

# Constraints