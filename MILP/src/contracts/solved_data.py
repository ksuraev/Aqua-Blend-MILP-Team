from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SourceResult:
    """Solved decision and cost outcome for one water source.

    Nb: ``blend_ratio`` is ``None`` when no water was delivered, to avoid a divide by zero.
    """

    source_id: str
    is_selected: bool
    withdrawal_ml_per_day: float
    blend_ratio: float | None
    fixed_cost_contribution: float
    withdrawal_cost_contribution: float


@dataclass(frozen=True, slots=True)
class PlantResult:
    """Solved activation and cost for one treatment plant."""

    plant_id: str
    is_active: bool
    throughput_ml_per_day: float
    fixed_cost_contribution: float
    treatment_cost_contribution: float


@dataclass(frozen=True, slots=True)
class DemandZoneResult:
    """Solved delivery for one demand zone."""

    zone_id: str
    demand_ml_per_day: float
    delivered_ml_per_day: float


@dataclass(frozen=True, slots=True)
class SourcePlantFlowResult:
    """Solved flow on one source-to-plant arc."""

    source_id: str
    plant_id: str
    is_active: bool
    flow_ml_per_day: float


@dataclass(frozen=True, slots=True)
class PlantZoneFlowResult:
    """Solved flow on one plant-to-zone arc."""

    plant_id: str
    zone_id: str
    is_active: bool
    flow_ml_per_day: float


@dataclass(frozen=True, slots=True)
class BlendedQualityResult:
    """Solved water-quality outcome for one parameter at one plant's inflow.

    Values and limits are reported in the scenario's original (raw) units, 
     e.g. pH rather than the hydrogen-ion concentration.
    """

    plant_id: str
    parameter_id: str
    unit: str
    blended_value: float
    lower_limit: float
    upper_limit: float
    lower_margin: float
    upper_margin: float
    is_binding: bool


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """Solved total cost, split by contribution, plus validity check.

    The validity check sums up the individual solved source and plant amounts
    to make sure it matches the outputted objective value.
    """

    total_source_fixed_cost: float
    total_source_withdrawal_cost: float
    total_plant_fixed_cost: float
    total_plant_treatment_cost: float
    reconstructed_total_cost: float
    solver_objective_value: float | None
    cost_reconciles: bool


@dataclass(frozen=True, slots=True)
class SolvedScenario:
    """Validated output contract for a single scenario."""

    scenario_id: str

    solver_status: str
    objective_value: float | None

    sources: tuple[SourceResult, ...]
    plants: tuple[PlantResult, ...]
    demand_zones: tuple[DemandZoneResult, ...]

    source_to_plant_flows: tuple[SourcePlantFlowResult, ...]
    plant_to_zone_flows: tuple[PlantZoneFlowResult, ...]

    blended_quality: tuple[BlendedQualityResult, ...]
    cost_breakdown: CostBreakdown

    warnings: tuple[str, ...]

    @property
    def is_optimal(self) -> bool:
        """Return True when the solver reported an optimal solution."""
        return self.solver_status == "Optimal"

    @property
    def selected_sources(self) -> tuple[SourceResult, ...]:
        """Return sources the solver activated."""
        return tuple(source for source in self.sources if source.is_selected)

    @property
    def unused_sources(self) -> tuple[SourceResult, ...]:
        """Return sources the solver left inactive."""
        return tuple(source for source in self.sources if not source.is_selected)

    @property
    def active_plants(self) -> tuple[PlantResult, ...]:
        """Return treatment plants the solver activated."""
        return tuple(plant for plant in self.plants if plant.is_active)

    @property
    def inactive_plants(self) -> tuple[PlantResult, ...]:
        """Return treatment plants the solver left inactive."""
        return tuple(plant for plant in self.plants if not plant.is_active)

    @property
    def active_source_to_plant_flows(self) -> tuple[SourcePlantFlowResult, ...]:
        """Return source-to-plant transfer paths with non-zero flow."""
        return tuple(flow for flow in self.source_to_plant_flows if flow.is_active)

    @property
    def active_plant_to_zone_flows(self) -> tuple[PlantZoneFlowResult, ...]:
        """Return plant-to-zone transfer paths with non-zero flow."""
        return tuple(flow for flow in self.plant_to_zone_flows if flow.is_active)

    @property
    def binding_quality_limits(self) -> tuple[BlendedQualityResult, ...]:
        """Return blended-quality results sitting at or against a limit."""
        return tuple(result for result in self.blended_quality if result.is_binding)


__all__ = [
    "BlendedQualityResult",
    "CostBreakdown",
    "DemandZoneResult",
    "PlantResult",
    "PlantZoneFlowResult",
    "SolvedScenario",
    "SourcePlantFlowResult",
    "SourceResult",
]