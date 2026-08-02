# AquaBlend `ScenarioData` Contract Guide

## 1. Purpose

`src/contracts/scenario_data.py` defines the canonical data structures exchanged between the AquaBlend data-loading and preprocessing stages.

Its main purpose is to give every part of the MILP pipeline one consistent, typed representation of a scenario.

```text
Scenario JSON + Supabase
          |
          v
    data_loader.py
  loads, merges and validates
          |
          v
      ScenarioData
   shared input contract
          |
          v
    preprocessing.py
 transforms raw inputs into
     ModelParameters
          |
          v
     constraints.py
 adds mathematical rules to
       the PuLP model
```

`ScenarioData` is therefore a boundary object. It is not the database schema, the JSON schema, the mathematical model, or the solver result. It is the validated in-memory representation placed between those layers.

---

## 2. Why the contract is separate from `data_loader.py`

Keeping these classes in `src/contracts/scenario_data.py` provides several benefits:

| Benefit | Explanation |
|---|---|
| Single source of truth | The loader, preprocessing, tests and future model builder all import the same class definitions. |
| No duplicate dataclasses | A field cannot accidentally be added to one copy of `ScenarioData` while another copy remains outdated. |
| Clear architectural boundary | Loading logic stays in `data_loader.py`; data structure definitions stay in the contract module. |
| Easier unit testing | Tests can construct `ScenarioData` directly without connecting to Supabase. |
| Easier refactoring | Database column names may change without forcing the preprocessing and optimisation layers to change. |
| Better type checking | IDEs, Ruff, Pyright and other tools can follow the same declared field types across the pipeline. |
| Safer collaboration | Team members can agree on one contract before implementing separate loader, preprocessing and model-builder tasks. |

---

## 3. Contract responsibilities

### The contract contains

- Scenario identity and descriptive metadata
- Water-source inputs
- Treatment-plant inputs
- Demand-zone inputs
- Directed network links
- Raw water-quality values and limits
- Treatment configuration
- Loader validation issues
- Data-quality and provenance metadata

### The contract does not contain

- `DATABASE_URL`
- SQL queries or database connection objects
- Raw PostgreSQL rows
- Scenario-file paths
- PuLP variables
- Objective expressions
- Constraints
- Solver configuration
- Solver status
- Optimised flows
- Optimised activation decisions
- Transformed model parameters such as hydrogen-ion concentration
- Reporting or dashboard output

Those elements belong to other modules.

---

## 4. Dataclass configuration

Every public contract class uses:

```python
@dataclass(frozen=True, slots=True)
```

| Option | Meaning | Why it is useful here |
|---|---|---|
| `frozen=True` | Fields cannot be reassigned after construction. | Prevents the scenario from being silently changed after validation. A downstream function must create a new object rather than mutating validated input. |
| `slots=True` | Instances use declared slots instead of a dynamic `__dict__`. | Reduces memory use, prevents accidental undeclared attributes and makes the contract stricter. |
| Type annotations | Every field declares its expected Python type. | Improves readability, editor support, static analysis and consistency between modules. |
| Tuples for collections | Scenario collections are stored as tuples. | Signals that the collection is ordered and should not be modified after loading. |

### Important limitation

`frozen=True` prevents field reassignment, but it does not deeply freeze mutable objects stored inside fields. For example:

```python
source.provenance["cost"] = "changed"
scenario.quality_limits["parameters"] = {}
```

would still mutate those dictionaries.

The current contract intentionally retains dictionaries because the loader and preprocessing already use them. Code should treat them as read-only. A future version could replace them with immutable mappings or dedicated typed dataclasses.

---

## 5. Class overview

| Class | Represents | Created by | Main downstream use |
|---|---|---|---|
| `SourceInput` | One candidate raw-water source | `data_loader.py` | Source sets, costs, withdrawal bounds and quality parameters |
| `PlantInput` | One treatment plant | `data_loader.py` | Plant activation costs and throughput bounds |
| `DemandZoneInput` | One delivery or demand location | `data_loader.py` | Demand parameter and demand-satisfaction constraints |
| `SourcePlantLinkInput` | One directed source-to-plant connection | `data_loader.py` | Network arc set and source-to-plant capacity constraints |
| `PlantZoneLinkInput` | One directed plant-to-zone connection | `data_loader.py` | Network arc set and plant-to-zone capacity constraints |
| `ScenarioData` | The complete validated scenario | `load_scenario()` | Sole input to `preprocess_scenario()` |

