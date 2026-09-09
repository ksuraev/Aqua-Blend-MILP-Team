import sys
from pathlib import Path
from dataclasses import replace

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from src.data_loader import load_scenario
from src.preprocessing import preprocess_scenario, PreprocessingError


# ============================================================
# FIXTURES
# ============================================================

@pytest.fixture
def toy_scenario():
    return load_scenario("config/scenarios/toy_scenario.json")


@pytest.fixture
def toy_parameters(toy_scenario):
    return preprocess_scenario(toy_scenario)


#  SUCCESSFUL PREPROCESSING


def test_preprocess_scenario(toy_parameters):
    """A valid scenario should produce usable ModelParameters."""
    assert toy_parameters is not None

    assert len(toy_parameters.source_ids) > 0
    assert len(toy_parameters.plant_ids) > 0
    assert len(toy_parameters.zone_ids) > 0

    assert toy_parameters.source_quality


# INVALID SCENARIO REJECTION


def test_preprocessing_rejects_validation_issues(toy_scenario):
    """Preprocessing should reject scenarios with loader validation issues."""
    invalid_scenario = replace(
        toy_scenario,
        validation_issues=("Test validation error",)
    )

    with pytest.raises(
        PreprocessingError,
        match="validation issues"
    ):
        preprocess_scenario(invalid_scenario)


# IDENTIFIER AND NETWORK PRESERVATION


def test_identifiers_preserved(toy_scenario, toy_parameters):
    """Source, plant and zone IDs should survive preprocessing."""
    assert set(toy_parameters.source_ids) == {
        source.source_id
        for source in toy_scenario.sources
    }

    assert set(toy_parameters.plant_ids) == {
        plant.plant_id
        for plant in toy_scenario.plants
    }

    assert set(toy_parameters.zone_ids) == {
        zone.zone_id
        for zone in toy_scenario.demand_zones
    }


def test_network_arcs_preserved(toy_scenario, toy_parameters):
    """Network connections should be preserved during preprocessing."""
    expected_source_plant = {
        (link.source_id, link.plant_id)
        for link in toy_scenario.source_to_plant_links
    }

    expected_plant_zone = {
        (link.plant_id, link.zone_id)
        for link in toy_scenario.plant_to_zone_links
    }

    assert set(toy_parameters.source_plant_arcs) == expected_source_plant
    assert set(toy_parameters.plant_zone_arcs) == expected_plant_zone


#  CORE VALUE PRESERVATION


def test_cost_and_demand_values_preserved(
    toy_scenario,
    toy_parameters
):
    """Core costs and demands should retain their original values."""

    for source in toy_scenario.sources:
        sid = source.source_id

        assert (
            toy_parameters.source_fixed_cost[sid]
            == source.fixed_activation_cost
        )

        if source.cost_per_ml is not None:
            assert (
                toy_parameters.source_unit_cost[sid]
                == source.cost_per_ml
            )

    for plant in toy_scenario.plants:
        pid = plant.plant_id

        assert (
            toy_parameters.plant_fixed_cost[pid]
            == plant.fixed_activation_cost
        )

        assert (
            toy_parameters.plant_unit_treatment_cost[pid]
            == plant.treatment_cost_per_ml
        )

    for zone in toy_scenario.demand_zones:
        assert (
            toy_parameters.demand_by_zone[zone.zone_id]
            == zone.demand_ml_per_day
        )



# QUALITY PARAMETER CONSISTENCY

def test_quality_parameters_are_complete(toy_parameters):
    """Every source should contain every configured quality parameter."""

    for source_id in toy_parameters.source_ids:
        for parameter_id in toy_parameters.quality_parameter_ids:
            assert (
                source_id,
                parameter_id
            ) in toy_parameters.source_quality

    for parameter_id in toy_parameters.quality_parameter_ids:
        assert parameter_id in toy_parameters.quality_lower_bound
        assert parameter_id in toy_parameters.quality_upper_bound
        assert parameter_id in toy_parameters.quality_units


def test_quality_bounds_are_valid(toy_parameters):
    """Every quality lower bound should be <= its upper bound."""

    for parameter_id in toy_parameters.quality_parameter_ids:
        lower = toy_parameters.quality_lower_bound[parameter_id]
        upper = toy_parameters.quality_upper_bound[parameter_id]

        assert lower <= upper


# DETERMINISTIC TRANSFORMATION

def test_preprocessing_is_deterministic(toy_scenario):
    """The same scenario should always produce the same parameters."""
    first = preprocess_scenario(toy_scenario)
    second = preprocess_scenario(toy_scenario)

    assert first.source_ids == second.source_ids
    assert first.plant_ids == second.plant_ids
    assert first.zone_ids == second.zone_ids

    assert first.source_quality == second.source_quality
    assert first.demand_by_zone == second.demand_by_zone

    assert first.source_fixed_cost == second.source_fixed_cost
    assert first.plant_fixed_cost == second.plant_fixed_cost

#  pH NUMERICAL SCALING

def test_ph_transformation_is_correct(toy_scenario, toy_parameters):
    """pH should be converted to hydrogen ion concentration in nmol/L."""
    
    ph_parameter = next(
    (
        parameter_id
        for parameter_id in toy_parameters.quality_parameter_ids
        if "hydrogen" in parameter_id.lower()
    ),
    None
)

    assert ph_parameter is not None, \
    "Hydrogen ion quality parameter was not created during preprocessing"
     
    assert toy_parameters.quality_units[ph_parameter] == "nmol/L"

    for source in toy_scenario.sources:
        if "pH" in source.quality:
            expected = (10 ** (-source.quality["pH"])) * 1e9

            actual = toy_parameters.source_quality[
                (source.source_id, ph_parameter)
            ]

            assert actual == pytest.approx(expected)