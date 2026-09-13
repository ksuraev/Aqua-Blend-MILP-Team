# AquaBlend `ScenarioData` Contract Guide

## 1. Purpose

`src/contracts/scenario_data.py` defines the canonical data structures exchanged between the AquaBlend data-loading and preprocessing stages.

```text
Scenario JSON + Supabase
          |
          v
    data_loader.py
 loads, normalises and validates
          |
          v
      ScenarioData
 shared scenario input contract
          |
          v
    preprocessing.py
 transforms raw inputs into
      ModelParameters
          |
          v
     constraints.py
```

`ScenarioData` is the boundary between external data sources and the mathematical model. It is not a database schema, JSON schema, PuLP model, solver result, or reporting object.

---

## 2. Design goals

The contract is kept separate from `data_loader.py` so that every component uses one shared definition.

| Goal | How the contract supports it |
|---|---|
| Single source of truth | Loader, preprocessing, tests and future model components import the same classes. |
| Stable interface | Database-specific names are normalised before data enters the contract. |
| Small contract | Repeated or unused fields are excluded until they have an approved model purpose. |
| Extensible quality model | Source quality values are keyed by parameter identifier instead of being hardcoded as dataclass fields. |
| Testability | Tests may construct `ScenarioData` directly without connecting to Supabase. |
| Separation of concerns | Loading, preprocessing, model construction and solving remain separate stages. |
| Auditability | Source readiness, origin and provenance information remain available. |

---

## 3. What belongs in the contract

The contract contains:

- Scenario identity and descriptive metadata
- Source identity, availability bounds, costs and quality values
- Plant capacity bounds and costs
- Demand-zone requirements
- Directed source-to-plant and plant-to-zone links
- Quality-limit definitions
- Loader validation issues
- Data-readiness and provenance metadata

The contract does not contain:

- Database credentials
- SQL queries or connection objects
- Raw PostgreSQL rows
- Scenario file paths
- Model decision variables
- Objective expressions
- Constraints
- Solver configuration or output
- Optimised flows or activation decisions
- Model-specific transformed quality values
- Unimplemented treatment, dosing or batching configuration

A value should only enter this contract when it is a real scenario input with a defined loader source, validation rule and downstream use.

---

## 4. Dataclass configuration

Every public class uses:

```python
@dataclass(frozen=True, slots=True)
```

| Option | Meaning | Reason |
|---|---|---|
| `frozen=True` | Fields cannot be reassigned after construction. | Protects validated input from accidental mutation between pipeline stages. |
| `slots=True` | Only declared attributes may exist. | Prevents accidental fields and reduces per-instance memory. |
| Type annotations | Every field declares an expected type. | Supports code review, editor assistance and static analysis. |
| Tuples for collections | Scenario collections are immutable at the top level. | Preserves ordering while discouraging downstream modification. |

`frozen=True` is not a deep freeze. The `quality`, `quality_limits` and `provenance` dictionaries should therefore be treated as read-only after construction.

---

## 5. Class overview

| Class | Represents | Main downstream use |
|---|---|---|
| `SourceInput` | One candidate raw-water source | Source set, withdrawal bounds, source costs and quality values |
| `PlantInput` | One treatment plant | Plant set, activation costs and throughput bounds |
| `DemandZoneInput` | One delivery zone | Zone set and demand parameter |
| `SourcePlantLinkInput` | One source-to-plant arc | Network topology and source-to-plant link capacity |
| `PlantZoneLinkInput` | One plant-to-zone arc | Network topology and plant-to-zone link capacity |
| `ScenarioData` | One complete scenario | Sole structured input to preprocessing |

---

# 6. `SourceInput`

## 6.1 Purpose

`SourceInput` represents one water source that may supply the network.

The loader is responsible for converting database-specific names into the canonical contract names. For example:

```text
database: max_available_ml_per_day
contract: maximum_withdrawal_ml_per_day
```

Water-quality values are stored in a generic dictionary:

```python
quality: dict[str, float]
```

The keys must use the same identifiers as:

```python
scenario.quality_limits["parameters"]
```

For example:

```python
quality={
    "ph": 7.2,
    "alkalinity": 45.0,
    "turbidity": 1.1,
}
```

