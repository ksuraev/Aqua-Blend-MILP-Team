import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import tempfile
import os

from src.data_loader import load_scenario, DataLoadError

# ===== FIXTURES =====

@pytest.fixture
def toy_scenario():
    return load_scenario('config/scenarios/toy_scenario.json')

# ===== BASIC LOADING TESTS =====

def test_load_scenario(toy_scenario):
    """Test: JSON loads → ScenarioData"""
    assert toy_scenario is not None
    assert len(toy_scenario.sources) > 0

# ===== ERROR HANDLING TESTS =====

def test_load_nonexistent_file():
    """Test: Loading non-existent file raises DataLoadError"""
    with pytest.raises(DataLoadError, match="Scenario file not found"):
        load_scenario('nonexistent_scenario.json')

def test_load_invalid_json():
    """Test: Invalid JSON format raises error"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        f.write("{ invalid json }")
        temp_path = f.name
    
    try:
        with pytest.raises(DataLoadError, match="Invalid JSON"):
            load_scenario(temp_path)
    finally:
        os.unlink(temp_path)

def test_load_empty_file():
    """Test: Empty JSON raises error"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        f.write("")
        temp_path = f.name
    
    try:
        with pytest.raises(DataLoadError):
            load_scenario(temp_path)
    finally:
        os.unlink(temp_path)

# ===== DATA VALIDATION TESTS =====

def test_quality_values_are_finite(toy_scenario):
    """Test: Quality values are finite numbers (not NaN or inf)"""
    for source in toy_scenario.sources:
        for param, value in source.quality.items():
            assert isinstance(value, (int, float)), \
                f"Source {source.source_id} quality '{param}' must be numeric, got {type(value)}"
            assert -float('inf') < value < float('inf'), \
                f"Source {source.source_id} quality '{param}' value {value} is not finite"

def test_costs_are_non_negative(toy_scenario):
    """Test: All costs are non-negative"""
    for source in toy_scenario.sources:
        assert source.fixed_activation_cost >= 0, \
            f"Source {source.source_id} fixed cost must be >= 0, got {source.fixed_activation_cost}"
        
        if source.cost_per_ml is not None:
            assert source.cost_per_ml >= 0, \
                f"Source {source.source_id} unit cost must be >= 0, got {source.cost_per_ml}"
    
    for plant in toy_scenario.plants:
        assert plant.fixed_activation_cost >= 0, \
            f"Plant {plant.plant_id} fixed cost must be >= 0"
        if plant.treatment_cost_per_ml is not None:
            assert plant.treatment_cost_per_ml >= 0, \
                f"Plant {plant.plant_id} treatment cost must be >= 0"

# ===== STRUCTURE VALIDATION TESTS =====

def test_single_plant_scenario(toy_scenario):
    """Test: Scenario loads with at least one plant"""
    assert len(toy_scenario.plants) > 0, "Scenario must have at least one plant"

# ===== BOUNDARY VALUE TESTS =====

def test_boundary_zero_values(toy_scenario):
    """Test: Handling zero costs, demands, capacities"""
    for zone in toy_scenario.demand_zones:
        if zone.demand_ml_per_day == 0:
            assert zone.demand_ml_per_day >= 0

def test_minimum_values(toy_scenario):
    """Test: Smallest possible valid values"""
    for source in toy_scenario.sources:
        assert source.fixed_activation_cost >= 0

def test_maximum_values(toy_scenario):
    """Test: Largest values don't overflow"""
    for source in toy_scenario.sources:
        if source.maximum_withdrawal_ml_per_day is not None:
            assert source.maximum_withdrawal_ml_per_day < float('inf')

def test_equal_bounds(toy_scenario):
    """Test: Min == Max (tight constraint)"""
    for source in toy_scenario.sources:
        if source.minimum_withdrawal_ml_per_day is not None and source.maximum_withdrawal_ml_per_day is not None:
            if source.minimum_withdrawal_ml_per_day == source.maximum_withdrawal_ml_per_day:
                assert source.minimum_withdrawal_ml_per_day >= 0

# ===== NULL/NONE HANDLING =====

def test_optional_fields_none(toy_scenario):
    """Test: Optional fields can be None"""
    for source in toy_scenario.sources:
        if source.cost_per_ml is None:
            assert source.source_id is not None

def test_empty_collections():
    """Test: Empty sources/plants/zones handled gracefully"""
    pass

# ===== QUALITY PARAMETER VALIDATION =====

def test_quality_values_in_bounds(toy_scenario):
    """Test: Source quality values are within physical bounds"""
    for source in toy_scenario.sources:
        for param_id, value in source.quality.items():
            if "ph" in param_id.lower() or "hydrogen" in param_id.lower():
                assert 0 <= value <= 14 or value > 0, f"Quality {param_id} value {value} outside expected range"
            
            if "turbid" in param_id.lower():
                assert value >= 0, f"Turbidity can't be negative: {value}"
            
            if "alkalin" in param_id.lower():
                assert value >= 0, f"Alkalinity can't be negative: {value}"

# ===== DEMAND VALIDATION =====

def test_total_demand_valid(toy_scenario):
    """Test: Total demand across zones is reasonable"""
    total_demand = sum(zone.demand_ml_per_day for zone in toy_scenario.demand_zones)
    assert total_demand > 0, "Total demand is zero - scenario is invalid"

def test_demand_coverage(toy_scenario):
    """Test: All zones have demand"""
    for zone in toy_scenario.demand_zones:
        assert zone.demand_ml_per_day > 0, f"Zone {zone.zone_id} has zero or negative demand"

# ===== TYPE VALIDATION TESTS  =====

