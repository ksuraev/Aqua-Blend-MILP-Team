"""
Unit tests for optimisation_variables.py

These tests check that:
  * every source/plant/zone has an associated cost entry
  * no entity is missing and no extra/typo'd entity has snuck in
  * the ordering of cost dict keys matches the ordering of the
    sources/plants/zones lists (important if any downstream code
    relies on iterating costs in the same order as the entity lists)
  * paired variable dicts (source-plant, plant-zone) contain exactly
    the expected combinations, in the expected loop order
"""

import itertools
import pytest

import MILP.model.vars as ov


# ---------------------------------------------------------------------------
# Single-entity cost dictionaries: keys present + correctly ordered
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "cost_dict, entities, name",
    [
        (ov.source_activation_cost, ov.sources, "source_activation_cost"),
        (ov.source_draw_cost, ov.sources, "source_draw_cost"),
        (ov.plant_activation_cost, ov.plants, "plant_activation_cost"),
        (ov.plant_treatment_cost, ov.plants, "plant_treatment_cost"),
    ],
)
def test_cost_keys_match_entities_exactly(cost_dict, entities, name):
    """Every entity has a cost, and there are no unexpected/missing entries."""
    assert set(cost_dict.keys()) == set(entities), (
        f"{name} keys {set(cost_dict.keys())} do not match "
        f"expected entities {set(entities)}"
    )


@pytest.mark.parametrize(
    "cost_dict, entities, name",
    [
        (ov.source_activation_cost, ov.sources, "source_activation_cost"),
        (ov.source_draw_cost, ov.sources, "source_draw_cost"),
        (ov.plant_activation_cost, ov.plants, "plant_activation_cost"),
        (ov.plant_treatment_cost, ov.plants, "plant_treatment_cost"),
    ],
)
def test_cost_keys_match_entities_in_order(cost_dict, entities, name):
    """Cost dict iteration order matches the order of the source/plant list."""
    assert list(cost_dict.keys()) == list(entities), (
        f"{name} key order {list(cost_dict.keys())} does not match "
        f"expected order {list(entities)}"
    )


@pytest.mark.parametrize(
    "cost_dict, name",
    [
        (ov.source_activation_cost, "source_activation_cost"),
        (ov.source_draw_cost, "source_draw_cost"),
        (ov.plant_activation_cost, "plant_activation_cost"),
        (ov.plant_treatment_cost, "plant_treatment_cost"),
    ],
)
def test_cost_values_are_numeric_and_defined(cost_dict, name):
    """Every cost value is present and numeric (not None, not missing)."""
    for entity, value in cost_dict.items():
        assert value is not None, f"{name}[{entity!r}] is None"
        assert isinstance(value, (int, float)), (
            f"{name}[{entity!r}] = {value!r} is not numeric"
        )


# ---------------------------------------------------------------------------
# Single-entity decision variables: keys present + correctly ordered
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "var_dict, entities, name",
    [
        (ov.s_active, ov.sources, "s_active"),
        (ov.s_vol, ov.sources, "s_vol"),
        (ov.t_active, ov.plants, "t_active"),
    ],
)
def test_single_entity_variables_match_entities_in_order(var_dict, entities, name):
    assert list(var_dict.keys()) == list(entities), (
        f"{name} key order {list(var_dict.keys())} does not match "
        f"expected order {list(entities)}"
    )


# ---------------------------------------------------------------------------
# Paired-entity decision variables: correct combinations, correct order
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "var_dict, name",
    [(ov.s_t_active, "s_t_active"), (ov.s_t_vol, "s_t_vol")],
)
def test_source_plant_variables_cover_all_pairs_in_order(var_dict, name):
    expected_order = list(itertools.product(ov.sources, ov.plants))
    assert list(var_dict.keys()) == expected_order, (
        f"{name} key order {list(var_dict.keys())} does not match "
        f"expected source-outer order {expected_order}"
    )


@pytest.mark.parametrize(
    "var_dict, name",
    [(ov.t_z_active, "t_z_active"), (ov.t_z_vol, "t_z_vol")],
)
def test_plant_zone_variables_cover_all_pairs_in_order(var_dict, name):
    expected_order = list(itertools.product(ov.plants, ov.zones))
    assert list(var_dict.keys()) == expected_order, (
        f"{name} key order {list(var_dict.keys())} does not match "
        f"expected plant-outer order {expected_order}"
    )


# ---------------------------------------------------------------------------
# Cross-check: every paired variable has a way to be costed via the
# single-entity costs it's built from (no orphan combinations)
# ---------------------------------------------------------------------------

def test_every_source_plant_pair_can_be_costed_from_source_and_plant_costs():
    for (s, p) in ov.s_t_active:
        assert s in ov.source_draw_cost, f"No draw cost defined for source {s!r}"
        assert p in ov.plant_activation_cost, f"No activation cost for plant {p!r}"


def test_every_plant_zone_pair_references_a_valid_plant():
    for (p, z) in ov.t_z_active:
        assert p in ov.plants, f"{p!r} is not a known plant"
        assert z in ov.zones, f"{z!r} is not a known zone"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))