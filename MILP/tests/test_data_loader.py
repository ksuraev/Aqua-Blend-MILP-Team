"""Tests for the InputValidationPolicy / LoaderValidation reporting added to
data_loader.py, and how it is passed through into ScenarioData.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.contracts import InputValidationPolicy, LoaderValidation
from src.data_loader import DataLoadError, load_scenario

# A minimal, internally-consistent inline scenario: one source, one plant,
# one demand zone, fully linked, with a single quality parameter. Every test
# deep-copies this and mutates only what it needs to isolate.
_BASE_CONFIG: dict[str, Any] = {
    "scenario_id": "test_scenario",
    "scenario_name": "Test Scenario",
    "status": "test",
    "description": "",
    "data_source": {
        "type": "inline",
        "allow_estimated_values": False,
        "source_rows": [
            {
                "source_id": "S1",
                "source_name": "Source 1",
                "source_type": "reservoir",
                "is_active": True,
                "minimum_withdrawal_ml_per_day": 1.0,
                "max_available_ml_per_day": 20.0,
                "cost_per_ml": 1.0,
                "turbidity_ntu": 2.0,
            }
        ],
    },
    "validation": {
        "fail_if_source_missing_from_database": True,
        "fail_if_daily_availability_missing": True,
        "fail_if_required_quality_value_missing": True,
        "fail_if_demand_missing": True,
    },
    "quality_limits": {
        "parameters": {
            "turbidity": {
                "min": 0.0,
                "max": 5.0,
                "unit": "NTU",
                "transform": "identity",
                "source_field": "turbidity_ntu",
            }
        }
    },
    "sources": [
        {
            "source_id": "S1",
            "enabled": True,
            "forced_inactive": False,
            "fixed_activation_cost": 0.0,
        }
    ],
    "network": {
        "plants": [
            {
                "plant_id": "P1",
                "name": "Plant 1",
                "enabled": True,
                "minimum_processing_capacity_ml_per_day": 0.0,
                "maximum_processing_capacity_ml_per_day": 100.0,
                "fixed_activation_cost": 0.0,
                "treatment_cost_per_ml": 0.1,
            }
        ],
        "demand_zones": [
            {"zone_id": "Z1", "name": "Zone 1", "demand_ml_per_day": 10.0}
        ],
        "source_to_plant_links": [
            {
                "source_id": "S1",
                "plant_id": "P1",
                "enabled": True,
                "maximum_flow_ml_per_day": 50.0,
            }
        ],
        "plant_to_zone_links": [
            {
                "plant_id": "P1",
                "zone_id": "Z1",
                "enabled": True,
                "maximum_flow_ml_per_day": 50.0,
            }
        ],
    },
}


# Used to give every test its own mutable copy of the base scenario config.
def _config(**overrides: Any) -> dict[str, Any]:
    """Return a deep copy of the base config, optionally with top-level overrides."""
    config = copy.deepcopy(_BASE_CONFIG)
    config.update(overrides)
    return config


# Used to write a scenario config to disk so load_scenario can read it back.
def _write(tmp_path: Path, config: dict[str, Any]) -> Path:
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


# Used to look up a named check inside a LoaderValidation's checks tuple.
def _check(validation: LoaderValidation, name: str):
    matches = [c for c in validation.checks if c.check == name]
    assert matches, f"No check named {name!r} in {validation.checks!r}"
    return matches[0]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_scenario_passes_all_loader_checks(tmp_path: Path) -> None:
    """A fully valid scenario reports PASSED with every check passing."""
    scenario = load_scenario(_write(tmp_path, _config()), strict=True)

    assert scenario.loader_validation.status == "PASSED"
    assert scenario.loader_validation.scenario_ready is True
    assert scenario.loader_validation.validation_issues == ()
    assert all(check.passed for check in scenario.loader_validation.checks)
    assert scenario.is_ready is True


def test_is_ready_matches_loader_validation_scenario_ready(tmp_path: Path) -> None:
    """ScenarioData.is_ready and loader_validation.scenario_ready must agree."""
    config = _config()
    config["network"]["demand_zones"][0]["demand_ml_per_day"] = None
    scenario = load_scenario(_write(tmp_path, config), strict=False)

    assert scenario.is_ready is False
    assert scenario.loader_validation.scenario_ready is False
    assert scenario.loader_validation.status == "FAILED"


# ---------------------------------------------------------------------------
# InputValidationPolicy pass-through
# ---------------------------------------------------------------------------


def test_input_policy_validation_defaults_to_strict_when_omitted(
    tmp_path: Path,
) -> None:
    """Omitting the "validation" block defaults every policy flag to True."""
    config = _config()
    del config["validation"]
    scenario = load_scenario(_write(tmp_path, config), strict=True)

    assert scenario.input_policy_validation == InputValidationPolicy(
        fail_if_source_missing_from_database=True,
        fail_if_daily_availability_missing=True,
        fail_if_required_quality_value_missing=True,
        fail_if_demand_missing=True,
    )


def test_input_policy_validation_reflects_explicit_flags(tmp_path: Path) -> None:
    """Explicit policy flags in the scenario file are carried onto ScenarioData."""
    config = _config()
    config["validation"] = {
        "fail_if_source_missing_from_database": False,
        "fail_if_daily_availability_missing": False,
        "fail_if_required_quality_value_missing": True,
        "fail_if_demand_missing": False,
    }
    scenario = load_scenario(_write(tmp_path, config), strict=True)

    assert scenario.input_policy_validation == InputValidationPolicy(
        fail_if_source_missing_from_database=False,
        fail_if_daily_availability_missing=False,
        fail_if_required_quality_value_missing=True,
        fail_if_demand_missing=False,
    )


# ---------------------------------------------------------------------------
# Policy-gated soft failures
# ---------------------------------------------------------------------------


def test_missing_source_in_database_fails_by_default(tmp_path: Path) -> None:
    """A configured source absent from the data source is a blocking issue by default."""
    config = _config()
    config["sources"].append(
        {"source_id": "S2", "enabled": True, "forced_inactive": False}
    )
    with pytest.raises(DataLoadError):
        load_scenario(_write(tmp_path, config), strict=True)


def test_missing_source_in_database_dropped_when_policy_relaxed(
    tmp_path: Path,
) -> None:
    """Relaxing fail_if_source_missing_from_database silently drops the source."""
    config = _config()
    config["validation"]["fail_if_source_missing_from_database"] = False
    config["sources"].append(
        {"source_id": "S2", "enabled": True, "forced_inactive": False}
    )
    scenario = load_scenario(_write(tmp_path, config), strict=True)

    assert scenario.loader_validation.status == "PASSED"
    assert {source.source_id for source in scenario.sources} == {"S1"}


def test_missing_daily_availability_fails_by_default(tmp_path: Path) -> None:
    """A source with no withdrawal bounds is a blocking issue by default."""
    config = _config()
    del config["data_source"]["source_rows"][0]["minimum_withdrawal_ml_per_day"]
    del config["data_source"]["source_rows"][0]["max_available_ml_per_day"]
    scenario = load_scenario(_write(tmp_path, config), strict=False)

    assert _check(scenario.loader_validation, "source_withdrawal_bounds_present").passed is False
    assert scenario.loader_validation.status == "FAILED"


def test_missing_daily_availability_allowed_when_policy_relaxed(
    tmp_path: Path,
) -> None:
    """Relaxing fail_if_daily_availability_missing tolerates missing bounds."""
    config = _config()
    config["validation"]["fail_if_daily_availability_missing"] = False
    del config["data_source"]["source_rows"][0]["minimum_withdrawal_ml_per_day"]
    del config["data_source"]["source_rows"][0]["max_available_ml_per_day"]
    scenario = load_scenario(_write(tmp_path, config), strict=True)

    assert scenario.loader_validation.status == "PASSED"
    assert scenario.sources[0].minimum_withdrawal_ml_per_day is None
    assert scenario.sources[0].maximum_withdrawal_ml_per_day is None


def test_missing_quality_value_fails_by_default(tmp_path: Path) -> None:
    """A source missing a required quality value is a blocking issue by default."""
    config = _config()
    del config["data_source"]["source_rows"][0]["turbidity_ntu"]
    scenario = load_scenario(_write(tmp_path, config), strict=False)

    assert _check(scenario.loader_validation, "source_quality_values_numeric_and_finite").passed is False
    assert scenario.loader_validation.status == "FAILED"


def test_missing_quality_value_still_fails_key_match_when_policy_relaxed(
    tmp_path: Path,
) -> None:
    """Relaxing fail_if_required_quality_value_missing skips that one check, but the
    source's quality dict still won't match quality_limits.parameters, so the
    scenario still fails via source_quality_keys_match_quality_limits.
    """
    config = _config()
    config["validation"]["fail_if_required_quality_value_missing"] = False
    del config["data_source"]["source_rows"][0]["turbidity_ntu"]
    scenario = load_scenario(_write(tmp_path, config), strict=False)

    assert _check(scenario.loader_validation, "source_quality_values_numeric_and_finite").passed is True
    assert _check(scenario.loader_validation, "source_quality_keys_match_quality_limits").passed is False
    assert scenario.loader_validation.status == "FAILED"


def test_missing_demand_fails_by_default(tmp_path: Path) -> None:
    """A demand zone with no demand value is a blocking issue by default."""
    config = _config()
    config["network"]["demand_zones"][0]["demand_ml_per_day"] = None
    scenario = load_scenario(_write(tmp_path, config), strict=False)

    assert _check(scenario.loader_validation, "demand_present_finite_and_non_negative").passed is False


def test_missing_demand_allowed_when_policy_relaxed(tmp_path: Path) -> None:
    """Relaxing fail_if_demand_missing tolerates a demand zone with no demand value."""
    config = _config()
    config["validation"]["fail_if_demand_missing"] = False
    config["network"]["demand_zones"][0]["demand_ml_per_day"] = None
    scenario = load_scenario(_write(tmp_path, config), strict=True)

    assert scenario.loader_validation.status == "PASSED"
    assert scenario.demand_zones[0].demand_ml_per_day is None


def test_estimated_values_rejected_unless_allowed(tmp_path: Path) -> None:
    """An estimated value fails when the data source disallows estimates."""
    config = _config()
    config["data_source"]["source_rows"][0]["cost_is_estimated"] = True
    scenario = load_scenario(_write(tmp_path, config), strict=False)

    assert _check(scenario.loader_validation, "estimated_values_allowed_by_policy").passed is False


def test_estimated_values_allowed_when_policy_permits(tmp_path: Path) -> None:
    """Estimated values pass once the data source explicitly allows them."""
    config = _config()
    config["data_source"]["allow_estimated_values"] = True
    config["data_source"]["source_rows"][0]["cost_is_estimated"] = True
    scenario = load_scenario(_write(tmp_path, config), strict=True)

    assert scenario.loader_validation.status == "PASSED"
    assert scenario.sources[0].has_estimated_values is True


# ---------------------------------------------------------------------------
# forced_inactive sources skip soft checks
# ---------------------------------------------------------------------------


def test_forced_inactive_source_skips_availability_and_quality_checks(
    tmp_path: Path,
) -> None:
    """A forced-inactive source is loaded even with missing withdrawal/quality data."""
    config = _config()
    config["sources"][0]["forced_inactive"] = True
    del config["data_source"]["source_rows"][0]["turbidity_ntu"]
    del config["data_source"]["source_rows"][0]["minimum_withdrawal_ml_per_day"]
    del config["data_source"]["source_rows"][0]["max_available_ml_per_day"]
    scenario = load_scenario(_write(tmp_path, config), strict=True)

    assert scenario.loader_validation.status == "PASSED"
    assert scenario.sources[0].forced_inactive is True


# ---------------------------------------------------------------------------
# Bound ordering / non-negativity soft failures
# ---------------------------------------------------------------------------


def test_source_withdrawal_min_greater_than_max_soft_fails(tmp_path: Path) -> None:
    """A minimum withdrawal above the maximum is reported, not raised."""
    config = _config()
    config["data_source"]["source_rows"][0]["minimum_withdrawal_ml_per_day"] = 30.0
    config["data_source"]["source_rows"][0]["max_available_ml_per_day"] = 5.0
    scenario = load_scenario(_write(tmp_path, config), strict=False)

    assert _check(
        scenario.loader_validation, "source_withdrawal_bounds_non_negative_and_ordered"
    ).passed is False


def test_negative_source_cost_soft_fails(tmp_path: Path) -> None:
    """A negative cost per ML is reported, not raised."""
    config = _config()
    config["data_source"]["source_rows"][0]["cost_per_ml"] = -1.0
    scenario = load_scenario(_write(tmp_path, config), strict=False)

    assert _check(
        scenario.loader_validation, "source_cost_present_finite_and_non_negative"
    ).passed is False


def test_plant_capacity_min_greater_than_max_soft_fails(tmp_path: Path) -> None:
    """A plant minimum capacity above its maximum is reported, not raised."""
    config = _config()
    config["network"]["plants"][0]["minimum_processing_capacity_ml_per_day"] = 50.0
    config["network"]["plants"][0]["maximum_processing_capacity_ml_per_day"] = 10.0
    scenario = load_scenario(_write(tmp_path, config), strict=False)

    assert _check(
        scenario.loader_validation,
        "plant_capacity_bounds_finite_non_negative_and_ordered",
    ).passed is False


def test_negative_link_capacity_soft_fails(tmp_path: Path) -> None:
    """A negative link flow capacity is reported, not raised."""
    config = _config()
    config["network"]["source_to_plant_links"][0]["maximum_flow_ml_per_day"] = -5.0
    scenario = load_scenario(_write(tmp_path, config), strict=False)

    assert _check(
        scenario.loader_validation, "link_capacities_finite_and_non_negative"
    ).passed is False


# ---------------------------------------------------------------------------
# Uniqueness / referential soft failures
# ---------------------------------------------------------------------------


def test_duplicate_enabled_plant_ids_soft_fails(tmp_path: Path) -> None:
    """A duplicated enabled plant ID is reported, not raised."""
    config = _config()
    duplicate_plant = copy.deepcopy(config["network"]["plants"][0])
    config["network"]["plants"].append(duplicate_plant)
    scenario = load_scenario(_write(tmp_path, config), strict=False)

    assert _check(scenario.loader_validation, "plant_ids_present_and_unique").passed is False


def test_link_referencing_unknown_plant_soft_fails(tmp_path: Path) -> None:
    """A plant-to-zone link naming a plant that doesn't exist is reported, not raised."""
    config = _config()
    config["network"]["plant_to_zone_links"][0]["plant_id"] = "unknown_plant"
    scenario = load_scenario(_write(tmp_path, config), strict=False)

    assert _check(
        scenario.loader_validation, "plant_to_zone_links_unique_and_valid"
    ).passed is False


