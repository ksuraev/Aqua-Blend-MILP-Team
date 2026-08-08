"""Processed input structures used by the modular AquaBlend solver."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class ReservoirInput:
    """Optimisation-ready values for one water source."""

    name: str
    minimum_flow_if_active: float
    max_flow: float
    variable_cost: float
    fixed_activation_cost: float
    quality: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class DemandInput:
    """Total water demand represented in ML/day."""

    required_volume: float


@dataclass(frozen=True)
class PlantInput:
    """Simplified single-plant values for the Sprint 1 toy model."""

    name: str
    capacity: float
    minimum_throughput: float = 0.0
    treatment_cost: float = 0.0


@dataclass(frozen=True)
class SolverInput:
    """Validated input expected by model, variables, objective and constraints."""

    reservoirs: tuple[ReservoirInput, ...]
    demand: DemandInput
    plant: PlantInput
    maximum_active_reservoirs: int | None = None

    @classmethod
    def from_model_parameters(cls, parameters: Any) -> "SolverInput":
        """Adapt Archit's formulation-ready ``ModelParameters`` output.

        Source minimum withdrawal currently defaults to zero when the
        preprocessing object does not expose ``source_min_withdrawal``.
        """

        source_ids = tuple(parameters.source_ids)
        if not source_ids:
            raise ValueError("At least one source is required.")

        source_minimum = getattr(parameters, "source_min_withdrawal", {})
        reservoirs = tuple(
            ReservoirInput(
                name=source_id,
                minimum_flow_if_active=float(source_minimum.get(source_id, 0.0)),
                max_flow=float(parameters.source_max_withdrawal[source_id]),
                variable_cost=float(parameters.source_unit_cost[source_id]),
                fixed_activation_cost=float(parameters.source_fixed_cost[source_id]),
                quality={
                    quality_id: float(parameters.source_quality[(source_id, quality_id)])
                    for quality_id in parameters.quality_parameter_ids
                },
            )
            for source_id in source_ids
        )

        plant_ids = tuple(parameters.plant_ids)
        if len(plant_ids) != 1:
            raise ValueError("The Sprint 1 toy solver requires exactly one plant.")
        plant_id = plant_ids[0]

        return cls(
            reservoirs=reservoirs,
            demand=DemandInput(
                required_volume=float(sum(parameters.demand_by_zone.values()))
            ),
            plant=PlantInput(
                name=plant_id,
                capacity=float(parameters.plant_max_throughput[plant_id]),
                minimum_throughput=float(
                    getattr(parameters, "plant_min_throughput", {}).get(
                        plant_id,
                        0.0,
                    )
                ),
                treatment_cost=float(
                    parameters.plant_unit_treatment_cost[plant_id]
                ),
            ),
        )
