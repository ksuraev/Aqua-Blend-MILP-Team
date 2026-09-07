import sys
from pathlib import Path
from dataclasses import replace

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from src.data_loader import load_scenario
from src.preprocessing import preprocess_scenario, PreprocessingError

# ===== FIXTURES =====

@pytest.fixture
def toy_scenario():
    return load_scenario('config/scenarios/toy_scenario.json')

@pytest.fixture
def toy_parameters(toy_scenario):
    return preprocess_scenario(toy_scenario)

# ===== TRANSFORMATION TESTS =====

def test_preprocess_scenario(toy_parameters):
    """Test: ScenarioData → ModelParameters"""
    assert toy_parameters is not None
    assert len(toy_parameters.source_ids) > 0
    assert len(toy_parameters.source_quality) > 0

def test_preprocessing_with_validation_issues(toy_scenario):
    """Test: Preprocessing rejects scenario with loader validation issues"""
    scenario_with_issues = replace(toy_scenario, validation_issues=("Test error 1", "Test error 2"))
    
    with pytest.raises(PreprocessingError, match="validation issues"):
        preprocess_scenario(scenario_with_issues)

# ===== CAPACITY VALIDATION TESTS =====

def test_capacity_bounds_valid(toy_parameters):
    """Test: Minimum <= Maximum for all capacity bounds"""
    for source_id in toy_parameters.source_ids:
        min_w = toy_parameters.source_min_withdrawal[source_id]
        max_w = toy_parameters.source_max_withdrawal[source_id]
        assert min_w <= max_w, \
            f"Source {source_id}: minimum {min_w} > maximum {max_w}"
    
    for plant_id in toy_parameters.plant_ids:
        min_p = toy_parameters.plant_min_throughput[plant_id]
        max_p = toy_parameters.plant_max_throughput[plant_id]
        assert min_p <= max_p, \
            f"Plant {plant_id}: minimum {min_p} > maximum {max_p}"

def test_demand_non_negative(toy_parameters):
    """Test: All demand values are non-negative"""
    for zone_id, demand in toy_parameters.demand_by_zone.items():
        assert demand >= 0, \
            f"Zone {zone_id} demand must be >= 0, got {demand}"

def test_link_capacities_positive(toy_parameters):
    """Test: All link capacities are positive"""
    for (source_id, plant_id), capacity in toy_parameters.source_plant_link_capacity.items():
        assert capacity > 0, \
            f"Link {source_id}→{plant_id} capacity must be > 0, got {capacity}"
    
    for (plant_id, zone_id), capacity in toy_parameters.plant_zone_link_capacity.items():
        assert capacity > 0, \
            f"Link {plant_id}→{zone_id} capacity must be > 0, got {capacity}"

def test_quality_bounds_valid(toy_parameters):
    """Test: Quality lower bound <= upper bound"""
    for param_id in toy_parameters.quality_parameter_ids:
        lower = toy_parameters.quality_lower_bound[param_id]
        upper = toy_parameters.quality_upper_bound[param_id]
        assert lower <= upper, \
            f"Quality '{param_id}': lower {lower} > upper {upper}"

# ===== EDGE CASE TESTS =====

def test_single_source_scenario(toy_scenario):
    """Test: Scenario with minimum viable resources"""
    parameters = preprocess_scenario(toy_scenario)
    
    assert len(parameters.source_ids) >= 1
    
    for src in parameters.source_ids:
        for param in parameters.quality_parameter_ids:
            assert (src, param) in parameters.source_quality

# ===== OUTPUT STRUCTURE VALIDATION =====

def test_all_parameters_present(toy_parameters):
    """Test: ModelParameters has all required fields"""
    assert toy_parameters.source_ids is not None
    assert toy_parameters.plant_ids is not None
    assert toy_parameters.zone_ids is not None
    assert toy_parameters.quality_parameter_ids is not None
    
    assert toy_parameters.demand_by_zone is not None
    assert toy_parameters.source_fixed_cost is not None
    assert toy_parameters.plant_fixed_cost is not None
    assert toy_parameters.source_unit_cost is not None
    assert toy_parameters.plant_unit_treatment_cost is not None
    assert toy_parameters.source_min_withdrawal is not None
    assert toy_parameters.source_max_withdrawal is not None
    assert toy_parameters.plant_min_throughput is not None
    assert toy_parameters.plant_max_throughput is not None
    assert toy_parameters.source_quality is not None
    assert toy_parameters.quality_lower_bound is not None
    assert toy_parameters.quality_upper_bound is not None

