"""
Essential unit tests for quality_transformations.py.

These tests verify the core mathematical transformations and validation
required before water quality values are passed into the AquaBlend MILP.
"""

import pytest

from quality_transformations import (
    QualityTransformationError,
    hydrogen_concentration_to_ph,
    ph_to_hydrogen_concentration,
    transform_ph_bounds,
    transform_quality_limits,
    transform_source_quality_values,
)

# ---------------------------------------------------------------------------
# pH conversion tests
# ---------------------------------------------------------------------------

def test_ph_to_hydrogen_concentration():
    """
    A pH value of 7 must be converted to 1 × 10^-7.
    """
    result = ph_to_hydrogen_concentration(7.0)

    assert result == pytest.approx(1e-7)


def test_hydrogen_concentration_can_be_converted_back_to_ph():
    """
    Converting pH to hydrogen-ion concentration and back should recover
    the original pH value.
    """
    original_ph = 7.4

    concentration = ph_to_hydrogen_concentration(original_ph)
    recovered_ph = hydrogen_concentration_to_ph(concentration)

    assert recovered_ph == pytest.approx(original_ph)


def test_invalid_ph_value_is_rejected():
    """
    pH values outside the supported range of 0 to 14 must be rejected.
    """
    with pytest.raises(QualityTransformationError):
        ph_to_hydrogen_concentration(15.0)


# ---------------------------------------------------------------------------
# pH-bound transformation tests
# ---------------------------------------------------------------------------

def test_ph_bounds_are_transformed_and_reversed():
    """
    The raw pH range 6.5 to 8.5 must be converted into hydrogen-ion
    concentration bounds in the reverse order.

    Higher pH produces lower hydrogen-ion concentration.
    """
    minimum_hydrogen, maximum_hydrogen = transform_ph_bounds(
        minimum_ph=6.5,
        maximum_ph=8.5,
    )

    assert minimum_hydrogen == pytest.approx(10 ** -8.5)
    assert maximum_hydrogen == pytest.approx(10 ** -6.5)
    assert minimum_hydrogen < maximum_hydrogen


def test_reversed_raw_ph_bounds_are_rejected():
    """
    The raw minimum pH cannot be greater than the raw maximum pH.
    """
    with pytest.raises(QualityTransformationError):
        transform_ph_bounds(
            minimum_ph=8.5,
            maximum_ph=6.5,
        )


# ---------------------------------------------------------------------------
# Complete quality limit transformation test
# ---------------------------------------------------------------------------

def test_quality_limits_are_transformed_correctly():
    """
    pH must be renamed and transformed, while alkalinity and turbidity
    must retain their original values through the identity transform.
    """
    quality_limits = {
        "applies_to": "blend_at_plant_inflow",
        "parameters": {
            "pH": {
                "unit": "pH",
                "min": 6.5,
                "max": 8.5,
                "transform": "hydrogenic",
            },
            "alkalinity": {
                "unit": "mg/L CaCO3",
                "min": 20,
                "max": 100,
                "transform": "identity",
            },
            "turbidity": {
                "unit": "NTU",
                "min": 0,
                "max": 8.0,
                "transform": "identity",
            },
        },
    }

    result = transform_quality_limits(quality_limits)
    parameters = result["parameters"]

    # Raw pH is replaced by the model-facing transformed parameter.
    assert "pH" not in parameters
    assert "hydrogen_ion_concentration" in parameters

    hydrogen_limits = parameters["hydrogen_ion_concentration"]

    assert hydrogen_limits["min"] == pytest.approx(10 ** -8.5)
    assert hydrogen_limits["max"] == pytest.approx(10 ** -6.5)
    assert hydrogen_limits["unit"] == "mol/L"
    assert hydrogen_limits["source_parameter"] == "pH"
    assert hydrogen_limits["raw_min"] == pytest.approx(6.5)
    assert hydrogen_limits["raw_max"] == pytest.approx(8.5)

    # Identity-transformed parameters retain their values.
    assert parameters["alkalinity"]["min"] == pytest.approx(20.0)
    assert parameters["alkalinity"]["max"] == pytest.approx(100.0)

    assert parameters["turbidity"]["min"] == pytest.approx(0.0)
    assert parameters["turbidity"]["max"] == pytest.approx(8.0)

    # The input dictionary must remain unchanged.
    assert "pH" in quality_limits["parameters"]
    assert (
        "hydrogen_ion_concentration"
        not in quality_limits["parameters"]
    )


# ---------------------------------------------------------------------------
# Source quality transformation tests
# ---------------------------------------------------------------------------

def test_source_quality_values_are_transformed_correctly():
    """
    Source pH must become hydrogen-ion concentration, while raw pH,
    alkalinity, and turbidity are retained in their correct forms.
    """
    source_records = {
        "silvan_reservoir": {
            "representative_ph": 7.2,
            "representative_alkalinity_mg_l_caco3": 45.0,
            "representative_turbidity_ntu": 2.0,
        }
    }

    result = transform_source_quality_values(source_records)
    source_quality = result["silvan_reservoir"]

    assert source_quality[
        "hydrogen_ion_concentration"
    ] == pytest.approx(10 ** -7.2)

    assert source_quality["raw_ph"] == pytest.approx(7.2)
    assert source_quality["alkalinity"] == pytest.approx(45.0)
    assert source_quality["turbidity"] == pytest.approx(2.0)


def test_missing_source_quality_value_is_rejected():
    """
    A source cannot be transformed when a required quality measurement
    is missing.
    """
    source_records = {
        "silvan_reservoir": {
            "representative_ph": 7.2,
            "representative_alkalinity_mg_l_caco3": 45.0,
            # representative_turbidity_ntu is missing
        }
    }

    with pytest.raises(
        QualityTransformationError,
        match="missing required quality values",
    ):
        transform_source_quality_values(source_records)


def test_negative_turbidity_is_rejected():
    """
    Negative turbidity is not valid under the current model contract.
    """
    source_records = {
        "silvan_reservoir": {
            "representative_ph": 7.2,
            "representative_alkalinity_mg_l_caco3": 45.0,
            "representative_turbidity_ntu": -1.0,
        }
    }

    with pytest.raises(
        QualityTransformationError,
        match="Turbidity.*cannot be negative",
    ):
        transform_source_quality_values(source_records)