"""
Water quality transformation utilities for the AquaBlend MILP model. This module prepares water quality data before it is passed to the
optimisation model.The Sprint 1 model currently handles three quality parameters: pH, alkalinity and turbidity.

Alkalinity and turbidity use the identity transformation because they are approximated as volume weighted linear blends.

Raw pH cannot be averaged directly because pH is logarithmic. Therefore, pH values are transformed into hydrogen-ion concentration before
optimisation:
    [H+] = 10 ** (-pH)

After optimisation, the blended hydrogen-ion concentration can be converted back into pH:
    pH = -log10([H+])

This module validates water quality inputs and converts them into the numeric form required by the optimisation model. It does not create PuLP
decision variables, add mathematical constraints, or run the solver.
"""

from __future__ import annotations
import math
from copy import deepcopy
from numbers import Real
from typing import Any, Mapping

# ---------------------------------------
# Constants
# ---------------------------------------

# The scenario input contract currently uses these transformation names.
# Identity keeps the original quality value unchanged, as used for turbidity and alkalinity.
# Hydrogenic converts raw pH into hydrogen-ion concentration before optimisation.

IDENTITY_TRANSFORM = "identity"
HYDROGENIC_TRANSFORM = "hydrogenic"

SUPPORTED_TRANSFORMS = {
    IDENTITY_TRANSFORM,
    HYDROGENIC_TRANSFORM,
}

# pH normally lies between 0 and 14. 

MINIMUM_VALID_PH = 0.0
MAXIMUM_VALID_PH = 14.0

# --------------------------------------------
# Custom exception
# --------------------------------------------

class QualityTransformationError(ValueError):
    """
    Raised when water quality data cannot be transformed safely. A separate exception type makes transformation errors easier to
    distinguish from database, scenario loading, and solver errors.
    """
# ---------------------------------------------
# Internal validation helpers
# ---------------------------------------------
"""
    Convert a numeric input to a finite float.
    Parameters
        value: Value to validate and convert.
        field_name: Human readable field name used in error messages.

    Returns
        float : Validated finite numeric value.

    Raises
        TypeError : If the value is not numeric.

    QualityTransformationError
        If the value is NaN or infinite.

    Boolean values are rejected explicitly. In Python, bool is a subclass of int, which means True and False would otherwise be accepted as 1 and
    0. That behaviour is inappropriate for water quality measurements.
    """

def _to_finite_float(
    value: Any,
    *,
    field_name: str,
) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(
            f"{field_name} must be numeric; "
            f"received {type(value).__name__}."
        )

    numeric_value = float(value)

    if not math.isfinite(numeric_value):
        raise QualityTransformationError(
            f"{field_name} must be finite; "
            f"received {numeric_value}."
        )

    return numeric_value

"""
    Validate an ordinary lower and upper quality bound. This helper is used for identity transformed parameters such as
    alkalinity and turbidity.  The * makes parameter_name keyword-only, so it must be passed explicitly as 
    parameter_name="turbidity" instead of as an unnamed positional argument.

    Returns
        tuple[float, float]
        Validated minimum and maximum values.
"""

def _validate_ordered_bounds(
    minimum: Any,
    maximum: Any,
    *,
    parameter_name: str,
) -> tuple[float, float]:
    minimum_value = _to_finite_float(
        minimum,
        field_name=f"{parameter_name} minimum",
    )

    maximum_value = _to_finite_float(
        maximum,
        field_name=f"{parameter_name} maximum",
    )

    if minimum_value > maximum_value:
        raise QualityTransformationError(
            f"The minimum limit for '{parameter_name}' cannot exceed "
            f"its maximum limit: minimum={minimum_value}, "
            f"maximum={maximum_value}."
        )

    return minimum_value, maximum_value

# -----------------------------------------
# pH transformation functions
# -----------------------------------------