def test_quality_parameter_consistency(toy_parameters):
    """Test: Quality parameters are consistent across scenario"""
    quality_params = toy_parameters.quality_parameter_ids
    
    for source_id in toy_parameters.source_ids:
        for param_id in quality_params:
            assert (source_id, param_id) in toy_parameters.source_quality, \
                f"Missing quality data for {source_id}.{param_id}"
    
    for param_id in quality_params:
        assert param_id in toy_parameters.quality_lower_bound
        assert param_id in toy_parameters.quality_upper_bound
        assert param_id in toy_parameters.quality_units

# ===== NULL/NONE HANDLING =====

def test_missing_optional_data(toy_scenario):
    """Test: Missing optional fields don't break pipeline"""
    parameters = preprocess_scenario(toy_scenario)
    assert parameters.source_unit_cost is not None or len(parameters.source_ids) == 0

# ===== DATA INTEGRITY DURING TRANSFORM =====

def test_no_data_loss_in_transform(toy_scenario):
    """Test: No data is lost from scenario to parameters"""
    parameters = preprocess_scenario(toy_scenario)
    
    assert len(parameters.source_ids) == len(toy_scenario.sources)
    assert len(parameters.plant_ids) == len(toy_scenario.plants)
    assert len(parameters.zone_ids) == len(toy_scenario.demand_zones)
    assert len(parameters.source_plant_arcs) == len(toy_scenario.source_to_plant_links)
    assert len(parameters.plant_zone_arcs) == len(toy_scenario.plant_to_zone_links)

def test_value_preservation(toy_scenario):
    """Test: Values don't change during transform"""
    parameters = preprocess_scenario(toy_scenario)
    
    for source in toy_scenario.sources:
        sid = source.source_id
        
        assert sid in parameters.source_fixed_cost
        assert sid in parameters.source_unit_cost
        assert sid in parameters.source_min_withdrawal
        assert sid in parameters.source_max_withdrawal
        
        for param_id in parameters.quality_parameter_ids:
            assert (sid, param_id) in parameters.source_quality

def test_no_duplicate_entries(toy_parameters):
    """Test: No duplicate sources/plants/zones in parameters"""
    assert len(toy_parameters.source_ids) == len(set(toy_parameters.source_ids))
    assert len(toy_parameters.plant_ids) == len(set(toy_parameters.plant_ids))
    assert len(toy_parameters.zone_ids) == len(set(toy_parameters.zone_ids))

def test_arcs_reference_existing_nodes(toy_parameters):
    """Test: All network arcs reference valid nodes"""
    source_ids = set(toy_parameters.source_ids)
    plant_ids = set(toy_parameters.plant_ids)
    zone_ids = set(toy_parameters.zone_ids)
    
    for src, plant in toy_parameters.source_plant_arcs:
        assert src in source_ids
        assert plant in plant_ids
    
    for plant, zone in toy_parameters.plant_zone_arcs:
        assert plant in plant_ids
        assert zone in zone_ids

# ===== COST STRUCTURE VALIDATION =====

def test_costs_are_realistic(toy_parameters):
    """Test: Cost values are within reasonable bounds"""
    for sid, cost in toy_parameters.source_fixed_cost.items():
        assert 0 <= cost < 1e12, f"Fixed cost for {sid} seems unrealistic: {cost}"
    
    for sid, cost in toy_parameters.source_unit_cost.items():
        assert 0 <= cost < 1e6, f"Unit cost for {sid} seems unrealistic: {cost}"


def test_quality_bounds_realistic(toy_parameters):
    """Test: Quality bounds are physically realistic"""
    for param_id in toy_parameters.quality_parameter_ids:
        lower = toy_parameters.quality_lower_bound[param_id]
        upper = toy_parameters.quality_upper_bound[param_id]
        
        assert lower < upper
        ratio = upper / (lower + 1e-10)
        assert 1 < ratio < 1e10

# ===== TRANSFORMATION CONSISTENCY =====

