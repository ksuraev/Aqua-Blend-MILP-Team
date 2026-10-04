"""Tests for runtime validation of quality-parameter identifiers."""

from src.data_loader import _normalise_quality_limits


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


def test_loader_defaults_missing_quality_parameter_id() -> None:
    result = _normalise_quality_limits(_quality_limits(None))

    assert result["parameters"]["ph"]["id"] == "ph"
