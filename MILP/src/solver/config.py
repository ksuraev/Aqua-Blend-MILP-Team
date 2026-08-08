"""Configuration for solving AquaBlend PuLP models with HiGHS."""

from __future__ import annotations

import math
from dataclasses import dataclass
from numbers import Real

import pulp


@dataclass(frozen=True)
class SolverConfig:
    """Supported HiGHS settings for the AquaBlend optimisation model."""

    message: bool = False
    time_limit_seconds: float | None = None
    relative_mip_gap: float | None = None
    threads: int | None = None
    random_seed: int | None = None

    def validate(self) -> None:
        """Reject invalid configuration before starting the solver."""

        if not isinstance(self.message, bool):
            raise TypeError("message must be a boolean.")
        _validate_optional_positive_number(
            self.time_limit_seconds,
            "time_limit_seconds",
        )
        _validate_optional_nonnegative_number(
            self.relative_mip_gap,
            "relative_mip_gap",
        )
        if self.relative_mip_gap is not None and self.relative_mip_gap > 1:
            raise ValueError("relative_mip_gap cannot exceed 1.")
        if self.threads is not None:
            if isinstance(self.threads, bool) or not isinstance(self.threads, int):
                raise TypeError("threads must be an integer or None.")
            if self.threads <= 0:
                raise ValueError("threads must be greater than zero.")
        if self.random_seed is not None:
            if isinstance(self.random_seed, bool) or not isinstance(
                self.random_seed, int
            ):
                raise TypeError("random_seed must be an integer or None.")
            if self.random_seed < 0:
                raise ValueError("random_seed cannot be negative.")

    def create_solver(self) -> pulp.HiGHS:
        """Create a configured PuLP HiGHS solver instance."""

        self.validate()
        solver_parameters: dict[str, object] = {}
        if self.random_seed is not None:
            solver_parameters["random_seed"] = self.random_seed

        return pulp.HiGHS(
            msg=self.message,
            timeLimit=self.time_limit_seconds,
            gapRel=self.relative_mip_gap,
            threads=self.threads,
            **solver_parameters,
        )


def _validate_optional_positive_number(value: object, field_name: str) -> None:
    if value is None:
        return
    numeric_value = _finite_number(value, field_name)
    if numeric_value <= 0:
        raise ValueError(f"{field_name} must be greater than zero.")


def _validate_optional_nonnegative_number(value: object, field_name: str) -> None:
    if value is None:
        return
    numeric_value = _finite_number(value, field_name)
    if numeric_value < 0:
        raise ValueError(f"{field_name} cannot be negative.")


def _finite_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{field_name} must be numeric or None.")
    numeric_value = float(value)
    if not math.isfinite(numeric_value):
        raise ValueError(f"{field_name} must be finite.")
    return numeric_value