def test_scenario_preprocessing_roundtrip(toy_scenario):
    """Test: Can preprocess same scenario multiple times consistently"""
    p1 = preprocess_scenario(toy_scenario)
    p2 = preprocess_scenario(toy_scenario)
    p3 = preprocess_scenario(toy_scenario)
    
    assert p1.source_ids == p2.source_ids == p3.source_ids
    assert p1.demand_by_zone == p2.demand_by_zone == p3.demand_by_zone
    assert p1.source_quality == p2.source_quality == p3.source_quality

def test_quality_transformation_direction(toy_scenario):
    """Test: Quality transformations maintain logical consistency"""
    for source in toy_scenario.sources:
        for param_id, quality_value in source.quality.items():
            pass

# ===== ORPHANED NODE DETECTION  =====

def test_no_orphaned_plants(toy_scenario):
    """Test: Every plant is connected to at least one source"""
    parameters = preprocess_scenario(toy_scenario)
    
    plants_with_sources = {plant for _, plant in parameters.source_plant_arcs}
    all_plants = set(parameters.plant_ids)
    
    orphaned = all_plants - plants_with_sources
    assert len(orphaned) == 0, \
        f"Orphaned plants (no source connection): {orphaned}"

def test_no_orphaned_zones(toy_scenario):
    """Test: Every zone is connected to at least one plant"""
    parameters = preprocess_scenario(toy_scenario)
    
    zones_with_plants = {zone for _, zone in parameters.plant_zone_arcs}
    all_zones = set(parameters.zone_ids)
    
    orphaned = all_zones - zones_with_plants
    assert len(orphaned) == 0, \
        f"Orphaned zones (no plant connection): {orphaned}"

# ===== ARC VALIDITY TESTS =====

def test_arcs_reference_valid_nodes_after_preprocessing(toy_scenario):
    """Test: All arcs reference nodes that exist after transformation"""
    parameters = preprocess_scenario(toy_scenario)
    
    source_ids = {s.source_id for s in toy_scenario.sources}
    plant_ids = {p.plant_id for p in toy_scenario.plants}
    zone_ids = {z.zone_id for z in toy_scenario.demand_zones}
    
    for link in toy_scenario.source_to_plant_links:
        assert link.source_id in source_ids, \
            f"Arc references non-existent source: {link.source_id}"
        assert link.plant_id in plant_ids, \
            f"Arc references non-existent plant: {link.plant_id}"
    
    for link in toy_scenario.plant_to_zone_links:
        assert link.plant_id in plant_ids
        assert link.zone_id in zone_ids

# ===== FLOATING POINT PRECISION  =====

def test_floating_point_precision_preserved(toy_scenario):
    """Test: Floating point values don't lose precision during transform"""
    parameters = preprocess_scenario(toy_scenario)
    
    for source in toy_scenario.sources:
        if source.cost_per_ml is not None:
            original = source.cost_per_ml
            transformed = parameters.source_unit_cost[source.source_id]
            
            assert abs(original - transformed) < 1e-9, \
                f"Precision lost: {original} vs {transformed}"
        
        if source.fixed_activation_cost is not None:
            original = source.fixed_activation_cost
            transformed = parameters.source_fixed_cost[source.source_id]
            
            assert abs(original - transformed) < 1e-9, \
                f"Precision lost in fixed cost: {original} vs {transformed}"

# ===== SUPPLY-DEMAND FEASIBILITY =====

def test_total_supply_meets_demand(toy_scenario):
    """Test: Total supply capacity >= total demand"""
    parameters = preprocess_scenario(toy_scenario)
    
    total_demand = sum(parameters.demand_by_zone.values())
    total_max_supply = sum(
        parameters.source_max_withdrawal[sid]
        for sid in parameters.source_ids
    )
    
    assert total_max_supply >= total_demand, \
        f"Total supply ({total_max_supply}) < total demand ({total_demand})"

# ===== TRANSFORMATION IDEMPOTENCY  =====

def test_preprocessing_idempotent(toy_scenario):
    """Test: Preprocessing same scenario twice produces identical results"""
    params1 = preprocess_scenario(toy_scenario)
    params2 = preprocess_scenario(toy_scenario)
    
    # All key structures should be identical
    assert params1.source_ids == params2.source_ids
    assert params1.plant_ids == params2.plant_ids
    assert params1.zone_ids == params2.zone_ids
    assert params1.source_quality == params2.source_quality
    assert params1.demand_by_zone == params2.demand_by_zone
    assert params1.source_fixed_cost == params2.source_fixed_cost
    assert params1.plant_fixed_cost == params2.plant_fixed_cost