---

# 6. `SourceInput`

## 6.1 Class purpose

`SourceInput` represents one water source that may supply water to the optimisation network.

Examples include:

- Thomson Reservoir
- O'Shannassy Reservoir
- Barwon River at Geelong

The class combines:

- Source identity
- Activation eligibility
- Minimum and maximum withdrawal limits
- Fixed and variable costs
- Raw water-quality values
- Data-readiness information
- Provenance information

The loader normalises database-specific names before creating this object. For example, the database column `max_available_ml_per_day` is exposed to the rest of the Python pipeline as `maximum_withdrawal_ml_per_day`.

## 6.2 Field reference

| Field | Type | Purpose | Typical origin | Downstream use | Validation and notes |
|---|---|---|---|---|---|
| `source_id` | `str` | Stable unique identifier for the source. It is used as the key that joins source data, network links, parameter dictionaries and solver variables. | Supabase source record and scenario JSON reference | Builds source set `S`; keys costs, withdrawal bounds, quality values and source variables | Must be present and unique within one scenario. Link records must reference an existing source ID. |
| `name` | `str` | Human-readable source name used in logs, warnings, reports and diagnostics. | Supabase `source_name` | Display and error messages | Not intended to be used as a unique model key; `source_id` is the stable identifier. |
| `source_type` | `str` | Describes the physical category of source, such as reservoir, river or other supply type. | Supabase `source_type` | Currently informational; may later support source-specific rules or reporting | The present MILP does not branch its mathematics by source type. |
| `enabled` | `bool` | Records whether the source was enabled in scenario configuration. | Scenario JSON | Intended for scenario filtering and diagnostics | Current loader normally constructs `SourceInput` only for enabled source configurations. Do not use this flag as a substitute for `forced_inactive`. |
| `forced_inactive` | `bool` | Explicitly excludes the source from the usable model set even when the source exists and is enabled. | Scenario JSON | Preprocessing removes the source from active set `S` and omits its outgoing arcs | Useful for outage, maintenance, testing and what-if scenarios. |
| `minimum_withdrawal_ml_per_day` | `float \| None` | Lower daily withdrawal bound applied when the source is activated. | Database minimum-withdrawal field or scenario override | Becomes `source_min_withdrawal[source_id]`, corresponding to \(W^{lower}_s\) | Must be finite, non-negative and no greater than the maximum. `None` may exist in preview mode, but strict loading/preprocessing should reject it for a usable source. |
| `maximum_withdrawal_ml_per_day` | `float \| None` | Upper daily withdrawal bound for the source. | Database `max_available_ml_per_day` or scenario override | Becomes `source_max_withdrawal[source_id]`, corresponding to \(W^{upper}_s\) | Must be finite, non-negative and at least the minimum. `None` may be retained for preview output but is not formulation-ready. |
| `withdrawal_bounds_origin` | `str` | Summarises where the effective minimum and maximum bounds came from. | Derived by the loader | Diagnostics, auditability and warnings | Current expected values are `database`, `scenario_override` or `mixed`. `mixed` means one bound came from the database and the other from a scenario override. |
| `fixed_activation_cost` | `float` | One-time cost charged when the source is activated. | Scenario configuration | Becomes `source_fixed_cost[source_id]`, corresponding to \(F_s\) | Must be finite and non-negative. A value of zero is valid for a test scenario but provides no economic penalty for activating that source. |
| `cost_per_ml` | `float \| None` | Variable cost for each ML withdrawn from the source. | Supabase source record | Becomes `source_unit_cost[source_id]`, corresponding to \(C_s\) | Required by current preprocessing for usable sources. Must be finite and non-negative. Positive marginal cost helps discourage unnecessary oversupply. |
| `ph` | `float \| None` | Representative raw pH value for the source. | Aggregated database quality view | Preprocessing converts it to hydrogen-ion concentration before model construction | Kept as pH in the contract. Current preprocessing expects a finite value within its supported pH range. It must not be converted in `data_loader.py`. |
| `alkalinity_mg_l_caco3` | `float \| None` | Representative alkalinity measured in mg/L as CaCO3. | Aggregated database quality view | Becomes one component of `source_quality[(source_id, parameter_id)]` | Must be finite and non-negative before model construction. |
| `turbidity_ntu` | `float \| None` | Representative turbidity measured in NTU. | Aggregated database quality view | Becomes one component of `source_quality[(source_id, parameter_id)]` | Must be finite and non-negative before model construction. |
| `has_estimated_values` | `bool` | Indicates that at least one relevant source input is estimated or replaced by a scenario override. | Derived by the loader from estimation flags and override use | Data-governance checks, warnings and scenario-policy enforcement | In the current loader, scenario overrides count as estimated/overridden values for this purpose. |
| `database_model_ready` | `bool` | Captures the readiness flag returned by the database view before scenario-level corrections or overrides. | Supabase `model_ready` | Generates preprocessing warnings and supports auditability | It does not by itself decide `ScenarioData.is_ready`. A draft/test scenario may pass using allowed overrides even when the database record is not model-ready. |
| `availability_status` | `str` | Stores the database view's descriptive status for source withdrawal readiness. | Supabase `availability_status` | Diagnostics and reporting | Informational in the current mathematical pipeline. The exact allowed strings are controlled by the database view rather than this dataclass. |
| `provenance` | `dict[str, str \| None]` | Records where selected source values came from. | Supabase provenance columns | Auditing, debugging and future reporting | Current loader includes keys such as `storage_capacity`, `reference_flow`, `minimum_withdrawal`, `maximum_withdrawal`, `cost` and `alkalinity`. The contract does not enforce a fixed key set. |