"""
    Convert a raw pH value to hydrogen-ion concentration.
    The conversion is: [H+] = 10 ** (-pH)

    Parameters
    ph_value: Raw pH value.

    Returns
    float : Hydrogen-ion concentration represented as 10 ** (-pH).

    Raises
    TypeError : If the supplied pH value is not numeric.

    QualityTransformationError
        If the value is not finite or is outside the accepted pH range.

"""
def ph_to_hydrogen_concentration(ph_value: Any) -> float:
    
    ph = _to_finite_float(
        ph_value,
        field_name="pH value",
    )

    if not MINIMUM_VALID_PH <= ph <= MAXIMUM_VALID_PH:
        raise QualityTransformationError(
            f"pH value must be between {MINIMUM_VALID_PH} and "
            f"{MAXIMUM_VALID_PH}; received {ph}."
        )

    return 10.0 ** (-ph)

"""
    Recover pH from hydrogen-ion concentration.
    The conversion is: pH = -log10([H+])
    This function is mainly intended for post-processing after the solver has produced the optimal source flows.

    Parameters
       concentration: Positive hydrogen-ion concentration.

    Returns
       float: Recovered pH value.

    Raises
      TypeError: If the concentration is not numeric.

    QualityTransformationError
        If the concentration is non-finite, zero, or negative.
"""

def hydrogen_concentration_to_ph(
    concentration: Any,
) -> float:
    
    concentration_value = _to_finite_float(
        concentration,
        field_name="Hydrogen-ion concentration",
    )

    # log10 is undefined for zero and negative values.
    if concentration_value <= 0.0:
        raise QualityTransformationError(
            "Hydrogen-ion concentration must be greater than zero; "
            f"received {concentration_value}."
        )

    return -math.log10(concentration_value)

"""
    Convert raw pH limits into ordered hydrogen-ion concentration limits.
    Suppose the acceptable raw pH range is: minimum_pH <= pH <= maximum_pH

    Hydrogen-ion concentration changes in the opposite direction to pH. Therefore the 
    bounds must be reversed during transformation.
        minimum_H = 10 ** (-maximum_pH)
        maximum_H = 10 ** (-minimum_pH)
   
    Parameters
       minimum_ph: Lower acceptable pH limit.
       maximum_ph: Upper acceptable pH limit.

    Returns 
       tuple[float, float]
        A tuple containing: minimum hydrogen-ion concentration and maximum hydrogen-ion concentration

    Raises
        QualityTransformationError: If the raw pH limits are invalid or incorrectly ordered.
"""
def transform_ph_bounds(
    minimum_ph: Any,
    maximum_ph: Any,
) -> tuple[float, float]:
    
    minimum_ph_value = _to_finite_float(
        minimum_ph,
        field_name="Minimum pH limit",
    )
    maximum_ph_value = _to_finite_float(
        maximum_ph,
        field_name="Maximum pH limit",
    )

    # Validate each bound through the standard pH conversion function.
    # This also confirms that both values lie between 0 and 14.
    ph_to_hydrogen_concentration(minimum_ph_value)
    ph_to_hydrogen_concentration(maximum_ph_value)

    if minimum_ph_value > maximum_ph_value:
        raise QualityTransformationError(
            "Minimum pH cannot exceed maximum pH: "
            f"minimum={minimum_ph_value}, "
            f"maximum={maximum_ph_value}."
        )

    # The maximum raw pH produces the minimum hydrogen concentration.
    minimum_concentration = ph_to_hydrogen_concentration(maximum_ph_value)

    # The minimum raw pH produces the maximum hydrogen concentration.
    maximum_concentration = ph_to_hydrogen_concentration(minimum_ph_value)

    return minimum_concentration, maximum_concentration


# ---------------------------------------------------------------------------
# Quality limit transformation
# ---------------------------------------------------------------------------

"""
Identity transformed parameters, such as alkalinity and turbidity, keep their original parameter names, units, and numeric limits.

Raw pH limits are converted into hydrogen-ion concentration limits because pH is 
logarithmic and cannot be blended directly in the linear model. The transformed values 
are stored under the key "hydrogen_ion_concentration", while the original pH limits are retained as
metadata for reporting, validation, and debugging.

A deep copy of the input is used, so the original scenario dictionary is not modified.

Parameters
quality_limits: The quality_limits section from the scenario data.

Returns
dict[str, Any] 
A dictionary containing validated, model ready quality limits.

Raises
TypeError : If quality_limits or one of its required sections has an invalid type.

QualityTransformationError
    If required fields are missing, a transform is unsupported, or a quality limit contains an invalid value.
"""