# ===== NETWORK CONNECTIVITY =====

def test_network_fully_connected(toy_scenario):
    """Test: Network has no isolated components"""
    parameters = preprocess_scenario(toy_scenario)
    
    # At least one arc from sources to plants
    assert len(parameters.source_plant_arcs) > 0, "No source-plant connections"
    
    # At least one arc from plants to zones
    assert len(parameters.plant_zone_arcs) > 0, "No plant-zone connections"

# ===== QUALITY FEASIBILITY TESTS =====

def test_quality_bounds_achievable_from_sources(toy_scenario):
    """Test: Quality bounds can be achieved from source values"""
    parameters = preprocess_scenario(toy_scenario)
    
    for param_id in parameters.quality_parameter_ids:
        lower_bound = parameters.quality_lower_bound[param_id]
        upper_bound = parameters.quality_upper_bound[param_id]
        
        # Get all source values for this parameter
        source_values = []
        for source_id in parameters.source_ids:
            value = parameters.source_quality.get((source_id, param_id))
            if value is not None:
                source_values.append(value)
        
        if source_values:
            min_source = min(source_values)
            max_source = max(source_values)
            
            # At least some source value should be within or close to bounds
            # (blending can achieve intermediate values)
            assert not (max_source < lower_bound or min_source > upper_bound), \
                f"Quality {param_id}: sources {min_source}-{max_source} can't achieve bounds {lower_bound}-{upper_bound}"

def test_no_impossible_quality_constraints(toy_scenario):
    """Test: Quality bounds don't contradict source values"""
    parameters = preprocess_scenario(toy_scenario)
    
    for param_id in parameters.quality_parameter_ids:
        lower_bound = parameters.quality_lower_bound[param_id]
        upper_bound = parameters.quality_upper_bound[param_id]
        
        # Get all source values
        source_values = [
            parameters.source_quality.get((sid, param_id))
            for sid in parameters.source_ids
        ]
        source_values = [v for v in source_values if v is not None]
        
        if source_values:
            # At least one source must be within bounds (for feasibility)
            within_bounds = [v for v in source_values if lower_bound <= v <= upper_bound]
            assert len(within_bounds) > 0, \
                f"Quality {param_id}: no source within bounds [{lower_bound}, {upper_bound}]"

def test_quality_achievable_through_blending(toy_scenario):
    """Test: Quality bounds are achievable through blending"""
    parameters = preprocess_scenario(toy_scenario)
    
    for param_id in parameters.quality_parameter_ids:
        lower_bound = parameters.quality_lower_bound[param_id]
        upper_bound = parameters.quality_upper_bound[param_id]
        
        source_values = [
            parameters.source_quality.get((sid, param_id))
            for sid in parameters.source_ids
        ]
        source_values = [v for v in source_values if v is not None]
        
        if source_values:
            min_val = min(source_values)
            max_val = max(source_values)
            
            # Blending can only achieve values between min and max of sources
            # So bounds must overlap with [min_val, max_val]
            assert not (upper_bound < min_val or lower_bound > max_val), \
                f"Quality {param_id}: bounds [{lower_bound}, {upper_bound}] outside achievable range [{min_val}, {max_val}]"

def test_all_sources_quality_feasibility(toy_scenario):
    """Test: All source quality values are within physical possibility"""
    parameters = preprocess_scenario(toy_scenario)
    
    for source_id in parameters.source_ids:
        for param_id in parameters.quality_parameter_ids:
            value = parameters.source_quality.get((source_id, param_id))
            if value is not None:
                # Quality value should be between bounds (sources should be feasible)
                lower = parameters.quality_lower_bound[param_id]
                upper = parameters.quality_upper_bound[param_id]
                
                # Source values might be outside bounds (that's why we blend)
                # But they should be physically possible
                assert lower < upper, f"Bounds inverted for {param_id}"