## 6.3 Formulation mapping

| `SourceInput` field | Preprocessing output | Formulation role |
|---|---|---|
| `source_id` | `source_ids` | Source set \(S\) |
| `minimum_withdrawal_ml_per_day` | `source_min_withdrawal` | \(W^{lower}_s\) |
| `maximum_withdrawal_ml_per_day` | `source_max_withdrawal` | \(W^{upper}_s\) |
| `fixed_activation_cost` | `source_fixed_cost` | \(F_s\) |
| `cost_per_ml` | `source_unit_cost` | \(C_s\) |
| `ph` | transformed `source_quality` entry | \(Q_{sp}\) for hydrogen-ion concentration |
| `alkalinity_mg_l_caco3` | `source_quality` entry | \(Q_{sp}\) for alkalinity |
| `turbidity_ntu` | `source_quality` entry | \(Q_{sp}\) for turbidity |

## 6.4 Example

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
    ph=7.2,
    alkalinity_mg_l_caco3=45.0,
    turbidity_ntu=1.1,
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

## 7.1 Class purpose

`PlantInput` represents a treatment plant that receives water from sources, processes it and transfers it to demand zones.

It contains the plant data required to create:

- Plant set `T`
- Plant binary activation variables
- Minimum-throughput constraints
- Maximum-throughput constraints
- Plant fixed-cost objective terms
- Per-ML treatment-cost objective terms

## 7.2 Field reference

| Field | Type | Purpose | Typical origin | Downstream use | Validation and notes |
|---|---|---|---|---|---|
| `plant_id` | `str` | Stable unique identifier for the plant. | Scenario JSON | Builds plant set `T`; keys plant parameters, links and variables | Must be unique. Source-to-plant and plant-to-zone links must reference an existing plant ID. |
| `name` | `str` | Human-readable plant name. | Scenario JSON | Logs, warnings and reporting | Not intended as the primary model key. |
| `enabled` | `bool` | Records whether the plant is enabled for the scenario. | Scenario JSON | Intended for filtering and scenario control | Current preprocessing expects its input collection to contain usable plants and does not independently enforce this flag everywhere. The loader/model integration should ensure disabled plants are excluded or explicitly handled. |
| `minimum_processing_capacity_ml_per_day` | `float` | Minimum throughput required whenever the plant is activated. | Scenario JSON | Becomes `plant_min_throughput[plant_id]`, corresponding to \(V^{lower}_t\) | Must be finite, non-negative and no greater than maximum capacity. Zero means the plant has no positive minimum-throughput requirement. |
| `maximum_processing_capacity_ml_per_day` | `float \| None` | Maximum volume the plant can process per day. | Scenario JSON | Becomes `plant_max_throughput[plant_id]`, corresponding to \(V^{upper}_t\) | Required by preprocessing. Must be finite, non-negative and at least the minimum. |
| `fixed_activation_cost` | `float` | Cost incurred when the plant is activated. | Scenario JSON | Becomes `plant_fixed_cost[plant_id]`, corresponding to \(F_t\) | Must be finite and non-negative. |
| `treatment_cost_per_ml` | `float` | Variable treatment cost for every ML processed. | Scenario JSON | Becomes `plant_unit_treatment_cost[plant_id]`, corresponding to \(C_t\) | Must be finite and non-negative. Positive values discourage unnecessary processing when demand is represented as a lower bound. |

