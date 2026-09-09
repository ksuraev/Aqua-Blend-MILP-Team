import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import copy
import json
import os
import tempfile

import pytest

from src.data_loader import load_scenario, DataLoadError


def valid_scenario_data():
    """Return a small valid inline scenario for testing."""
    return {
        "scenario_id": "test_scenario",
        "scenario_name": "Test Scenario",
        "status": "test",

        "data_source": {
            "type": "inline",
            "allow_estimated_values": False,
            "source_rows": [
                {
                    "source_id": "S1",
                    "source_name": "Test Source",
                    "source_type": "surface_water",
                    "minimum_withdrawal_ml_per_day": 0,
                    "max_available_ml_per_day": 100,
                    "cost_per_ml": 1.0,
                    "representative_turbidity_ntu": 1.0,
                    "is_active": True,
                    "model_ready": True
                }
            ]
        },

        "validation": {
            "fail_if_source_missing_from_database": True,
            "fail_if_daily_availability_missing": True,
            "fail_if_required_quality_value_missing": True,
            "fail_if_demand_missing": True
        },

        "quality_limits": {
            "parameters": {
                "turbidity": {
                    "min": 0,
                    "max": 5,
                    "unit": "NTU"
                }
            }
        },

        "sources": [
            {
                "source_id": "S1",
                "enabled": True,
                "fixed_activation_cost": 10.0
            }
        ],

        "network": {
            "plants": [
                {
                    "plant_id": "P1",
                    "name": "Test Plant",
                    "enabled": True,
                    "minimum_processing_capacity_ml_per_day": 0,
                    "maximum_processing_capacity_ml_per_day": 100,
                    "fixed_activation_cost": 5.0,
                    "treatment_cost_per_ml": 0.5
                }
            ],

            "demand_zones": [
                {
                    "zone_id": "Z1",
                    "name": "Test Zone",
                    "demand_ml_per_day": 30
                }
            ],

            "source_to_plant_links": [
                {
                    "source_id": "S1",
                    "plant_id": "P1",
                    "enabled": True,
                    "maximum_flow_ml_per_day": 100
                }
            ],

            "plant_to_zone_links": [
                {
                    "plant_id": "P1",
                    "zone_id": "Z1",
                    "enabled": True,
                    "maximum_flow_ml_per_day": 100
                }
            ]
        }
    }


def write_temp_scenario(data):
    """Write scenario data to a temporary JSON file."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        delete=False,
        encoding="utf-8"
    ) as file:
        json.dump(data, file)
        return file.name


@pytest.fixture
def loaded_scenario():
    """Load one valid scenario and clean up the temporary file."""
    path = write_temp_scenario(valid_scenario_data())

    try:
        yield load_scenario(path)
    finally:
        os.unlink(path)

# SUCCESSFUL LOADING


def test_valid_scenario_loads(loaded_scenario):
    """A valid scenario should load into the expected structure."""
    assert loaded_scenario.scenario_id == "test_scenario"
    assert len(loaded_scenario.sources) == 1
    assert len(loaded_scenario.plants) == 1
    assert len(loaded_scenario.demand_zones) == 1


# FILE ERROR HANDLING

def test_nonexistent_file_rejected():
    """A missing scenario file should raise DataLoadError."""
    with pytest.raises(DataLoadError, match="Scenario file not found"):
        load_scenario("file_that_does_not_exist.json")


def test_invalid_json_rejected():
    """Malformed JSON should raise DataLoadError."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        delete=False
    ) as file:
        file.write("{ invalid json }")
        path = file.name

    try:
        with pytest.raises(DataLoadError, match="Invalid JSON"):
            load_scenario(path)
    finally:
        os.unlink(path)



# REQUIRED FIELD VALIDATION

def test_missing_scenario_id_rejected():
    """scenario_id is required."""
    data = valid_scenario_data()
    data.pop("scenario_id")

    path = write_temp_scenario(data)

    try:
        with pytest.raises(DataLoadError, match="required"):
            load_scenario(path)
    finally:
        os.unlink(path)

# DATA SOURCE VALIDATION


def test_invalid_data_source_type_rejected():
    """Only inline and supabase data sources are supported."""
    data = valid_scenario_data()
    data["data_source"]["type"] = "csv"

    path = write_temp_scenario(data)

    try:
        with pytest.raises(
            DataLoadError,
            match='must be either "supabase" or "inline"'
        ):
            load_scenario(path)
    finally:
        os.unlink(path)

