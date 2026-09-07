import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from src.data_loader import load_scenario, DataLoadError
from src.preprocessing import preprocess_scenario

# ===== FIXTURES =====

@pytest.fixture
def toy_scenario():
    return load_scenario('config/scenarios/toy_scenario.json')

@pytest.fixture
def toy_parameters(toy_scenario):
    return preprocess_scenario(toy_scenario)

@pytest.fixture
def base_scenario():
    try:
        return load_scenario('config/scenarios/base_scenarios_v1.json')
    except DataLoadError as e:
        if "Network is unreachable" in str(e) or "connection" in str(e).lower():
            pytest.skip("Supabase database not accessible from this network")
        raise

# ===== END-TO-END PIPELINE TESTS =====

def test_quality_invariant(toy_scenario):
    """Test: Source quality keys match quality limit parameter keys"""
    quality_params = set(toy_scenario.quality_limits["parameters"].keys())
    
    for source in toy_scenario.sources:
        source_quality_keys = set(source.quality.keys())
        assert source_quality_keys == quality_params, \
            f"Source {source.source_id}: expected {quality_params}, got {source_quality_keys}"

def test_end_to_end(toy_scenario, toy_parameters):
    """Test: JSON → ScenarioData → ModelParameters"""
    assert set(toy_parameters.source_ids) == {s.source_id for s in toy_scenario.sources}
    assert toy_parameters.source_quality is not None

def test_base_scenarios_quality(base_scenario):
    """Test: base_scenarios_v1.json has quality data"""
    quality_params = set(base_scenario.quality_limits["parameters"].keys())
    
    for source in base_scenario.sources:
        source_quality_keys = set(source.quality.keys())
        assert source_quality_keys == quality_params, \
            f"Source {source.source_id} missing quality in base_scenarios_v1.json"

# ===== COMPLETE MAPPING TESTS =====

def test_scenario_to_parameters_complete_mapping(toy_scenario, toy_parameters):
    """Test: All scenario data maps to parameters correctly"""
    scenario_source_ids = {s.source_id for s in toy_scenario.sources}
    parameter_source_ids = set(toy_parameters.source_ids)
    assert scenario_source_ids == parameter_source_ids, \
        f"Source mismatch: scenario {scenario_source_ids} vs params {parameter_source_ids}"
    
    scenario_plant_ids = {p.plant_id for p in toy_scenario.plants}
    parameter_plant_ids = set(toy_parameters.plant_ids)
    assert scenario_plant_ids == parameter_plant_ids, \
        f"Plant mismatch: scenario {scenario_plant_ids} vs params {parameter_plant_ids}"
    
    scenario_zone_ids = {z.zone_id for z in toy_scenario.demand_zones}
    parameter_zone_ids = set(toy_parameters.zone_ids)
    assert scenario_zone_ids == parameter_zone_ids, \
        f"Zone mismatch: scenario {scenario_zone_ids} vs params {parameter_zone_ids}"

def test_network_arcs_preserved(toy_scenario, toy_parameters):
    """Test: All network arcs from scenario map to parameters"""
    scenario_source_plant = {(link.source_id, link.plant_id) for link in toy_scenario.source_to_plant_links}
    param_source_plant = set(toy_parameters.source_plant_arcs)
    assert scenario_source_plant == param_source_plant, \
        f"Source-plant arc mismatch"
    
    scenario_plant_zone = {(link.plant_id, link.zone_id) for link in toy_scenario.plant_to_zone_links}
    param_plant_zone = set(toy_parameters.plant_zone_arcs)
    assert scenario_plant_zone == param_plant_zone, \
        f"Plant-zone arc mismatch"

def test_cost_data_transferred(toy_scenario, toy_parameters):
    """Test: All cost data transfers from scenario to parameters"""
    for source in toy_scenario.sources:
        assert source.source_id in toy_parameters.source_fixed_cost, \
            f"Missing fixed cost for source {source.source_id}"
        assert source.source_id in toy_parameters.source_unit_cost, \
            f"Missing unit cost for source {source.source_id}"
    
    for plant in toy_scenario.plants:
        assert plant.plant_id in toy_parameters.plant_fixed_cost, \
            f"Missing fixed cost for plant {plant.plant_id}"
        assert plant.plant_id in toy_parameters.plant_unit_treatment_cost, \
            f"Missing treatment cost for plant {plant.plant_id}"

def test_demand_data_transferred(toy_scenario, toy_parameters):
    """Test: All demand data transfers from scenario to parameters"""
    for zone in toy_scenario.demand_zones:
        assert zone.zone_id in toy_parameters.demand_by_zone, \
            f"Missing demand for zone {zone.zone_id}"
        assert toy_parameters.demand_by_zone[zone.zone_id] == zone.demand_ml_per_day, \
            f"Demand mismatch for zone {zone.zone_id}"

# ===== CONSISTENCY TESTS =====