## 7.3 Formulation mapping

| `PlantInput` field | Preprocessing output | Formulation role |
|---|---|---|
| `plant_id` | `plant_ids` | Plant set \(T\) |
| `minimum_processing_capacity_ml_per_day` | `plant_min_throughput` | \(V^{lower}_t\) |
| `maximum_processing_capacity_ml_per_day` | `plant_max_throughput` | \(V^{upper}_t\) |
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

## 8.1 Class purpose

`DemandZoneInput` represents one location or customer group that requires treated water.

The current formulation requires delivered water to be at least the demand:

```text
sum of plant-to-zone flow >= demand_ml_per_day
```

Demand is intentionally a lower-bound requirement rather than exact equality in the current model.

## 8.2 Field reference

| Field | Type | Purpose | Typical origin | Downstream use | Validation and notes |
|---|---|---|---|---|---|
| `zone_id` | `str` | Stable unique identifier for the demand zone. | Scenario JSON | Builds zone set `Z`; keys demand parameters, links and delivery variables | Must be unique. Plant-to-zone links must reference an existing zone ID. |
| `name` | `str` | Human-readable zone name. | Scenario JSON | Logs, errors and reports | Not the model key. |
| `demand_ml_per_day` | `float \| None` | Required daily delivery to the zone. | Scenario JSON or future demand service | Becomes `demand_by_zone[zone_id]`, corresponding to \(D_z\) | Required when `demand_must_be_met=True`. Must be finite and non-negative. |
| `demand_must_be_met` | `bool` | Indicates whether the model must fully satisfy the zone's demand. | Scenario JSON | Loader/preprocessing policy check | The current formulation supports mandatory demand. Current preprocessing rejects zones that allow unmet demand because no shortage variable or shortage penalty exists yet. |

## 8.3 Formulation mapping

| `DemandZoneInput` field | Preprocessing output | Formulation role |
|---|---|---|
| `zone_id` | `zone_ids` | Demand-zone set \(Z\) |
| `demand_ml_per_day` | `demand_by_zone` | \(D_z\) |
| `demand_must_be_met` | Validation policy | Confirms current model is compatible with mandatory-demand formulation |

## 8.4 Example

```python
DemandZoneInput(
    zone_id="zone_1",
    name="Toy Demand Zone",
    demand_ml_per_day=1200.0,
    demand_must_be_met=True,
)
```

---

# 9. `SourcePlantLinkInput`

## 9.1 Class purpose

`SourcePlantLinkInput` represents one directed network connection from a water source to a treatment plant.

The existence of an object means the network topology permits that source-plant route. The maximum-flow value limits how much water may travel through it.

## 9.2 Field reference

| Field | Type | Purpose | Typical origin | Downstream use | Validation and notes |
|---|---|---|---|---|---|
| `source_id` | `str` | Identifies the source at the start of the directed link. | Scenario JSON | Forms arc key `(source_id, plant_id)` | Must reference a source that is available in the scenario. Arcs from forced-inactive sources are excluded during preprocessing. |
| `plant_id` | `str` | Identifies the receiving treatment plant. | Scenario JSON | Forms arc key `(source_id, plant_id)` | Must reference an existing plant. |
| `enabled` | `bool` | Records whether this route is enabled. | Scenario JSON | Intended for topology filtering | Current preprocessing expects usable arcs in the tuple and does not consistently apply the flag itself. Disabled links should be filtered before model construction or explicitly supported in preprocessing. |
| `maximum_flow_ml_per_day` | `float \| None` | Maximum daily flow permitted on this connection. | Scenario JSON or future network database | Becomes `source_plant_link_capacity[(source_id, plant_id)]`, corresponding to \(L_{st}\) | Required by current preprocessing. Must be finite and non-negative. |