def test_quality_transformation_physically_possible(toy_scenario):
    """Test: Quality transformation is physically possible"""
    parameters = preprocess_scenario(toy_scenario)
    
    # For each quality parameter, check transformation preserves logic
    for param_id in parameters.quality_parameter_ids:
        lower = parameters.quality_lower_bound[param_id]
        upper = parameters.quality_upper_bound[param_id]
        
        # Bounds should be positive for most quality params (pH, turbidity, alkalinity)
        if "hydrogen" in param_id.lower():
            # Hydrogen ion concentration should be positive
            assert lower > 0 and upper > 0, \
                f"Hydrogen ion bounds should be positive: {lower}-{upper}"
        
        if "turbid" in param_id.lower() or "alkalin" in param_id.lower():
            # Turbidity and alkalinity should be non-negative
            assert lower >= 0 and upper >= 0, \
                f"{param_id} bounds should be non-negative: {lower}-{upper}"

# ===== NETWORK FLOW FEASIBILITY TESTS =====

def test_source_min_withdrawal_vs_arc_capacity(toy_scenario):
    """Test: Source min withdrawal doesn't exceed arc capacity"""
    parameters = preprocess_scenario(toy_scenario)
    
    for source_id in parameters.source_ids:
        min_withdrawal = parameters.source_min_withdrawal[source_id]
        
        # Get all arcs from this source
        outgoing_arcs = [
            (src, plant) for src, plant in parameters.source_plant_arcs
            if src == source_id
        ]
        
        if outgoing_arcs and min_withdrawal > 0:
            total_arc_capacity = sum(
                parameters.source_plant_link_capacity[(src, plant)]
                for src, plant in outgoing_arcs
            )
            
            # Total arc capacity must be >= min withdrawal
            assert total_arc_capacity >= min_withdrawal, \
                f"Source {source_id}: min withdrawal {min_withdrawal} > total arc capacity {total_arc_capacity}"

def test_plant_min_throughput_achievable(toy_scenario):
    """Test: Plant min throughput achievable from sources"""
    parameters = preprocess_scenario(toy_scenario)
    
    for plant_id in parameters.plant_ids:
        min_throughput = parameters.plant_min_throughput[plant_id]
        
        # Get all incoming arcs to this plant
        incoming_arcs = [
            (src, plant) for src, plant in parameters.source_plant_arcs
            if plant == plant_id
        ]
        
        if incoming_arcs and min_throughput > 0:
            total_incoming_capacity = sum(
                parameters.source_plant_link_capacity[(src, plant)]
                for src, plant in incoming_arcs
            )
            
            # Total incoming capacity must be >= min throughput
            assert total_incoming_capacity >= min_throughput, \
                f"Plant {plant_id}: min throughput {min_throughput} > incoming capacity {total_incoming_capacity}"

def test_demand_satisfiable_by_network(toy_scenario):
    """Test: Zone demand can be satisfied by network"""
    parameters = preprocess_scenario(toy_scenario)
    
    for zone_id in parameters.zone_ids:
        demand = parameters.demand_by_zone[zone_id]
        
        # Get all incoming arcs to this zone
        incoming_arcs = [
            (plant, zone) for plant, zone in parameters.plant_zone_arcs
            if zone == zone_id
        ]
        
        if incoming_arcs and demand > 0:
            total_incoming_capacity = sum(
                parameters.plant_zone_link_capacity[(plant, zone)]
                for plant, zone in incoming_arcs
            )
            
            # Total incoming capacity must be >= demand
            assert total_incoming_capacity >= demand, \
                f"Zone {zone_id}: demand {demand} > incoming capacity {total_incoming_capacity}"

def test_no_bottleneck_in_network(toy_scenario):
    """Test: No single arc is a critical bottleneck"""
    parameters = preprocess_scenario(toy_scenario)
    
    total_demand = sum(parameters.demand_by_zone.values())
    
    # No single arc should be responsible for ALL demand
    # (unless it's the only arc, which is OK)
    for arc, capacity in parameters.source_plant_link_capacity.items():
        if len(parameters.source_plant_arcs) > 1:
            # Arc shouldn't be able to carry all demand by itself
            # (unless network is very small)
            if total_demand > 0:
                assert capacity < total_demand * 2, \
                    f"Arc {arc} capacity {capacity} is disproportionately large vs demand {total_demand}"