#### Source-quality key contract

The keys in `SourceInput.quality` are stable parameter identifiers. They must
exactly match the keys in
`ScenarioData.quality_limits["parameters"]`.

Each quality-parameter definition contains an explicit `id` and a
human-readable `name`. The `id` is used by the loader, preprocessing and model,
while `name` is intended for display purposes. For example, `ph` is the stable
identifier and `pH` is its display name.

Source-quality measurements are loaded from the configured Supabase view or
from inline source rows using each parameter's `source_field`. The scenario
`sources` array remains a source-selection list and does not duplicate the
source measurements.

This same-key requirement prevents the source values and quality limits from drifting apart.

## 6.2 Field reference

| Field | Type | Purpose | Typical origin | Downstream use | Validation notes |
|---|---|---|---|---|---|
| `source_id` | `str` | Stable unique source identifier. | Database and scenario JSON | Builds source set \(S\); keys source parameters and variables | Required and unique within a scenario. |
| `name` | `str` | Human-readable source name. | Database | Logs, warnings and reports | Display only; relationships use `source_id`. |
| `source_type` | `str` | Physical source category such as reservoir or river. | Database | Reporting and possible future source-specific rules | Informational in the current formulation. |
| `enabled` | `bool` | Indicates whether the source is enabled in scenario configuration. | Scenario JSON | Scenario filtering and diagnostics | Disabled sources should be filtered consistently before model construction. |
| `forced_inactive` | `bool` | Explicitly excludes an otherwise available source. | Scenario JSON | Preprocessing omits the source and its outgoing arcs | Useful for outage and what-if scenarios. |
| `minimum_withdrawal_ml_per_day` | `float \| None` | Minimum daily withdrawal when the source is active. | Database or scenario override | Becomes `source_min_withdrawal[source_id]` | Must be finite, non-negative and no greater than the maximum. |
| `maximum_withdrawal_ml_per_day` | `float \| None` | Maximum daily withdrawal. | Database or scenario override | Becomes `source_max_withdrawal[source_id]` | Must be finite, non-negative and at least the minimum. |
| `withdrawal_bounds_origin` | `str` | Records whether effective bounds came from the database, scenario overrides or both. | Derived by loader | Auditability and warnings | Expected values include `database`, `scenario_override` and `mixed`. |
| `fixed_activation_cost` | `float` | Cost incurred when the source is activated. | Scenario configuration | Becomes `source_fixed_cost[source_id]` | Must be finite and non-negative. |
| `cost_per_ml` | `float \| None` | Variable cost per ML withdrawn. | Database | Becomes `source_unit_cost[source_id]` | Required for usable sources and must be non-negative. |
| `quality` | `dict[str, float]` | Raw representative source-quality values keyed by canonical parameter identifier. | Database quality view, normalised by loader | Preprocessing transforms and writes `source_quality[(source_id, parameter_id)]` | Keys must match `quality_limits["parameters"]`. Values must be finite and valid for each parameter. |
| `has_estimated_values` | `bool` | Indicates that at least one relevant input is estimated or overridden. | Derived by loader | Policy enforcement and warnings | Scenario overrides may count as estimated/overridden values. |
| `database_model_ready` | `bool` | Database-view readiness before scenario-level overrides. | Database view | Warnings and audit information | Does not independently determine `ScenarioData.is_ready`. |
| `availability_status` | `str` | Descriptive database readiness status. | Database view | Diagnostics and reporting | Informational in the current model. |
| `provenance` | `dict[str, str \| None]` | Records where selected source values came from. | Database provenance columns | Audit and debugging | Contract does not enforce a fixed provenance-key set. |

## 6.3 Formulation mapping

| `SourceInput` field | Preprocessing output | Formulation role |
|---|---|---|
| `source_id` | `source_ids` | Source set \(S\) |
| `minimum_withdrawal_ml_per_day` | `source_min_withdrawal` | \(\underline{W}_s\) |
| `maximum_withdrawal_ml_per_day` | `source_max_withdrawal` | \(\overline{W}_s\) |
| `fixed_activation_cost` | `source_fixed_cost` | \(F_s\) |
| `cost_per_ml` | `source_unit_cost` | \(C_s\) |
| `quality[parameter_id]` | `source_quality[(source_id, parameter_id)]` | \(Q_{sp}\) |

