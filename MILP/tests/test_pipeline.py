import json

from src.contracts import (
    DemandZoneInput,
    PlantInput,
    ScenarioData,
    SourceInput,
    SourcePlantLinkInput,
    PlantZoneLinkInput,
)
from src.preprocessing import preprocess_scenario
from src.model import build_model, solve
from src.postprocessing import SolverVariables, postprocess_solution, print_solution_summary

# Illustrative-only quality/cost values (the real scenario sources this from
# Supabase; the JSON file only carries withdrawal overrides).
SOURCE_INFO = {
    "225103": dict(name="Thomson Reservoir", cost=450.0, pH=7.2, alkalinity=45.0, turbidity=1.2),
    "229421": dict(name="O'Shannassy Reservoir", cost=380.0, pH=6.8, alkalinity=30.0, turbidity=0.8),
    "233217": dict(name="Barwon River at Geelong", cost=520.0, pH=7.6, alkalinity=85.0, turbidity=2.5),
}

with open("config/scenarios/base_scenarios_v1.json") as f:
    raw = json.load(f)

sources = tuple(
    SourceInput(
        source_id=s["source_id"],
        name=s["name"],
        source_type="reservoir",
        enabled=s["enabled"],
        forced_inactive=s["forced_inactive"],
        minimum_withdrawal_ml_per_day=s["minimum_withdrawal_ml_per_day_override"],
        maximum_withdrawal_ml_per_day=s["max_available_ml_per_day_override"],
        withdrawal_bounds_origin="scenario_override",
        fixed_activation_cost=s["fixed_activation_cost"],
        cost_per_ml=SOURCE_INFO[s["source_id"]]["cost"],
        quality={
            "pH": SOURCE_INFO[s["source_id"]]["pH"],
            "alkalinity": SOURCE_INFO[s["source_id"]]["alkalinity"],
            "turbidity": SOURCE_INFO[s["source_id"]]["turbidity"],
        },
        has_estimated_values=False,
        database_model_ready=True,
        availability_status="available",
        provenance={},
    )
    for s in raw["sources"]
)

plants = tuple(
    PlantInput(
        plant_id=p["plant_id"],
        name=p["name"],
        enabled=p["enabled"],
        minimum_processing_capacity_ml_per_day=p["minimum_processing_capacity_ml_per_day"],
        maximum_processing_capacity_ml_per_day=p["maximum_processing_capacity_ml_per_day"],
        fixed_activation_cost=p["fixed_activation_cost"],
        treatment_cost_per_ml=p["treatment_cost_per_ml"],
    )
    for p in raw["network"]["plants"]
)

demand_zones = tuple(
    DemandZoneInput(
        zone_id=z["zone_id"],
        name=z["name"],
        demand_ml_per_day=z["demand_ml_per_day"],
    )
    for z in raw["network"]["demand_zones"]
)

source_links = tuple(
    SourcePlantLinkInput(
        source_id=l["source_id"],
        plant_id=l["plant_id"],
        enabled=l["enabled"],
        maximum_flow_ml_per_day=l["maximum_flow_ml_per_day"],
    )
    for l in raw["network"]["source_to_plant_links"]
)

zone_links = tuple(
    PlantZoneLinkInput(
        plant_id=l["plant_id"],
        zone_id=l["zone_id"],
        enabled=l["enabled"],
        maximum_flow_ml_per_day=l["maximum_flow_ml_per_day"],
    )
    for l in raw["network"]["plant_to_zone_links"]
)

scenario = ScenarioData(
    scenario_id=raw["scenario_id"],
    scenario_name=raw["scenario_name"],
    status=raw["status"],
    description=raw["description"],
    sources=sources,
    plants=plants,
    demand_zones=demand_zones,
    source_to_plant_links=source_links,
    plant_to_zone_links=zone_links,
    quality_limits=raw["quality_limits"],
    validation_issues=(),
)

parameters = preprocess_scenario(scenario)

model, results = solve(parameters)

solved = postprocess_solution(scenario, parameters, model, results)
print_solution_summary(solved)

import json as _json
from dataclasses import asdict
print("\n--- raw solved dataclass dump ---")
print(_json.dumps(asdict(solved), indent=2, default=str))