## 9.3 Formulation mapping

| Contract value | Preprocessing output | Formulation role |
|---|---|---|
| `(source_id, plant_id)` | `source_plant_arcs` | Existing source-to-plant arc set |
| `maximum_flow_ml_per_day` | `source_plant_link_capacity` | \(L_{st}\) |

## 9.4 Example

```python
SourcePlantLinkInput(
    source_id="225103",
    plant_id="plant_1",
    enabled=True,
    maximum_flow_ml_per_day=700.0,
)
```

---

# 10. `PlantZoneLinkInput`

## 10.1 Class purpose

`PlantZoneLinkInput` represents one directed connection from a treatment plant to a demand zone.

It defines which zones a plant may supply and the maximum capacity of that delivery route.

## 10.2 Field reference

| Field | Type | Purpose | Typical origin | Downstream use | Validation and notes |
|---|---|---|---|---|---|
| `plant_id` | `str` | Identifies the sending treatment plant. | Scenario JSON | Forms arc key `(plant_id, zone_id)` | Must reference an existing plant. |
| `zone_id` | `str` | Identifies the receiving demand zone. | Scenario JSON | Forms arc key `(plant_id, zone_id)` | Must reference an existing demand zone. |
| `enabled` | `bool` | Records whether the route is enabled. | Scenario JSON | Intended for topology filtering | As with source-to-plant links, the current pipeline should ensure disabled links are filtered before the arc reaches the solver. |
| `maximum_flow_ml_per_day` | `float \| None` | Maximum daily flow allowed from the plant to the zone. | Scenario JSON or future network database | Becomes `plant_zone_link_capacity[(plant_id, zone_id)]`, corresponding to \(L_{tz}\) | Required by current preprocessing. Must be finite and non-negative. |

## 10.3 Formulation mapping

| Contract value | Preprocessing output | Formulation role |
|---|---|---|
| `(plant_id, zone_id)` | `plant_zone_arcs` | Existing plant-to-zone arc set |
| `maximum_flow_ml_per_day` | `plant_zone_link_capacity` | \(L_{tz}\) |

## 10.4 Example

```python
PlantZoneLinkInput(
    plant_id="plant_1",
    zone_id="zone_1",
    enabled=True,
    maximum_flow_ml_per_day=1680.0,
)
```

---

# 11. `ScenarioData`

## 11.1 Class purpose

`ScenarioData` groups all inputs for one optimisation scenario into a single immutable top-level object.

It is returned by:

```python
scenario = load_scenario(path, strict=True)
```

and consumed by:

```python
parameters = preprocess_scenario(scenario)
```

This guarantees that preprocessing receives one well-defined object instead of many unrelated dictionaries and lists.

## 11.2 Field reference

