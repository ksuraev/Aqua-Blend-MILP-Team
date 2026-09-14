"""Tests for runtime validation of quality-parameter identifiers."""

import pytest

from src.data_loader import DataLoadError, _normalise_quality_limits


def _quality_limits(parameter_id: str | None = "ph") -> dict:
    parameter = {
        "name": "pH",
        "unit": "pH",
        "min": 6.5,
        "max": 8.5,
        "transform": "ph_to_hydrogen_ion",
        "source_field": "representative_ph",
        "model_name": "hydrogen_ion_concentration_nmol_l",
        "model_unit": "nmol/L",
    }

    if parameter_id is not None:
        parameter["id"] = parameter_id

    return {
        "applies_to": "blend_at_plant_inflow",
        "parameters": {
            "ph": parameter,
        },
    }


def test_loader_accepts_matching_quality_parameter_id() -> None:
    result = _normalise_quality_limits(_quality_limits())

    assert result["parameters"]["ph"]["id"] == "ph"


def test_loader_rejects_mismatched_quality_parameter_id() -> None:
    with pytest.raises(DataLoadError, match="must exactly match"):
        _normalise_quality_limits(_quality_limits("pH"))


def test_loader_rejects_blank_quality_parameter_id() -> None:
    with pytest.raises(DataLoadError, match=r"parameters\.ph\.id"):
        _normalise_quality_limits(_quality_limits(""))


def test_loader_accepts_legacy_parameter_without_explicit_id() -> None:
    result = _normalise_quality_limits(_quality_limits(None))

    assert "id" not in result["parameters"]["ph"]