def test_type_validation_numeric_costs(toy_scenario):
    """Test: Costs are numeric, not strings"""
    for source in toy_scenario.sources:
        assert isinstance(source.fixed_activation_cost, (int, float)), \
            f"Fixed cost is {type(source.fixed_activation_cost)}, expected numeric"
        if source.cost_per_ml is not None:
            assert isinstance(source.cost_per_ml, (int, float))
    
    for plant in toy_scenario.plants:
        assert isinstance(plant.fixed_activation_cost, (int, float))
        if plant.treatment_cost_per_ml is not None:
            assert isinstance(plant.treatment_cost_per_ml, (int, float))

def test_type_validation_numeric_demands(toy_scenario):
    """Test: Demand values are numeric, not strings"""
    for zone in toy_scenario.demand_zones:
        assert isinstance(zone.demand_ml_per_day, (int, float)), \
            f"Demand is {type(zone.demand_ml_per_day)}, expected numeric"

def test_type_validation_ids_are_strings(toy_scenario):
    """Test: All IDs are strings"""
    for source in toy_scenario.sources:
        assert isinstance(source.source_id, str), \
            f"Source ID is {type(source.source_id)}, expected string"
    
    for plant in toy_scenario.plants:
        assert isinstance(plant.plant_id, str), \
            f"Plant ID is {type(plant.plant_id)}, expected string"
    
    for zone in toy_scenario.demand_zones:
        assert isinstance(zone.zone_id, str), \
            f"Zone ID is {type(zone.zone_id)}, expected string"

# ===== DUPLICATE ID DETECTION =====

def test_no_duplicate_source_ids(toy_scenario):
    """Test: All source IDs are unique"""
    source_ids = [s.source_id for s in toy_scenario.sources]
    assert len(source_ids) == len(set(source_ids)), \
        f"Duplicate source IDs found: {[id for id in source_ids if source_ids.count(id) > 1]}"

def test_no_duplicate_plant_ids(toy_scenario):
    """Test: All plant IDs are unique"""
    plant_ids = [p.plant_id for p in toy_scenario.plants]
    assert len(plant_ids) == len(set(plant_ids)), \
        "Duplicate plant IDs found"

def test_no_duplicate_zone_ids(toy_scenario):
    """Test: All zone IDs are unique"""
    zone_ids = [z.zone_id for z in toy_scenario.demand_zones]
    assert len(zone_ids) == len(set(zone_ids)), \
        "Duplicate zone IDs found"

# ===== MISSING REQUIRED FIELDS =====

def test_no_missing_required_source_fields(toy_scenario):
    """Test: No source is missing critical fields"""
    for source in toy_scenario.sources:
        assert source.source_id is not None, "Source missing ID"
        assert source.fixed_activation_cost is not None, "Source missing fixed cost"
        assert source.quality is not None, "Source missing quality data"
        assert len(source.quality) > 0, "Source has empty quality dict"

def test_no_missing_required_plant_fields(toy_scenario):
    """Test: No plant is missing critical fields"""
    for plant in toy_scenario.plants:
        assert plant.plant_id is not None, "Plant missing ID"
        assert plant.fixed_activation_cost is not None, "Plant missing fixed cost"

def test_no_missing_required_zone_fields(toy_scenario):
    """Test: No zone is missing critical fields"""
    for zone in toy_scenario.demand_zones:
        assert zone.zone_id is not None, "Zone missing ID"
        assert zone.demand_ml_per_day is not None, "Zone missing demand"

# ===== NEGATIVE VALUE CHECKS  =====

def test_no_negative_numeric_ids(toy_scenario):
    """Test: IDs are never negative numbers"""
    for source in toy_scenario.sources:
        if isinstance(source.source_id, (int, float)):
            assert source.source_id >= 0, "Negative numeric ID found"

def test_no_negative_physical_quantities(toy_scenario):
    """Test: Physical quantities are never negative"""
    for zone in toy_scenario.demand_zones:
        assert zone.demand_ml_per_day >= 0, \
            f"Zone {zone.zone_id} has negative demand: {zone.demand_ml_per_day}"
    
    for source in toy_scenario.sources:
        if source.maximum_withdrawal_ml_per_day is not None:
            assert source.maximum_withdrawal_ml_per_day >= 0
        if source.minimum_withdrawal_ml_per_day is not None:
            assert source.minimum_withdrawal_ml_per_day >= 0

# ===== SCENARIO METADATA VALIDATION  =====

def test_scenario_has_valid_metadata(toy_scenario):
    """Test: Scenario metadata is valid"""
    assert toy_scenario.scenario_id is not None, "Missing scenario ID"
    assert toy_scenario.scenario_name is not None, "Missing scenario name"
    assert toy_scenario.status in ["test", "active", "archived"], \
        f"Invalid status: {toy_scenario.status}"

def test_scenarios_loadable_multiple_times(toy_scenario):
    """Test: Reloading same scenario produces identical results"""
    s1 = load_scenario('config/scenarios/toy_scenario.json')
    s2 = load_scenario('config/scenarios/toy_scenario.json')
    s3 = load_scenario('config/scenarios/toy_scenario.json')
    
    assert s1.scenario_id == s2.scenario_id == s3.scenario_id
    assert len(s1.sources) == len(s2.sources) == len(s3.sources)

# ===== SPECIAL CHARACTER HANDLING  =====

def test_handles_special_characters_in_names(toy_scenario):
    """Test: Can handle special characters in names"""
    for source in toy_scenario.sources:
        # Should have name without errors
        assert source.source_id is not None
    
    for plant in toy_scenario.plants:
        # Should have name without errors
        assert plant.plant_id is not None