def transform_quality_limits(
    quality_limits: Mapping[str, Any],
) -> dict[str, Any]:
   
    if not isinstance(quality_limits, Mapping):
        raise TypeError(
            "quality_limits must be a mapping or dictionary."
        )

    transformed_limits = deepcopy(dict(quality_limits))

    parameters = transformed_limits.get("parameters")

    if not isinstance(parameters, Mapping):
        raise QualityTransformationError(
            "quality_limits.parameters must be an object."
        )

    if not parameters:
        raise QualityTransformationError(
            "quality_limits.parameters cannot be empty."
        )

    # Build a new dictionary because the pH input parameter is renamed
    # to hydrogen_ion_concentration after transformation.
    transformed_parameters: dict[str, dict[str, Any]] = {}

    for parameter_name, specification in parameters.items():
        if not isinstance(parameter_name, str):
            raise QualityTransformationError(
                "Every quality parameter name must be a string."
            )

        if not isinstance(specification, Mapping):
            raise QualityTransformationError(
                f"Quality parameter '{parameter_name}' must be "
                "defined as an object."
            )

        # Convert the mapping into a normal dictionary so it can be copied and updated safely.
        specification_copy = dict(specification)

        # Read the requested transformation. If it is not supplied,
        # keep the original values using the identity transformation.
        transform_name = specification_copy.get(
            "transform",
            IDENTITY_TRANSFORM,
        )

        if not isinstance(transform_name, str):
            raise QualityTransformationError(
                f"The transform for '{parameter_name}' must be a string."
            )

        transform_name = transform_name.strip()

        if transform_name not in SUPPORTED_TRANSFORMS:
            supported = ", ".join(sorted(SUPPORTED_TRANSFORMS))

            raise QualityTransformationError(
                f"Unsupported transform '{transform_name}' for "
                f"quality parameter '{parameter_name}'. "
                f"Supported transforms are: {supported}."
            )

        if "min" not in specification_copy:
            raise QualityTransformationError(
                f"Quality parameter '{parameter_name}' is missing "
                "the required 'min' limit."
            )

        if "max" not in specification_copy:
            raise QualityTransformationError(
                f"Quality parameter '{parameter_name}' is missing "
                "the required 'max' limit."
            )

        raw_minimum = specification_copy["min"]
        raw_maximum = specification_copy["max"]

        if transform_name == IDENTITY_TRANSFORM:
            minimum_value, maximum_value = _validate_ordered_bounds(
                raw_minimum,
                raw_maximum,
                parameter_name=parameter_name,
            )

            transformed_parameters[parameter_name] = {
                **specification_copy,
                "min": minimum_value,
                "max": maximum_value,
                "transform": IDENTITY_TRANSFORM,
            }

        elif transform_name == HYDROGENIC_TRANSFORM:
            # The hydrogenic transformation is currently only valid for pH.
            if parameter_name.strip().lower() != "ph":
                raise QualityTransformationError(
                    f"The '{HYDROGENIC_TRANSFORM}' transform is only "
                    f"supported for pH; received '{parameter_name}'."
                )

            minimum_hydrogen, maximum_hydrogen = transform_ph_bounds(
                raw_minimum,
                raw_maximum,
            )

            raw_minimum_ph = _to_finite_float(
                raw_minimum,
                field_name="Minimum pH limit",
            )

            raw_maximum_ph = _to_finite_float(
                raw_maximum,
                field_name="Maximum pH limit",
            )

            transformed_parameters[
                "hydrogen_ion_concentration"
            ] = {
                "unit": "mol/L",
                "min": minimum_hydrogen,
                "max": maximum_hydrogen,
                "transform": HYDROGENIC_TRANSFORM,

                # Record where this transformed parameter came from.
                "source_parameter": "pH",

                # Retain the original pH limits for reports and validation.
                "raw_min": raw_minimum_ph,
                "raw_max": raw_maximum_ph,
                "raw_unit": specification_copy.get(
                    "unit",
                    "pH",
                ),
            }

    transformed_limits["parameters"] = transformed_parameters

    return transformed_limits