| Field | Type | Purpose | Populated by | Downstream use | Validation and notes |
|---|---|---|---|---|---|
| `scenario_id` | `str` | Stable machine-readable identifier for the scenario. | Scenario JSON | Logging, test identification, scenario storage and future result association | Should be non-empty and unique within the scenario catalogue. The dataclass itself does not enforce uniqueness. |
| `scenario_name` | `str` | Human-readable scenario title. | Scenario JSON | CLI summaries, reports and UI display | Descriptive metadata only. |
| `status` | `str` | Lifecycle label such as `draft`, `approved` or another team-defined state. | Scenario JSON | Governance, reporting and future scenario selection | Informational in the current pipeline. `status="draft"` does not automatically make `is_ready` false. |
| `description` | `str` | Explains the scope and intent of the scenario. | Scenario JSON | Documentation, auditability and UI display | Not used in mathematical calculations. |
| `sources` | `tuple[SourceInput, ...]` | Ordered collection of source records. | `data_loader.py` | Preprocessing builds source sets, costs, bounds and quality parameters | IDs must be unique. Strict preprocessing requires at least one usable source. |
| `plants` | `tuple[PlantInput, ...]` | Ordered collection of treatment plants. | `data_loader.py` | Preprocessing builds plant sets, costs and throughput parameters | IDs must be unique. Current model requires at least one usable plant. |
| `demand_zones` | `tuple[DemandZoneInput, ...]` | Ordered collection of demand zones. | `data_loader.py` | Preprocessing builds demand set and demand dictionary | IDs must be unique. Current model requires at least one demand zone. |
| `source_to_plant_links` | `tuple[SourcePlantLinkInput, ...]` | Directed source-to-plant topology. | `data_loader.py` | Creates source-to-plant arc set and capacity dictionary | Duplicate arcs and unknown references should be rejected. |
| `plant_to_zone_links` | `tuple[PlantZoneLinkInput, ...]` | Directed plant-to-zone topology. | `data_loader.py` | Creates plant-to-zone arc set and capacity dictionary | Duplicate arcs and unknown references should be rejected. |
| `quality_limits` | `dict[str, Any]` | Raw quality-limit configuration for the scenario. | Scenario JSON | Preprocessing normalises parameter names, transforms pH bounds and builds lower/upper quality dictionaries | Current preprocessing expects a `parameters` mapping containing entries for `pH`, `alkalinity` and `turbidity`, each with `min` and `max`. It remains broadly typed to avoid premature schema expansion. |
| `treatment` | `dict[str, Any]` | Stores treatment-related scenario options that are not yet represented as dedicated fields. | Scenario JSON | Reserved for future treatment, dosing or batching extensions | Currently carried through the contract but not used by the present preprocessing or constraints implementation. It must not be mistaken for implemented model behaviour. |
| `validation_issues` | `tuple[str, ...]` | Blocking issues found by the loader. | `data_loader.py` | Controls readiness and prevents strict preprocessing when issues remain | Empty tuple means no blocking loader issues. It contains errors, not solved-model warnings. |
| `is_ready` | computed `bool` property | Convenience readiness check. | Derived from `validation_issues` | CLI summaries and caller gating | Returns `True` only when `validation_issues` is empty. It does not inspect `status`, solver feasibility or database readiness independently. |

## 11.3 `is_ready` behaviour

The property is:

```python
@property
def is_ready(self) -> bool:
    return not self.validation_issues
```

| `validation_issues` | `is_ready` |
|---|---:|
| `()` | `True` |
| `("Missing demand",)` | `False` |
| `("Minimum exceeds maximum", "Missing turbidity")` | `False` |

### What `is_ready=True` means

- The loader found no blocking issues under the selected loading policy.
- Required values are present according to the loader's current rules.
- The scenario may proceed to preprocessing.

### What `is_ready=True` does not mean

- The database has verified every value.
- The scenario is approved for production use.
- The MILP is mathematically feasible.
- Water-quality constraints are satisfiable.
- The solver will find an optimal solution.
- The scenario status is approved.
- Scenario overrides are operationally verified.

Those checks happen elsewhere or require governance outside the code.

---

# 12. Quality-limit structure

Although `quality_limits` is typed as `dict[str, Any]`, the current preprocessing expects a structure similar to:

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

## Why pH remains raw in `ScenarioData`

pH is not linearly additive. The contract keeps the source value as ordinary pH because that is the natural input representation. Preprocessing performs the transformation:

```text
pH -> hydrogen-ion concentration
```

The transformed value is then placed in `ModelParameters.source_quality`.

This separation keeps:

- Data loading responsible for loading and validation
- Preprocessing responsible for model-specific mathematical transformation

---

# 13. Treatment structure

The current `treatment` dictionary can carry values such as:

```json
{
  "chemical_dosing_enabled": false,
  "batching_enabled": false,
  "batch_capacity_ml": null,
  "notes": "Dosing and batching are not yet part of the approved formulation."
}
```

At present this is configuration metadata only.

A field existing inside `treatment` does not mean the optimisation model implements it. A feature becomes active only after:

1. Its data contract is approved.
2. Loader validation is added.
3. Preprocessing creates the required parameters.
4. Decision variables are added if needed.
5. Objective terms and constraints are implemented.
6. Tests confirm expected behaviour.
7. Documentation and formulation are updated.

---

# 14. Data lifecycle

## 14.1 Production loading

```text
1. Scenario JSON selects sources and network structure.
2. data_loader.py reads the JSON.
3. data_loader.py connects to Supabase.
4. Database source records are fetched.
5. Scenario overrides are applied where allowed.
6. Validation issues are collected.
7. SourceInput, PlantInput, DemandZoneInput and link objects are created.
8. ScenarioData is returned.
9. preprocessing.py transforms ScenarioData into ModelParameters.
10. constraints.py uses ModelParameters when constructing the PuLP model.
```