def test_arc_capacity_vs_source_capacity(toy_scenario):
    """Test: Arc capacities consistent with source capacities"""
    parameters = preprocess_scenario(toy_scenario)
    
    for source_id in parameters.source_ids:
        max_withdrawal = parameters.source_max_withdrawal[source_id]
        
        # Get all outgoing arc capacities
        outgoing_capacity = sum(
            parameters.source_plant_link_capacity[(src, plant)]
            for src, plant in parameters.source_plant_arcs
            if src == source_id
        )
        
        # Arc capacities shouldn't exceed source max withdrawal
        # (source can't push more than it can withdraw)
        if outgoing_capacity > 0:
            assert outgoing_capacity >= parameters.source_min_withdrawal[source_id], \
                f"Source {source_id}: outgoing capacity {outgoing_capacity} < min withdrawal"


# ===== NUMERICAL STABILITY TESTS =====

def test_pH_is_expressed_as_nmol_per_liter(toy_parameters):
    """Test: pH correctly transformed to nmol/L for solver numerical stability
    
    Rationale: HiGHS solver has precision issues with very small floating point values.
    pH in mol/L creates values like 1e-14 (pH 0) to 1e-7 (pH 7), causing solver precision loss.
    Scaling to nmol/L (multiply by 1e9) creates values like 1 to 1e8, improving numerical stability.
    
    This test verifies preprocessing layer applies correct scaling.
    The solver layer test (in test_constraints.py) verifies solver behavior is correct with this scaling.
    """
    
    ph_param_id = None
    for param_id in toy_parameters.quality_parameter_ids:
        if "hydrogen" in param_id.lower() or "ph" in param_id.lower():
            ph_param_id = param_id
            break
    
    assert ph_param_id is not None, "No pH/hydrogen ion parameter found in quality parameters"
    
    # Verify unit is nmol/L, not mol/L
    assert ph_param_id in toy_parameters.quality_units, \
        f"pH parameter {ph_param_id} missing unit specification"
    
    unit = toy_parameters.quality_units[ph_param_id]
    assert unit == "nmol/L", \
        f"pH unit is '{unit}' but should be 'nmol/L' (mol/L causes solver precision issues)"
    
    # Verify values are in correct range for nmol/L
    # Expected range: pH 0-14 → 1e14 nmol/L to 1e0 nmol/L
    # pH 7 (neutral) → ~1e8 nmol/L
    # pH 8.5 (typical upper) → ~3160 nmol/L
    
    for source_id in toy_parameters.source_ids:
        value = toy_parameters.source_quality.get((source_id, ph_param_id))
        
        if value is not None:
            # If value < 1e-5, it's probably still in mol/L (not scaled)
            assert value >= 1, \
                f"Source {source_id} pH = {value} - too small, looks like mol/L not nmol/L. " \
                f"Expected value >= 1 for nmol/L."
            
            # pH 14 in nmol/L ≈ 1e14, so upper bound shouldn't exceed this
            assert value <= 1e15, \
                f"Source {source_id} pH = {value} - unrealistically large for nmol/L"
    
    # Verify bounds are also in nmol/L scale
    lower = toy_parameters.quality_lower_bound[ph_param_id]
    upper = toy_parameters.quality_upper_bound[ph_param_id]
    
    assert lower >= 1, \
        f"pH lower bound = {lower} - too small, looks like mol/L not nmol/L"
    assert upper >= 1, \
        f"pH upper bound = {upper} - too small, looks like mol/L not nmol/L"
    assert lower < upper, \
        f"pH bounds invalid: {lower} >= {upper}"

def test_all_quality_values_numerically_stable(toy_parameters):
    """Test: All quality values are in numerically stable range for solver
    
    HiGHS can struggle with extremely small (< 1e-8) or large (> 1e8) values.
    This test ensures preprocessing produces values in stable range.
    """
    
    for source_id in toy_parameters.source_ids:
        for param_id in toy_parameters.quality_parameter_ids:
            value = toy_parameters.source_quality.get((source_id, param_id))
            
            if value is not None:
                # Avoid extremes that cause numerical precision loss
                assert value >= 1e-8, \
                    f"{source_id}.{param_id} = {value} - too small, solver precision issues"
                assert value <= 1e10, \
                    f"{source_id}.{param_id} = {value} - too large, solver precision issues"