## 6.4 Quality-key invariant

For a strict, formulation-ready scenario:

```python
set(source.quality) == set(scenario.quality_limits["parameters"])
```

This should be checked for every usable source.

The rule provides immediate failure when:

- a source value is missing;
- a source contains an unknown quality parameter;
- a quality limit exists without a corresponding source value;
- inconsistent names such as `alkalinity_mg_l_caco3` and `alkalinity` are used on opposite sides.

Adding a new parameter such as fluoride does not require a new `SourceInput` field. The parameter is added through configuration and source data using the same key:

```python
quality={
    "pH": 7.2,
    "alkalinity": 45.0,
    "turbidity": 1.1,
    "fluoride": 0.7,
}
```

The associated unit and transformation metadata belong in:

```python
quality_limits["parameters"]["fluoride"]
```

## 6.5 Example

```python
SourceInput(
    source_id="225103",
    name="Thomson Reservoir",
    source_type="reservoir",
    enabled=True,
    forced_inactive=False,
    minimum_withdrawal_ml_per_day=100.0,
    maximum_withdrawal_ml_per_day=700.0,
    withdrawal_bounds_origin="scenario_override",
    fixed_activation_cost=0.0,
    cost_per_ml=1.25,
    quality={
        "pH": 7.2,
        "alkalinity": 45.0,
        "turbidity": 1.1,
    },
    has_estimated_values=True,
    database_model_ready=False,
    availability_status="withdrawal_bounds_required",
    provenance={
        "minimum_withdrawal": None,
        "maximum_withdrawal": None,
        "cost": "source-cost dataset",
    },
)
```

---

# 7. `PlantInput`

## 7.1 Purpose

`PlantInput` represents one treatment plant. It provides the data needed to create the plant set, plant activation decisions, throughput bounds and cost terms.

## 7.2 Field reference

| Field | Type | Purpose | Downstream use | Validation notes |
|---|---|---|---|---|
| `plant_id` | `str` | Stable unique plant identifier. | Builds plant set \(T\); keys plant parameters and arcs | Required and unique. |
| `name` | `str` | Human-readable plant name. | Logs and reports | Display only. |
| `enabled` | `bool` | Indicates whether the plant is enabled. | Scenario topology selection | Disabled plants should be filtered consistently. |
| `minimum_processing_capacity_ml_per_day` | `float` | Minimum throughput when the plant is active. | `plant_min_throughput[plant_id]` | Must be finite, non-negative and no greater than maximum. |
| `maximum_processing_capacity_ml_per_day` | `float \| None` | Maximum daily processing capacity. | `plant_max_throughput[plant_id]` | Required for preprocessing and must be at least the minimum. |
| `fixed_activation_cost` | `float` | Cost incurred when the plant is activated. | `plant_fixed_cost[plant_id]` | Must be finite and non-negative. |
| `treatment_cost_per_ml` | `float` | Variable treatment cost per ML processed. | `plant_unit_treatment_cost[plant_id]` | Must be finite and non-negative. |

## 7.3 Formulation mapping

| `PlantInput` field | Preprocessing output | Formulation role |
|---|---|---|
| `plant_id` | `plant_ids` | Plant set \(T\) |
| `minimum_processing_capacity_ml_per_day` | `plant_min_throughput` | \(\underline{V}_t\) |
| `maximum_processing_capacity_ml_per_day` | `plant_max_throughput` | \(\overline{V}_t\) |
| `fixed_activation_cost` | `plant_fixed_cost` | \(F_t\) |
| `treatment_cost_per_ml` | `plant_unit_treatment_cost` | \(C_t\) |

## 7.4 Example

```python
PlantInput(
    plant_id="plant_1",
    name="Toy Treatment Plant",
    enabled=True,
    minimum_processing_capacity_ml_per_day=100.0,
    maximum_processing_capacity_ml_per_day=1680.0,
    fixed_activation_cost=250.0,
    treatment_cost_per_ml=0.10,
)
```

---

# 8. `DemandZoneInput`

