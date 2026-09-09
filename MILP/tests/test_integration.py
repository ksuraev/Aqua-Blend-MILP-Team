import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.data_loader import load_scenario
from src.preprocessing import preprocess_scenario


TOY_SCENARIO_PATH = "config/scenarios/toy_scenario.json"



# COMPLETE PIPELINE

def test_loader_to_preprocessing_pipeline():
    """
    JSON should successfully pass through the complete pipeline:
    JSON -> ScenarioData -> ModelParameters.
    """
    scenario = load_scenario(TOY_SCENARIO_PATH)
    parameters = preprocess_scenario(scenario)

    assert parameters is not None

    assert set(parameters.source_ids) == {
        source.source_id
        for source in scenario.sources
    }

    assert set(parameters.plant_ids) == {
        plant.plant_id
        for plant in scenario.plants
    }

    assert set(parameters.zone_ids) == {
        zone.zone_id
        for zone in scenario.demand_zones
    }

#  NETWORK PRESERVATION

def test_network_preserved_through_pipeline():
    """Network links loaded from JSON should reach ModelParameters unchanged."""
    scenario = load_scenario(TOY_SCENARIO_PATH)
    parameters = preprocess_scenario(scenario)

    expected_source_plant = {
        (link.source_id, link.plant_id)
        for link in scenario.source_to_plant_links
    }

    expected_plant_zone = {
        (link.plant_id, link.zone_id)
        for link in scenario.plant_to_zone_links
    }

    assert set(parameters.source_plant_arcs) == expected_source_plant
    assert set(parameters.plant_zone_arcs) == expected_plant_zone


# CORE DATA PRESERVATION

def test_core_values_preserved_through_pipeline():
    """
    Important costs, demands and quality data should survive
    loading and preprocessing.
    """
    scenario = load_scenario(TOY_SCENARIO_PATH)
    parameters = preprocess_scenario(scenario)

    # Source costs
    for source in scenario.sources:
        source_id = source.source_id

        assert (
            parameters.source_fixed_cost[source_id]
            == source.fixed_activation_cost
        )

        if source.cost_per_ml is not None:
            assert (
                parameters.source_unit_cost[source_id]
                == source.cost_per_ml
            )

    # Demand values
    for zone in scenario.demand_zones:
        assert (
            parameters.demand_by_zone[zone.zone_id]
            == zone.demand_ml_per_day
        )

    # Every source should still have a value
    # for every model quality parameter.
    for source in scenario.sources:
        for quality_parameter in parameters.quality_parameter_ids:
            assert (
                source.source_id,
                quality_parameter
            ) in parameters.source_quality