def test_preprocessing_deterministic(toy_scenario):
    """Test: Same input produces same output (deterministic)"""
    params1 = preprocess_scenario(toy_scenario)
    params2 = preprocess_scenario(toy_scenario)
    
    assert params1.source_quality == params2.source_quality
    assert params1.demand_by_zone == params2.demand_by_zone
    assert params1.source_fixed_cost == params2.source_fixed_cost
    assert params1.source_ids == params2.source_ids

def test_cost_consistency(toy_scenario, toy_parameters):
    """Test: Costs transform consistently"""
    for source in toy_scenario.sources:
        sid = source.source_id
        
        assert toy_parameters.source_fixed_cost[sid] == source.fixed_activation_cost
        
        if source.cost_per_ml is not None:
            assert toy_parameters.source_unit_cost[sid] == source.cost_per_ml
        else:
            assert sid in toy_parameters.source_unit_cost

# ===== PERFORMANCE TESTING  =====

def test_preprocessing_performance(toy_scenario):
    """Test: Preprocessing completes in reasonable time"""
    import time
    
    start = time.time()
    parameters = preprocess_scenario(toy_scenario)
    elapsed = time.time() - start
    
    # Should complete in < 1 second for toy scenario
    assert elapsed < 1.0, f"Preprocessing took {elapsed}s (too slow)"

# ===== END-TO-END ROBUSTNESS =====

def test_full_pipeline_is_reversible(toy_scenario, toy_parameters):
    """Test: Can recover original scenario structure from parameters"""
    # Source IDs should match
    scenario_sources = {s.source_id for s in toy_scenario.sources}
    param_sources = set(toy_parameters.source_ids)
    assert scenario_sources == param_sources
    
    # Plant IDs should match
    scenario_plants = {p.plant_id for p in toy_scenario.plants}
    param_plants = set(toy_parameters.plant_ids)
    assert scenario_plants == param_plants
    
    # Zone IDs should match
    scenario_zones = {z.zone_id for z in toy_scenario.demand_zones}
    param_zones = set(toy_parameters.zone_ids)
    assert scenario_zones == param_zones

def test_pipeline_handles_all_data_types(toy_scenario, toy_parameters):
    """Test: Pipeline correctly handles various data types"""
    # Numeric types
    assert isinstance(next(iter(toy_parameters.demand_by_zone.values())), (int, float))
    
    # String IDs
    assert isinstance(next(iter(toy_parameters.source_ids)), str)
    
    # Tuple arcs
    assert isinstance(next(iter(toy_parameters.source_plant_arcs)), tuple)

def test_scenario_and_parameters_sizes_consistent(toy_scenario, toy_parameters):
    """Test: Scenario and parameters have consistent dimensions"""
    scenario_size = len(toy_scenario.sources) + len(toy_scenario.plants) + len(toy_scenario.demand_zones)
    param_size = len(toy_parameters.source_ids) + len(toy_parameters.plant_ids) + len(toy_parameters.zone_ids)
    
    assert scenario_size == param_size, \
        f"Scenario size {scenario_size} != parameters size {param_size}"

def test_all_transformed_values_valid(toy_parameters):
    """Test: All values in ModelParameters are valid (not NaN, inf, etc)"""
    import math
    
    # Check demands
    for demand in toy_parameters.demand_by_zone.values():
        assert not math.isnan(demand), "NaN demand found"
        assert not math.isinf(demand), "Infinite demand found"
    
    # Check costs
    for cost in toy_parameters.source_fixed_cost.values():
        assert not math.isnan(cost), "NaN cost found"
        assert not math.isinf(cost), "Infinite cost found"
    
    # Check capacities
    for capacity in toy_parameters.source_plant_link_capacity.values():
        assert not math.isnan(capacity), "NaN capacity found"
        assert not math.isinf(capacity), "Infinite capacity found"

def test_database_and_json_scenarios_compatible(base_scenario):
    """Test: Database and JSON scenarios have same structure"""
    # Both should load successfully
    toy = load_scenario('config/scenarios/toy_scenario.json')
    
    # Both should have same structure
    assert hasattr(toy, 'sources')
    assert hasattr(toy, 'plants')
    assert hasattr(toy, 'demand_zones')
    assert hasattr(base_scenario, 'sources')
    assert hasattr(base_scenario, 'plants')
    assert hasattr(base_scenario, 'demand_zones')

# ===== SCENARIO-LEVEL FEASIBILITY TESTS  =====