## 14.2 Preview mode

In preview mode, optional fields may remain `None`, and `validation_issues` may contain blocking problems.

This is useful for:

- Inspecting incomplete database records
- Viewing which inputs are missing
- Reviewing provenance
- Testing scenario configuration before strict execution

Preprocessing should not accept a scenario with blocking validation issues.

## 14.3 Strict mode

Strict loading stops when `validation_issues` is non-empty.

This ensures that preprocessing does not silently convert missing values into zero or make undocumented assumptions.

## 14.4 Unit testing

Tests may construct the contract directly:

```python
scenario = ScenarioData(
    scenario_id="unit_test_1",
    scenario_name="Valid lower-bound scenario",
    status="test",
    description="Directly constructed deterministic test input.",
    sources=(source,),
    plants=(plant,),
    demand_zones=(zone,),
    source_to_plant_links=(source_plant_link,),
    plant_to_zone_links=(plant_zone_link,),
    quality_limits=quality_limits,
    treatment={},
    validation_issues=(),
)
```

This allows deterministic preprocessing and model tests without:

- Reading files
- Connecting to Supabase
- Depending on live database values
- Maintaining a second production loader

---

# 15. Current downstream field usage

| Contract field/group | `data_loader.py` | `preprocessing.py` | `constraints.py` | Reporting/audit |
|---|---:|---:|---:|---:|
| Scenario metadata | Creates | Mostly ignores | Ignores | Yes |
| Source IDs | Creates | Builds `S` | Keys source constraints | Yes |
| Source lower/upper bounds | Creates and validates | Builds \(W^{lower}_s\), \(W^{upper}_s\) | Source activation bounds | Yes |
| Source costs | Creates and validates | Builds \(F_s\), \(C_s\) | Future objective/model builder | Yes |
| Source raw quality | Creates and validates | Transforms and builds \(Q_{sp}\) | Blend-quality constraints | Yes |
| Source provenance/readiness | Creates | Produces warnings | Ignores | Yes |
| Plant capacities | Creates | Builds \(V^{lower}_t\), \(V^{upper}_t\) | Plant activation bounds | Yes |
| Plant costs | Creates | Builds \(F_t\), \(C_t\) | Future objective/model builder | Yes |
| Demand | Creates | Builds \(D_z\) | Demand-satisfaction constraint | Yes |
| Network links | Creates and checks references | Builds arc sets and capacities | Flow, activation and capacity constraints | Yes |
| Quality limits | Creates | Transforms lower/upper bounds | Water-quality constraints | Yes |
| Treatment dictionary | Carries through | Currently unused | Currently unused | Yes |
| Validation issues | Creates | Blocks preprocessing | Not reached when blocking issues remain | Yes |

---

# 16. Naming conventions

## 16.1 Units in field names

Physical quantities include their unit in the Python field name where practical:

| Suffix | Meaning |
|---|---|
| `_ml_per_day` | Megalitres per day |
| `_per_ml` | Cost per megalitre |
| `_mg_l_caco3` | Milligrams per litre expressed as CaCO3 |
| `_ntu` | Nephelometric Turbidity Units |

This reduces ambiguity and prevents accidental unit mixing.

## 16.2 Minimum and maximum terminology

Use:

```text
minimum_withdrawal_ml_per_day
maximum_withdrawal_ml_per_day

minimum_processing_capacity_ml_per_day
maximum_processing_capacity_ml_per_day
```

Avoid older or ambiguous names such as:

```text
max_available_ml_per_day           # database-specific name
minimum_operating_flow_ml_per_day  # older scenario name
W_s                                # ambiguous without lower/upper qualification
V_t                                # ambiguous without lower/upper qualification
```

Database-specific names may remain in SQL, but they should be mapped to the canonical contract names before entering `ScenarioData`.

## 16.3 Identifier rules

Use IDs for relationships and dictionary keys:

```text
source_id
plant_id
zone_id
```

Use names for display only:

```text
name
scenario_name
```

---

# 17. Validation ownership

The dataclasses themselves do not implement numerical validation in `__post_init__`. Validation is currently owned by the loader and preprocessing layers.