# ---------------------------------------------------------------------------
# Hard failures (raise before a LoaderValidation is ever built)
# ---------------------------------------------------------------------------


def test_duplicate_enabled_source_ids_raises(tmp_path: Path) -> None:
    """Duplicate enabled source IDs abort loading immediately."""
    config = _config()
    config["sources"].append(dict(config["sources"][0]))
    with pytest.raises(DataLoadError):
        load_scenario(_write(tmp_path, config), strict=False)


def test_missing_scenario_id_raises(tmp_path: Path) -> None:
    """A missing scenario_id aborts loading immediately, regardless of strict."""
    config = _config()
    del config["scenario_id"]
    with pytest.raises(DataLoadError):
        load_scenario(_write(tmp_path, config), strict=False)


def test_quality_parameter_missing_min_raises(tmp_path: Path) -> None:
    """An incomplete quality-limit definition aborts loading immediately."""
    config = _config()
    del config["quality_limits"]["parameters"]["turbidity"]["min"]
    with pytest.raises(DataLoadError):
        load_scenario(_write(tmp_path, config), strict=False)


# ---------------------------------------------------------------------------
# strict flag behaviour
# ---------------------------------------------------------------------------


def test_strict_mode_raises_with_issue_text(tmp_path: Path) -> None:
    """strict=True raises DataLoadError whose message includes every issue."""
    config = _config()
    config["network"]["demand_zones"][0]["demand_ml_per_day"] = None
    with pytest.raises(DataLoadError, match="Demand zone 'Z1'"):
        load_scenario(_write(tmp_path, config), strict=True)


def test_non_strict_mode_returns_scenario_instead_of_raising(tmp_path: Path) -> None:
    """strict=False returns a not-ready ScenarioData instead of raising."""
    config = _config()
    config["network"]["demand_zones"][0]["demand_ml_per_day"] = None
    scenario = load_scenario(_write(tmp_path, config), strict=False)

    assert scenario.is_ready is False
    assert scenario.validation_issues