## 8.1 Purpose

`DemandZoneInput` represents one delivery zone with a required daily demand.

The current formulation requires:

```text
total delivered flow to zone >= demand_ml_per_day
```

Because unmet demand is not currently supported, a separate `demand_must_be_met` flag would always be `True` and would carry no information. It is therefore intentionally excluded from the contract.

## 8.2 Field reference

| Field | Type | Purpose | Downstream use | Validation notes |
|---|---|---|---|---|
| `zone_id` | `str` | Stable unique zone identifier. | Builds zone set \(Z\); keys demand and delivery arcs | Required and unique. |
| `name` | `str` | Human-readable zone name. | Logs and reporting | Display only. |
| `demand_ml_per_day` | `float \| None` | Required daily delivery. | Becomes `demand_by_zone[zone_id]`, corresponding to \(D_z\) | Must be finite and non-negative in a strict scenario. |

If a later formulation introduces optional unmet demand, the contract can add explicit shortage parameters only when the associated variables, penalties and constraints are approved.

## 8.3 Example

```python
DemandZoneInput(
    zone_id="zone_1",
    name="Toy Demand Zone",
    demand_ml_per_day=1200.0,
)
```

---

# 9. `SourcePlantLinkInput`

## 9.1 Purpose

Represents one directed network connection from a source to a plant.

## 9.2 Field reference

| Field | Type | Purpose | Downstream use | Validation notes |
|---|---|---|---|---|
| `source_id` | `str` | Source at the start of the arc. | Arc key `(source_id, plant_id)` | Must reference an available source. |
| `plant_id` | `str` | Plant at the end of the arc. | Arc key `(source_id, plant_id)` | Must reference an existing plant. |
| `enabled` | `bool` | Indicates whether the route is enabled. | Topology filtering | Disabled arcs should not enter model arc sets. |
| `maximum_flow_ml_per_day` | `float \| None` | Maximum daily link flow. | `source_plant_link_capacity[(source_id, plant_id)]` | Required, finite and non-negative for a usable link. |

## 9.3 Formulation mapping

| Contract value | Preprocessing output | Formulation role |
|---|---|---|
| `(source_id, plant_id)` | `source_plant_arcs` | Existing source-to-plant arc set |
| `maximum_flow_ml_per_day` | `source_plant_link_capacity` | \(\overline{L}_{st}\) |

---

# 10. `PlantZoneLinkInput`

## 10.1 Purpose

Represents one directed connection from a plant to a demand zone.

## 10.2 Field reference

| Field | Type | Purpose | Downstream use | Validation notes |
|---|---|---|---|---|
| `plant_id` | `str` | Sending plant. | Arc key `(plant_id, zone_id)` | Must reference an existing plant. |
| `zone_id` | `str` | Receiving demand zone. | Arc key `(plant_id, zone_id)` | Must reference an existing zone. |
| `enabled` | `bool` | Indicates whether the route is enabled. | Topology filtering | Disabled arcs should not enter model arc sets. |
| `maximum_flow_ml_per_day` | `float \| None` | Maximum daily link flow. | `plant_zone_link_capacity[(plant_id, zone_id)]` | Required, finite and non-negative for a usable link. |

## 10.3 Formulation mapping

| Contract value | Preprocessing output | Formulation role |
|---|---|---|
| `(plant_id, zone_id)` | `plant_zone_arcs` | Existing plant-to-zone arc set |
| `maximum_flow_ml_per_day` | `plant_zone_link_capacity` | \(\overline{L}_{tz}\) |

---

# 11. `ScenarioData`

## 11.1 Purpose

`ScenarioData` groups all inputs for one scenario into one structured object.

It is returned by the loader:

```python
scenario = load_scenario(path, strict=True)
```

and consumed by preprocessing:

```python
parameters = preprocess_scenario(scenario)
```

## 11.2 Field reference