# ---------------------------------------------------------------------------
# Source-quality transformation
# ---------------------------------------------------------------------------

"""
    Validate and transform source water quality values for the MILP model.
    Each source record is expected to contain: representative_ph, representative_alkalinity_mg_l_caco3, representative_turbidity_ntu 

    Raw pH is converted into hydrogen-ion concentration because pH is logarithmic and should not be blended directly. The original pH value
    is also retained for reporting, validation, and debugging.

    Alkalinity and turbidity use the identity transformation, so their validated values are passed through without mathematical conversion.

    Parameters
    source_records:
        Mapping of source IDs to their database loaded quality records.

    Returns
    dict[str, dict[str, float]]
    Model ready quality values for each source.
"""

def transform_source_quality_values(
    source_records: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, float]]:
    
    # Confirm that the input is a dictionary like collection of sources.
    if not isinstance(source_records, Mapping):
        raise TypeError(
            "source_records must be a mapping from source IDs "
            "to source records."
        )

    # At least one source is required to prepare model quality parameters.
    if not source_records:
        raise QualityTransformationError(
            "No source records were provided for transformation."
        )

    transformed_sources: dict[str, dict[str, float]] = {}

    for source_id, source_record in source_records.items():
        # Each source must have a valid, non-empty string identifier.
        if not isinstance(source_id, str) or not source_id.strip():
            raise QualityTransformationError(
                "Every source record must have a non-empty string ID."
            )

        cleaned_source_id = source_id.strip()

        # Each source record must behave like a dictionary.
        if not isinstance(source_record, Mapping):
            raise QualityTransformationError(
                f"Source record '{cleaned_source_id}' must be an object."
            )

        # These fields are required to construct the three model quality
        # parameters currently used by AquaBlend.
        required_fields = {
            "representative_ph",
            "representative_alkalinity_mg_l_caco3",
            "representative_turbidity_ntu",
        }

        # A field is treated as missing when it does not exist or its supplied value is None.
        missing_fields = sorted(
            field_name
            for field_name in required_fields
            if source_record.get(field_name) is None
        )

        if missing_fields:
            raise QualityTransformationError(
                f"Source '{cleaned_source_id}' is missing required "
                f"quality values: {', '.join(missing_fields)}."
            )

        # Validate and convert the raw pH value into a finite float.
        raw_ph = _to_finite_float(
            source_record["representative_ph"],
            field_name=f"pH for source '{cleaned_source_id}'",
        )

        # Convert raw pH into hydrogen-ion concentration for use by the linear water quality constraint.
        hydrogen_ion_concentration = (
            ph_to_hydrogen_concentration(raw_ph)
        )

        # Alkalinity uses the identity transformation, so it is validated and converted to float without mathematical transformation.
        alkalinity = _to_finite_float(
            source_record[
                "representative_alkalinity_mg_l_caco3"
            ],
            field_name=(
                f"Alkalinity for source '{cleaned_source_id}'"
            ),
        )

        # Turbidity also uses the identity transformation.
        turbidity = _to_finite_float(
            source_record["representative_turbidity_ntu"],
            field_name=(
                f"Turbidity for source '{cleaned_source_id}'"
            ),
        )

        # Negative values are invalid for the current alkalinity and turbidity input contract.
        if alkalinity < 0.0:
            raise QualityTransformationError(
                f"Alkalinity for source '{cleaned_source_id}' cannot "
                f"be negative; received {alkalinity}."
            )

        if turbidity < 0.0:
            raise QualityTransformationError(
                f"Turbidity for source '{cleaned_source_id}' cannot "
                f"be negative; received {turbidity}."
            )

        transformed_sources[cleaned_source_id] = {
            # This is the model facing value used in the generic water quality constraint instead of raw pH.
            "hydrogen_ion_concentration": (
                hydrogen_ion_concentration
            ),

            # Retain raw pH for readable reports, validation, and debugging.
            # This value should not be blended directly in the MILP.
            "raw_ph": raw_ph,

            # Identity transformed quality values.
            "alkalinity": alkalinity,
            "turbidity": turbidity,
        }

    return transformed_sources