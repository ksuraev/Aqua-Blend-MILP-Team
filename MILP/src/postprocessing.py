"""
This script is used for post processing after the solver is run.

- Extracts the solver status, objective value, active sources and draw values
- Cleans results and checks for Nones, near-zero values and invalid values.
- Calculates the total delivered volume and source level cost contributions
- Calculates blended turbidity and blended alkalinity after solving
- Calculates blended hydrogen-ion concentration and recovers the final blended pH
- performs validition on solver output to ensure that the objective matches the source/plant/zone output

For the purpose of the toy model, we are assuming linear blending.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

try:
    from .contracts import (
        ScenarioData,
        BlendedQualityResult,
        CostBreakdown,
        DemandZoneResult,
        PlantResult,
        PlantZoneFlowResult,
        SolvedScenario,
        SourcePlantFlowResult,
        SourceResult,
    )
    from .preprocessing import ModelParameters
except ImportError:
    from contracts import (
        ScenarioData,
        BlendedQualityResult,
        CostBreakdown,
        DemandZoneResult,
        PlantResult,
        PlantZoneFlowResult,
        SolvedScenario,
        SourcePlantFlowResult,
        SourceResult,
    )
    from preprocessing import ModelParameters


class PostprocessingError(RuntimeError):
    """Raised when a solved PuLP model cannot be converted into a SolvedScenario."""


_EPSILON = 1e-9
# Looser than the preprocessing epsilon: this tolerance absorbs ordinary
# solver noise (e.g. 1e-11 instead of an exact 0.0), not modelling error.
_SOLVER_TOLERANCE = 1e-6
_COST_RECONCILIATION_TOLERANCE = 1e-3

_SUPPORTED_QUALITY_INVERSE_TRANSFORMS = {
    "identity": lambda value: value,
    "ph_to_hydrogen_ion": lambda value: -math.log10(value) if value > 0 else math.nan,
}


@dataclass(frozen=True, slots=True)
class SolverVariables:
    """Class for the decision variable objects produced by the solver.
    Values can be PuLP ``LpVariable`` objects or plain numbers (for testing purposes).
    """

    source_active: dict[str, Any]
    plant_active: dict[str, Any]
    source_withdrawal: dict[str, Any]
    source_plant_flow: dict[tuple[str, str], Any]
    plant_zone_flow: dict[tuple[str, str], Any]


def hydrogen_concentration_to_ph(concentration: float) -> float:
    """Convert hydrogen-ion concentration in mol/L back into pH.

    Inverse of ``preprocessing.ph_to_hydrogen_ion``.
    """
    if not math.isfinite(concentration) or concentration <= 0:
        raise PostprocessingError(
            f"Hydrogen-ion concentration must be finite and positive, got {concentration!r}."
        )

    value = -math.log10(concentration)
    if not math.isfinite(value):
        raise PostprocessingError(
            f"Hydrogen-ion concentration {concentration!r} could not be converted "
            "back to pH safely."
        )
    return value


def _variable_value(variable: Any, label: str) -> float | None:
    """Extract a raw solved value from either a PuLP variable or a plain number."""
    if isinstance(variable, (int, float)):
        return float(variable)

    value_method = getattr(variable, "value", None)
    if callable(value_method):
        return value_method()

    try:
        import pulp

        return pulp.value(variable)
    except ImportError as exc:
        raise PostprocessingError(
            f"Could not read a solved value for {label}: pulp is not installed "
            "and the variable is not a plain number."
        ) from exc


def _clean_flow_value(
    variable: Any,
    label: str,
    warnings: list[str],
) -> float:
    """Read one solved flow/withdrawal value and apply solver-tolerance cleaning."""
    raw = _variable_value(variable, label)

    if raw is None:
        warnings.append(f"{label} was unset by the solver (None); treated as 0.0.")
        return 0.0

    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise PostprocessingError(
            f"{label} solved value {raw!r} could not be converted to a float."
        ) from exc

    if not math.isfinite(value):
        raise PostprocessingError(f"{label} solved to a non-finite value: {value!r}.")

    if value < -_SOLVER_TOLERANCE:
        raise PostprocessingError(
            f"{label} solved to an invalid negative value ({value:g}); "
            "expected a non-negative flow or withdrawal."
        )
    if value < 0:
        # Within tolerance of zero but not exactly zero: solver noise.
        value = 0.0
    if abs(value) < _SOLVER_TOLERANCE:
        value = 0.0

    return value


def _clean_binary_value(
    variable: Any,
    label: str,
    warnings: list[str],
) -> bool:
    """Read one solved activation value and snap it to a boolean within tolerance."""
    raw = _variable_value(variable, label)

    if raw is None:
        warnings.append(f"{label} was unset by the solver (None); treated as inactive.")
        return False

    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise PostprocessingError(
            f"{label} solved value {raw!r} could not be converted to a float."
        ) from exc

    if not math.isfinite(value):
        raise PostprocessingError(f"{label} solved to a non-finite value: {value!r}.")

    if abs(value) <= _SOLVER_TOLERANCE:
        return False
    if abs(value - 1.0) <= _SOLVER_TOLERANCE:
        return True

    raise PostprocessingError(
        f"{label} solved to {value!r}, which is not within tolerance of 0 or 1."
    )


def _extract_solver_status(problem: Any) -> tuple[str, float | None]:
    """Read the solver status label and objective value from a PuLP problem."""
    try:
        import pulp
    except ImportError as exc:
        raise PostprocessingError(
            'Missing dependency "pulp". Install it with: pip install pulp'
        ) from exc

    status = pulp.LpStatus.get(problem.status, str(problem.status))
    objective_value = pulp.value(problem.objective)
    if objective_value is not None:
        objective_value = float(objective_value)
    return status, objective_value