| Field | Type | Purpose | Downstream use | Notes |
|---|---|---|---|---|
| `scenario_id` | `str` | Stable machine-readable scenario identifier. | Logging, testing and future result association | Should be non-empty and unique in the scenario catalogue. |
| `scenario_name` | `str` | Human-readable scenario title. | CLI and reporting | Descriptive only. |
| `status` | `str` | Lifecycle state such as `draft` or `approved`. | Governance and reporting | Does not independently control `is_ready`. |
| `description` | `str` | Explains scenario purpose and scope. | Documentation and UI | Not used mathematically. |
| `sources` | `tuple[SourceInput, ...]` | Ordered source collection. | Builds source parameters | IDs must be unique. |
| `plants` | `tuple[PlantInput, ...]` | Ordered plant collection. | Builds plant parameters | IDs must be unique. |
| `demand_zones` | `tuple[DemandZoneInput, ...]` | Ordered demand-zone collection. | Builds demand parameters | IDs must be unique. |
| `source_to_plant_links` | `tuple[SourcePlantLinkInput, ...]` | Source-to-plant topology. | Builds source-plant arcs and capacities | Duplicate and unknown references should be rejected. |
| `plant_to_zone_links` | `tuple[PlantZoneLinkInput, ...]` | Plant-to-zone topology. | Builds plant-zone arcs and capacities | Duplicate and unknown references should be rejected. |
| `quality_limits` | `dict[str, Any]` | Parameter definitions, limits, units and transformations. | Preprocessing creates model quality bounds | Parameter keys must match each usable source's `quality` keys. |
| `validation_issues` | `tuple[str, ...]` | Blocking loader issues. | Controls readiness and strict preprocessing | Empty means no blocking loader issues. |
| `is_ready` | computed property | Convenience readiness result. | CLI summaries and gating | True only when `validation_issues` is empty. |

An unused `treatment` dictionary is intentionally excluded. Treatment, dosing or batching inputs should only be added after their mathematical variables, parameters and constraints are approved.

---

# 12. `is_ready`

```python
@property
def is_ready(self) -> bool:
    return not self.validation_issues
```

| `validation_issues` | `is_ready` |
|---|---:|
| `()` | `True` |
| `("Missing demand",)` | `False` |

`is_ready=True` means the loader found no blocking issue under the current policy. It does not mean:

- every database value is operationally verified;
- the scenario is approved;
- capacity and quality are feasible;
- the solver will find an optimal solution.

---

# 13. Quality-limit structure

The current preprocessing expects a structure similar to:

```json
{
  "applies_to": "blend_at_plant_inflow",
  "parameters": {
    "pH": {
      "min": 6.5,
      "max": 8.5,
      "unit": "pH",
      "transform": "ph_to_hydrogen_ion"
    },
    "alkalinity": {
      "min": 20,
      "max": 200,
      "unit": "mg/L CaCO3",
      "transform": "identity"
    },
    "turbidity": {
      "min": 0,
      "max": 5,
      "unit": "NTU",
      "transform": "identity"
    }
  }
}
```

The same identifiers are used in each source:

```python
source.quality["pH"]
source.quality["alkalinity"]
source.quality["turbidity"]
```

The unit is defined once in `quality_limits`, rather than being duplicated in a source-field name. This reduces the risk of unit metadata and source-field naming drifting apart.

pH remains raw in `SourceInput`. Preprocessing performs the model-specific conversion to hydrogen-ion concentration.

---

# 14. Validation ownership

| Validation | Responsible layer |
|---|---|
| Scenario JSON shape | Loader |
| Database connectivity and source lookup | Loader |
| Missing required values | Loader |
| Estimated-value policy | Loader |
| Source quality keys equal limit parameter keys | Loader, with defensive preprocessing check |
| Minimum does not exceed maximum | Loader and preprocessing |
| Non-negative costs and capacities | Loader and preprocessing |
| Unique IDs and valid arc references | Loader and preprocessing |
| pH and other quality transformations | Preprocessing |
| Capacity feasibility | Preprocessing |
| Necessary quality feasibility | Preprocessing |
| Variable and parameter key alignment | Constraints/model builder |
| Final MILP feasibility | Solver |

The dataclasses do not run `__post_init__` validation. This permits preview objects, but direct test construction must still follow the documented invariants.

---

# 15. Testing use

Tests may construct the contract directly:

