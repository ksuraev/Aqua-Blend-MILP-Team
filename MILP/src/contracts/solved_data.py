from __future__ import annotations

from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class DataSource:
    """The data source for a scenario"""

    type: str
    view: str
    allow_estimated_values: bool | None


@dataclass(frozen=True, slots=True)
class InputScenario:
    """Information about the scenario that was entered into the solver."""

    scenario_id: str
    status: str
    data_source: DataSource


@dataclass(frozen=True, slots=True)
class InputValidationPolicy:
    """A summary of the validation policy inputted to the solver"""

    fail_if_source_missing_from_database: bool
    fail_if_daily_availability_missing: bool
    fail_if_required_quality_value_missing: bool
    fail_if_demand_missing: bool


@dataclass(frozen=True, slots=True)
class ValidationCheck:
    """A summary of a single validation check"""

    check: str
    enabled: bool
    passed: bool


@dataclass(frozen=True, slots=True)
class LoaderValidation:
    """The outcome of validating the data loader step before solving"""

    status: str
    scenario_ready: bool
    validation_issues: tuple[str, ...]
    checks: tuple[ValidationCheck, ...]


@dataclass(frozen=True, slots=True)
class PreprocessingValidation:
    """The outcome of validating the preprocessing step before solving – mathematical-readiness"""

    status: str
    warnings: tuple[str, ...]
    checks: tuple[ValidationCheck, ...]


@dataclass(frozen=True, slots=True)
class OutputValidation:
    """The result of validating that solver outputs are internally consistent"""

    status: str
    tolerance: float
    checks: tuple[ValidationCheck, ...]


@dataclass(frozen=True, slots=True)
class SolutionValidation:
    """A summary of the validation at all steps of the solver: loading, preprocessing and solver output"""

    input_policy: InputValidationPolicy
    loader: LoaderValidation
    preprocessing: PreprocessingValidation
    output_consistency: OutputValidation


@dataclass(frozen=True, slots=True)
class SolverSummary:
    """A summary of the status of the solver and its objective value"""
    
    status: str
    is_feasible: bool
    is_optimal: bool
    objective_value: float | None
    version: str


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """Solved total cost, split by contribution, plus validity check.

    The validity check sums up the individual solved source and plant amounts
    to make sure it matches the outputted objective value.
    """

    total_source_fixed_cost: float
    total_source_variable_cost: float
    total_plant_fixed_cost: float
    total_plant_variable_cost: float
    reconstructed_total_cost: float
    total_cost: float
    cost_reconciles: bool


@dataclass(frozen=True, slots=True)
class CostSummary:
    """Summary of the model output for consumers that do not need every model entity."""

    total_demand_ml_per_day: float
    total_withdrawal_ml_per_day: float
    total_treated_ml_per_day: float
    total_delivered_ml_per_day: float
    selected_source_count: int
    active_plant_count: int
    costs: CostBreakdown

@dataclass(frozen=True, slots=True)
class SourceDecisionEvidence:
    """
    Evidence to provide additional information about why a source was activated or not,
    and why a certain amount of water was drawn from it
    """

    unit_cost_rank: int | None
    binding_lower: bool | None
    binding_upper: bool | None


@dataclass(frozen=True, slots=True)
class SourceResult:
    """Solved decision and cost outcome for one water source.

    Nb: blend_ratio is None when no water was delivered, to avoid a divide by zero.
    """

    source_id: str
    model_included: bool
    activated: bool
    withdrawal_ml_per_day: float
    utilisation_percent: float
    blend_ratio: float | None
    variable_withdrawal_cost: float
    total_source_cost: float
    selection_status: str
    exclusion_reason_code: str
    decision_evidence: SourceDecisionEvidence


@dataclass(frozen=True, slots=True)
class PlantResult:
    """Solved activation and cost for one treatment plant."""
    
    plant_id: str
    activated: bool
    throughput_ml_per_day: float
    utilisation_percent: float
    variable_treatment_cost: float
    total_plant_cost: float


@dataclass(frozen=True, slots=True)
class DemandZoneResult:
    """Solved delivery for one demand zone."""
    
    zone_id: str
    delivered_ml_per_day: float
    surplus_ml_per_day: float
    demand_satisfied: bool
    demand_must_be_met: bool
    unmet_demand_ml_per_day: float


