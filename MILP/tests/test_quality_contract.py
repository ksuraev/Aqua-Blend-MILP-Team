"""Tests for quality-parameter metadata in scenario JSON contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

MILP_ROOT = Path(__file__).resolve().parents[1]

CONTRACT_FILES = (
    MILP_ROOT / "json_contracts" / "model_input_contract.json",
    MILP_ROOT / "config" / "scenarios" / "base_scenarios_v1.json",
)

EXPECTED_PARAMETER_IDS = {"ph", "alkalinity", "turbidity"}

REQUIRED_PARAMETER_FIELDS = {
    "id",
    "name",
    "unit",
    "min",
    "max",
    "transform",
    "source_field",
    "model_name",
    "model_unit",
}


def _read_json(path: Path) -> dict[str, object]:
    """Read and return one JSON contract file."""
    with path.open(encoding="utf-8") as file:
        data = json.load(file)

    assert isinstance(data, dict)
    return data


def _quality_parameters(path: Path) -> dict[str, object]:
    """Return the quality-parameter definitions from one contract."""
    scenario = _read_json(path)

    quality_limits = scenario["quality_limits"]
    assert isinstance(quality_limits, dict)

    parameters = quality_limits["parameters"]
    assert isinstance(parameters, dict)

    return parameters


@pytest.mark.parametrize("path", CONTRACT_FILES)
def test_quality_parameters_use_stable_ids(path: Path) -> None:
    """Quality keys and their explicit IDs must agree."""
    parameters = _quality_parameters(path)

    assert set(parameters) == EXPECTED_PARAMETER_IDS

    for parameter_id, specification in parameters.items():
        assert isinstance(specification, dict)
        assert specification["id"] == parameter_id


@pytest.mark.parametrize("path", CONTRACT_FILES)
def test_quality_parameters_have_required_metadata(path: Path) -> None:
    """Every quality parameter must describe its source and model mapping."""
    parameters = _quality_parameters(path)

    for parameter_id, specification in parameters.items():
        assert isinstance(specification, dict)

        missing_fields = REQUIRED_PARAMETER_FIELDS - set(specification)

        assert not missing_fields, (
            f"{path.name}: {parameter_id} is missing {sorted(missing_fields)}"
        )


@pytest.mark.parametrize("path", CONTRACT_FILES)
def test_quality_parameter_ranges_are_valid(path: Path) -> None:
    """Each minimum must be less than or equal to its maximum."""
    parameters = _quality_parameters(path)

    for parameter_id, specification in parameters.items():
        assert isinstance(specification, dict)

        minimum = specification["min"]
        maximum = specification["max"]

        assert isinstance(minimum, (int, float))
        assert not isinstance(minimum, bool)
        assert isinstance(maximum, (int, float))
        assert not isinstance(maximum, bool)
        assert minimum <= maximum, f"Invalid range for {parameter_id}"


@pytest.mark.parametrize("path", CONTRACT_FILES)
def test_quality_source_fields_are_unique(path: Path) -> None:
    """Different parameters must not read the same source field."""
    parameters = _quality_parameters(path)

    source_fields = []

    for specification in parameters.values():
        assert isinstance(specification, dict)
        source_fields.append(specification["source_field"])

    assert len(source_fields) == len(set(source_fields))


@pytest.mark.parametrize("path", CONTRACT_FILES)
def test_ph_uses_nmol_per_litre_for_model(path: Path) -> None:
    """Transformed pH must use the numerically stable model unit."""
    parameters = _quality_parameters(path)

    ph = parameters["ph"]
    assert isinstance(ph, dict)

    assert ph["name"] == "pH"
    assert ph["transform"] == "ph_to_hydrogen_ion"
    assert ph["source_field"] == "representative_ph"
    assert ph["model_unit"] == "nmol/L"