```python
scenario = ScenarioData(
    scenario_id="unit_test_1",
    scenario_name="Valid contract test",
    status="test",
    description="Deterministic input without Supabase.",
    sources=(source,),
    plants=(plant,),
    demand_zones=(zone,),
    source_to_plant_links=(source_plant_link,),
    plant_to_zone_links=(plant_zone_link,),
    quality_limits={
        "parameters": {
            "pH": {"min": 6.5, "max": 8.5, "unit": "pH"},
            "alkalinity": {
                "min": 20,
                "max": 200,
                "unit": "mg/L CaCO3",
            },
            "turbidity": {"min": 0, "max": 5, "unit": "NTU"},
        }
    },
    validation_issues=(),
)
```

This supports deterministic tests without creating a second production loader.

---

# 16. Naming conventions

## 16.1 Quantities and units

Units remain explicit for scalar operational quantities:

```text
minimum_withdrawal_ml_per_day
maximum_withdrawal_ml_per_day
minimum_processing_capacity_ml_per_day
maximum_processing_capacity_ml_per_day
cost_per_ml
```

Quality values use canonical parameter identifiers instead of unit-bearing dataclass fields. Their units are defined by:

```python
quality_limits["parameters"][parameter_id]["unit"]
```

## 16.2 Mathematical notation

The documentation follows the formulation notation:

| Meaning | Notation |
|---|---|
| Source minimum withdrawal | \(\underline{W}_s\) |
| Source maximum withdrawal | \(\overline{W}_s\) |
| Plant minimum throughput | \(\underline{V}_t\) |
| Plant maximum throughput | \(\overline{V}_t\) |
| Source-to-plant link capacity | \(\overline{L}_{st}\) |
| Plant-to-zone link capacity | \(\overline{L}_{tz}\) |
| Source quality parameter | \(Q_{sp}\) |
| Demand | \(D_z\) |

The plant maximum uses index \(t\), because the value belongs to treatment plant \(t\).

---

# 17. Adding a quality parameter

Adding a parameter such as fluoride should not require modifying the contract class.

Required work should be limited to the places that genuinely supply or process the parameter:

1. Add the parameter definition to `quality_limits["parameters"]`.
2. Supply a source value using the same key in every usable source.
3. Add or select the required transformation in preprocessing.
4. Ensure the database query/view can provide the value.
5. Add focused tests for transformation and feasibility.

The following should not be necessary:

- adding a new `SourceInput` field;
- changing every constructor call;
- updating unrelated model code;
- adding a duplicate unit-bearing Python attribute.

---

# 18. Rules for future contract changes

Before adding a field, confirm:

1. It is a real scenario input.
2. Its source is known.
3. Its unit and validation rule are defined.
4. Its downstream consumer exists.
5. The formulation requires it now.
6. It cannot be represented by an existing generic structure.
7. Tests and documentation can describe its behaviour.

Do not add fields only because they may be useful later.

---

# 19. Known limitations

| Limitation | Effect | Possible improvement |
|---|---|---|
| `quality_limits` remains broadly typed | Misspelled nested keys are not caught statically | Introduce typed quality-limit classes when the shape stabilises |
| Dictionaries are mutable | `frozen=True` is not a deep freeze | Use immutable mappings or copy-on-construction |
| `enabled` filtering is not enforced by the dataclasses | Disabled records depend on loader/preprocessing discipline | Centralise filtering in one layer and test it |
| Status and origin values are free text | Typos are possible | Introduce enums when values stabilise |
| Constructors do not validate | Directly built test objects may be invalid | Add validated factories if needed |

---

# 20. Module exports

`src/contracts/__init__.py` re-exports the public classes, allowing:

```python
from src.contracts import ScenarioData, SourceInput
```

This keeps callers independent of the internal file layout of the contracts package.

---

# 21. Summary

The revised contract remains small and stable by:

- representing quality values through `quality: dict[str, float]`;
- requiring the same parameter keys on source values and quality limits;
- defining quality units once in `quality_limits`;
- removing the always-true `demand_must_be_met` flag;
- excluding the currently unused treatment dictionary;
- preserving explicit source and plant lower and upper bounds;
- keeping model variables, transformations and solver output outside the contract.

This structure supports the current formulation while reducing the number of code changes required when new quality parameters are introduced.