# NUMERIC VALIDATION

def test_negative_source_cost_rejected():
    """Negative source activation cost should fail validation."""
    data = valid_scenario_data()
    data["sources"][0]["fixed_activation_cost"] = -10

    path = write_temp_scenario(data)

    try:
        with pytest.raises(
            DataLoadError,
            match="fixed activation cost"
        ):
            load_scenario(path)
    finally:
        os.unlink(path)


def test_negative_demand_rejected():
    """Negative demand should fail validation."""
    data = valid_scenario_data()
    data["network"]["demand_zones"][0]["demand_ml_per_day"] = -30

    path = write_temp_scenario(data)

    try:
        with pytest.raises(
            DataLoadError,
            match="demand"
        ):
            load_scenario(path)
    finally:
        os.unlink(path)


# DUPLICATE DATA VALIDATION

def test_duplicate_source_ids_rejected():
    """Enabled source IDs must be unique."""
    data = valid_scenario_data()

    duplicate = copy.deepcopy(data["sources"][0])
    data["sources"].append(duplicate)

    path = write_temp_scenario(data)

    try:
        with pytest.raises(
            DataLoadError,
            match="duplicate enabled source IDs"
        ):
            load_scenario(path)
    finally:
        os.unlink(path)


#  QUALITY CONFIGURATION VALIDATION

def test_quality_min_greater_than_max_rejected():
    """Quality minimum cannot be greater than maximum."""
    data = valid_scenario_data()

    quality = data["quality_limits"]["parameters"]["turbidity"]
    quality["min"] = 10
    quality["max"] = 5

    path = write_temp_scenario(data)

    try:
        with pytest.raises(
            DataLoadError,
            match="min greater than max"
        ):
            load_scenario(path)
    finally:
        os.unlink(path)


def test_unsupported_quality_transform_rejected():
    """Unsupported quality transformations should be rejected."""
    data = valid_scenario_data()

    data["quality_limits"]["parameters"]["turbidity"][
        "transform"
    ] = "unsupported_transform"

    path = write_temp_scenario(data)

    try:
        with pytest.raises(
            DataLoadError,
            match="Unsupported quality transform"
        ):
            load_scenario(path)
    finally:
        os.unlink(path)


def test_missing_required_quality_value_rejected():
    """Required source quality data must be present."""
    data = valid_scenario_data()

    data["data_source"]["source_rows"][0].pop(
        "representative_turbidity_ntu"
    )

    path = write_temp_scenario(data)

    try:
        with pytest.raises(
            DataLoadError,
            match="missing quality parameter"
        ):
            load_scenario(path)
    finally:
        os.unlink(path)


#  NETWORK CONSISTENCY

def test_unknown_network_reference_rejected():
    """Network links must reference existing plants."""
    data = valid_scenario_data()

    data["network"]["source_to_plant_links"][0][
        "plant_id"
    ] = "UNKNOWN_PLANT"

    path = write_temp_scenario(data)

    try:
        with pytest.raises(
            DataLoadError,
            match="unknown or invalid plant"
        ):
            load_scenario(path)
    finally:
        os.unlink(path)


# STRICT MODE BEHAVIOUR

def test_strict_false_returns_validation_issues():
    """strict=False should return validation issues instead of raising."""
    data = valid_scenario_data()

    data["network"]["demand_zones"][0][
        "demand_ml_per_day"
    ] = -5

    path = write_temp_scenario(data)

    try:
        scenario = load_scenario(path, strict=False)

        assert scenario.validation_issues

        assert any(
            "demand" in issue.lower()
            for issue in scenario.validation_issues
        )
    finally:
        os.unlink(path)



# VALUE PRESERVATION

def test_loaded_values_are_preserved(loaded_scenario):
    """Important values should be preserved during loading."""
    source = loaded_scenario.sources[0]
    plant = loaded_scenario.plants[0]
    zone = loaded_scenario.demand_zones[0]

    assert source.source_id == "S1"
    assert source.fixed_activation_cost == 10.0
    assert source.cost_per_ml == 1.0
    assert source.quality["turbidity"] == 1.0

    assert plant.plant_id == "P1"
    assert plant.fixed_activation_cost == 5.0

    assert zone.zone_id == "Z1"
    assert zone.demand_ml_per_day == 30.0