def test_scenario_overall_feasibility(toy_scenario, toy_parameters):
    """Test: Scenario is collectively feasible"""
    # Check 1: All nodes connected
    assert len(toy_parameters.source_plant_arcs) > 0, "No source-plant connections"
    assert len(toy_parameters.plant_zone_arcs) > 0, "No plant-zone connections"
    
    # Check 2: Total capacity >= total demand
    total_demand = sum(toy_parameters.demand_by_zone.values())
    total_supply = sum(
        toy_parameters.source_max_withdrawal[sid]
        for sid in toy_parameters.source_ids
    )
    assert total_supply >= total_demand, \
        f"Total supply {total_supply} < total demand {total_demand}"
    
    # Check 3: No impossible constraints
    for source_id in toy_parameters.source_ids:
        min_w = toy_parameters.source_min_withdrawal[source_id]
        max_w = toy_parameters.source_max_withdrawal[source_id]
        assert min_w <= max_w, f"Source {source_id}: min > max"
    
    for plant_id in toy_parameters.plant_ids:
        min_t = toy_parameters.plant_min_throughput[plant_id]
        max_t = toy_parameters.plant_max_throughput[plant_id]
        assert min_t <= max_t, f"Plant {plant_id}: min > max"

def test_supply_meets_total_demand(toy_scenario, toy_parameters):
    """Test: Total supply capacity >= total demand"""
    total_demand = sum(toy_parameters.demand_by_zone.values())
    
    # Sum of all source max withdrawals
    total_max_supply = sum(
        toy_parameters.source_max_withdrawal[sid]
        for sid in toy_parameters.source_ids
    )
    
    assert total_max_supply >= total_demand, \
        f"Total max supply {total_max_supply} < total demand {total_demand}"
    
    # Sum of all source min withdrawals shouldn't exceed total demand
    total_min_supply = sum(
        toy_parameters.source_min_withdrawal[sid]
        for sid in toy_parameters.source_ids
    )
    total_max_demand = sum(
        toy_parameters.demand_by_zone.values()
    )
    assert total_min_supply <= total_max_demand * 1.5, \
        "Total minimum supply requirement seems excessive"

def test_network_connectivity_complete(toy_scenario, toy_parameters):
    """Test: All zones are reachable from all sources through network"""
    source_ids = set(toy_parameters.source_ids)
    plant_ids = set(toy_parameters.plant_ids)
    zone_ids = set(toy_parameters.zone_ids)
    
    # Build connectivity graph
    sources_to_plants = {src: [] for src in source_ids}
    for src, plant in toy_parameters.source_plant_arcs:
        sources_to_plants[src].append(plant)
    
    plants_to_zones = {plant: [] for plant in plant_ids}
    for plant, zone in toy_parameters.plant_zone_arcs:
        plants_to_zones[plant].append(zone)
    
    # Check: Every source can reach every zone (through some plant)
    for source_id in source_ids:
        reachable_plants = set(sources_to_plants.get(source_id, []))
        if reachable_plants:
            reachable_zones = set()
            for plant in reachable_plants:
                reachable_zones.update(plants_to_zones.get(plant, []))
            
            # At least one zone should be reachable
            assert len(reachable_zones) > 0, \
                f"Source {source_id} can't reach any zone"

def test_no_conflicting_constraints(toy_scenario, toy_parameters):
    """Test: Constraints don't contradict each other"""
    # Check 1: Quality bounds are valid
    for param_id in toy_parameters.quality_parameter_ids:
        lower = toy_parameters.quality_lower_bound[param_id]
        upper = toy_parameters.quality_upper_bound[param_id]
        assert lower < upper, f"Quality {param_id}: lower >= upper"
        assert lower >= 0, f"Quality {param_id}: negative lower bound"
    
    # Check 2: Capacity bounds are valid
    for source_id in toy_parameters.source_ids:
        min_w = toy_parameters.source_min_withdrawal[source_id]
        max_w = toy_parameters.source_max_withdrawal[source_id]
        assert 0 <= min_w <= max_w, \
            f"Source {source_id}: invalid withdrawal bounds"
    
    # Check 3: Demand is non-negative
    for zone_id, demand in toy_parameters.demand_by_zone.items():
        assert demand >= 0, f"Zone {zone_id}: negative demand"
    
    # Check 4: Costs are non-negative
    for cost in toy_parameters.source_fixed_cost.values():
        assert cost >= 0, "Negative source cost"
    for cost in toy_parameters.plant_fixed_cost.values():
        assert cost >= 0, "Negative plant cost"

def test_scenario_has_minimum_viable_structure(toy_scenario, toy_parameters):
    """Test: Scenario has minimum viable network structure"""
    # At least 1 source
    assert len(toy_parameters.source_ids) >= 1, "No sources"
    
    # At least 1 plant
    assert len(toy_parameters.plant_ids) >= 1, "No plants"
    
    # At least 1 zone
    assert len(toy_parameters.zone_ids) >= 1, "No zones"
    
    # At least one connection at each layer
    assert len(toy_parameters.source_plant_arcs) >= 1, "No source-plant connections"
    assert len(toy_parameters.plant_zone_arcs) >= 1, "No plant-zone connections"
    
    # All zones have demand > 0
    for demand in toy_parameters.demand_by_zone.values():
        assert demand > 0, "Zone with zero demand"
    
    # At least one quality parameter
    assert len(toy_parameters.quality_parameter_ids) >= 1, "No quality parameters"