| Validation type | Responsible layer |
|---|---|
| JSON structure and required sections | `data_loader.py` |
| Database connectivity and source lookup | `data_loader.py` |
| Missing source values | `data_loader.py` |
| Estimated-value policy | `data_loader.py` |
| Minimum not greater than maximum | Loader and preprocessing defensive checks |
| Non-negative costs and capacities | Loader and preprocessing |
| Unique IDs and valid link references | Loader and preprocessing |
| pH transformation | `preprocessing.py` |
| Capacity feasibility | `preprocessing.py` |
| Necessary quality feasibility | `preprocessing.py` |
| Variable/parameter key alignment | `constraints.py` |
| Final optimisation feasibility | PuLP solver |

Keeping validation outside the dataclasses makes preview-mode objects possible, but it also means direct test construction must be disciplined. A manually constructed `ScenarioData` object can contain invalid values until preprocessing checks it.

---

# 18. Rules for adding a new field

Before adding a new field to this contract, answer the following questions:

1. Is the value part of scenario input rather than solver output?
2. Which source supplies it: database, JSON, API or derived loader value?
3. Is it raw data or a model-specific transformation?
4. What is its exact unit?
5. Can it be missing in preview mode?
6. Is it required in strict mode?
7. Which class owns it?
8. Which preprocessing parameter will consume it?
9. Does it require a new formulation parameter?
10. Does it require a variable, objective term or constraint?
11. Does it require provenance or estimation metadata?
12. Which tests need updating?
13. Is backward compatibility required?
14. Does the scenario JSON schema need updating?
15. Does the database view need updating?

A field should not be added merely because it may be useful later. It should have a clear owner and a defined downstream purpose.

---

# 19. Known limitations and planned improvements

| Current limitation | Effect | Possible future improvement |
|---|---|---|
| `quality_limits` is broadly typed | Misspelled keys are not caught by static typing | Add `QualityLimitInput` and `QualityParameterInput` dataclasses |
| `treatment` is broadly typed | Configuration can exist without implemented model behaviour | Add typed treatment classes only when formulation is approved |
| Nested dictionaries remain mutable | `frozen=True` is not a deep freeze | Use immutable mappings or typed immutable records |
| `enabled` handling is not fully centralised | A disabled plant or link could be retained if filtering is inconsistent | Filter disabled objects in one agreed layer and test the behaviour |
| `status` is free text | Team members could use inconsistent lifecycle values | Introduce an enum such as `draft`, `review`, `approved`, `archived` |
| Origin/status fields are free text | Typos could create unexpected values | Introduce enums for withdrawal origin and availability status |
| Contract constructors do not validate | Directly created test objects can be invalid | Add factory functions or optional `__post_init__` checks |
| Provenance keys are not fixed | Different loaders could use inconsistent keys | Add a typed `SourceProvenance` dataclass |
| No schema version field | Future contract migrations may be harder to distinguish | Add `contract_version` or `scenario_schema_version` |

These are design improvements, not requirements for the current Sprint 1 pipeline.

---

# 20. Module exports

`scenario_data.py` defines:

```python
__all__ = [
    "DemandZoneInput",
    "PlantInput",
    "PlantZoneLinkInput",
    "ScenarioData",
    "SourceInput",
    "SourcePlantLinkInput",
]
```

The package-level `src/contracts/__init__.py` re-exports the same classes.

This allows clean imports:

```python
from src.contracts import ScenarioData, SourceInput
```

instead of:

```python
from src.contracts.scenario_data import ScenarioData, SourceInput
```

The package import is preferred across AquaBlend modules because it keeps callers independent of the internal file layout of the contracts package.

---

# 21. Summary

`ScenarioData` is the canonical validated input contract for the AquaBlend MILP pipeline.

Its design ensures that:

- The loader remains responsible for acquiring and validating data.
- Preprocessing remains responsible for mathematical transformation.
- Constraints remain responsible for enforcing the formulation.
- Tests can construct deterministic scenarios directly.
- The model does not depend on database-specific field names.
- Provenance and readiness information remain available for audit.
- Minimum and maximum source/plant bounds are represented explicitly.
- Solver variables and outputs do not leak into the input contract.

The contract should remain small, stable and focused on scenario input. New fields should be introduced only when their data source, validation rule and downstream model use are clearly defined.