@dataclass(frozen=True, slots=True)
class SourcePlantFlowResult:
    """Solved flow on one source-to-plant arc."""

    source_id: str
    plant_id: str
    activated: bool
    flow_ml_per_day: float
    utilisation_percent: float


@dataclass(frozen=True, slots=True)
class PlantZoneFlowResult:
    """Solved flow on one plant-to-zone arc."""

    plant_id: str
    zone_id: str
    activated: bool
    flow_ml_per_day: float
    utilisation_percent: float


@dataclass(frozen=True, slots=True)
class FlowResult:
    """Contains the solutions flows between sources, plants and demand zones"""

    source_to_plant: tuple[SourcePlantFlowResult, ...]
    plant_to_zone: tuple[PlantZoneFlowResult, ...]


@dataclass(frozen=True, slots=True)
class QualityParameterResult:
    """Solved water-quality outcome for a single parameter.
    (must be associated with a single plant)
    
    Values and limits are reported in the scenario's original (raw) units, 
     e.g. pH rather than the hydrogen-ion concentration.
    """

    parameter_id: str
    model_parameter_id: str
    transform: str
    model_unit: str
    model_value: float
    model_min: float
    model_max: float
    reported_value: float
    reported_unit: str
    within_limits: bool
    binding_upper: bool
    binding_lower: bool


@dataclass(frozen=True, slots=True)
class InflowQualityPlantResult:
    """Solved water-quality outcome for one plant's inflow."""

    plant_id: str
    flow_ml_per_day: float
    parameters: tuple[QualityParameterResult, ...]
    

@dataclass(frozen=True, slots=True)
class QualityResult:
    """Solved water quality results, accounting for future adoption of outflow result"""

    applies_to: str
    plant_inflow: tuple[InflowQualityPlantResult, ...]


@dataclass(frozen=True, slots=True)
class SolvedScenario:
    """Validated output contract for a single scenario."""

    schema_version: str
    run_id: str

    scenario: InputScenario
    validation: SolutionValidation

    solver: SolverSummary
    summary: CostSummary

    sources: tuple[SourceResult, ...]
    plants: tuple[PlantResult, ...]
    demand_zones: tuple[DemandZoneResult, ...]
    flows: FlowResult

    quality: QualityResult

    binding_constraints_summary: tuple[str, ...]

    warnings: tuple[str, ...]

    @property
    def selected_sources(self) -> tuple[SourceResult, ...]:
        """Return sources the solver activated."""
        return tuple(source for source in self.sources if source.activated)

    @property
    def unused_sources(self) -> tuple[SourceResult, ...]:
        """Return sources the solver left inactive."""
        return tuple(source for source in self.sources if not source.activated)

    @property
    def active_plants(self) -> tuple[PlantResult, ...]:
        """Return treatment plants the solver activated."""
        return tuple(plant for plant in self.plants if plant.activated)

    @property
    def inactive_plants(self) -> tuple[PlantResult, ...]:
        """Return treatment plants the solver left inactive."""
        return tuple(plant for plant in self.plants if not plant.activated)

    @property
    def active_source_to_plant_flows(self) -> tuple[SourcePlantFlowResult, ...]:
        """Return source-to-plant transfer paths with non-zero flow."""
        return tuple(flow for flow in self.flows.source_to_plant if flow.activated)

    @property
    def active_plant_to_zone_flows(self) -> tuple[PlantZoneFlowResult, ...]:
        """Return plant-to-zone transfer paths with non-zero flow."""
        return tuple(flow for flow in self.flows.plant_to_zone if flow.activated)
    

__all__ = [
    "DataSource",
    "InputScenario",
    "InputValidationPolicy",
    "ValidationCheck",
    "LoaderValidation",
    "PreprocessingValidation",
    "OutputValidation",
    "SolutionValidation",
    "SolverSummary",
    "QualityResult",
    "CostBreakdown",
    "CostSummary",
    "SourceDecisionEvidence",
    "SourceResult",
    "PlantResult",
    "DemandZoneResult",
    "PlantZoneFlowResult",
    "SourcePlantFlowResult",
    "FlowResult",
    "QualityParameterResult",
    "InflowQualityPlantResult",
    "QualityResult",
    "SolvedScenario",
]