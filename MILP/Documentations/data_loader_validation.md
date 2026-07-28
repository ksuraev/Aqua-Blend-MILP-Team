# Aqua Blend Data Loader Reference

**File:** `MILP/src/data_loader.py`  
**Purpose:** Map how scenario data is loaded, validated, structured, and passed to preprocessing.

---

## 1. Role of the loader

`data_loader.py` is the data-entry boundary for the Aqua Blend MILP pipeline. It combines scenario configuration from JSON with source information retrieved from Supabase.

| Input | Main contents |
|---|---|
| Scenario JSON | Selected sources, source activation costs, plants, demand zones, links, capacities, costs, and quality limits. |
| Supabase view | Source identity, availability, variable cost, water quality, estimation flags, provenance, and readiness metadata. |

The loader:

1. Reads the scenario JSON.
2. Checks the required JSON structures.
3. Loads `DATABASE_URL` from `.env`.
4. Retrieves enabled and active source records from Supabase.
5. Combines scenario values with database values.
6. Validates sources and network data.
7. Returns a structured `ScenarioData` object.
8. Blocks invalid scenarios in strict mode.

It does not create MILP variables, apply constraints, or run the solver.

```mermaid
flowchart LR
    A[Scenario JSON] --> C[data_loader.py]
    B[Supabase view] --> C
    C --> D[ScenarioData]
    D --> E[preprocessing.py]
    E --> F[ModelParameters]
    F --> G[MILP model]
    G --> H[Solver]
```

---

## 2. Loading sequence

```mermaid
flowchart TD
    A[Read JSON] --> B[Validate top-level sections]
    B --> C[Collect enabled source IDs]
    C --> D[Load DATABASE_URL]
    D --> E[Query active source rows]
    E --> F[Build and validate sources]
    F --> G[Build and validate network]
    G --> H[Validate link references]
    H --> I[Validate quality limits]
    I --> J[Collect issues]
    J --> K{Strict mode and issues?}
    K -->|Yes| L[Raise DataLoadError]
    K -->|No| M[Return ScenarioData]
```

### Source-value precedence

For source availability, a scenario override takes priority over the database value.

```text
max_available_ml_per_day_override supplied?
        ├─ Yes → use scenario override
        └─ No  → use database max_available_ml_per_day
```

The selected origin is stored as `scenario_override` or `database`.

---

## 3. Main functions

| Function | Responsibility | Result |
|---|---|---|
| `_read_json()` | Reads and parses the scenario file. | Scenario dictionary. |
| `_database_url()` | Loads the local database connection string. | `DATABASE_URL`. |
| `_quoted_relation()` | Validates and quotes the configured database relation. | Safe SQL relation. |
| `_fetch_sources()` | Retrieves selected active source rows. | Database row dictionaries. |
| `_enabled_mappings()` | Removes entries with `enabled: false`. | Enabled JSON objects. |
| `_build_sources()` | Merges scenario and database source data and validates it. | `SourceInput` records and issues. |
| `_build_network()` | Builds and validates plants, demand zones, and links. | Network records and issues. |
| `_validate_quality_limits()` | Checks required quality bounds. | Quality issues. |
| `load_scenario()` | Coordinates the complete loading process. | `ScenarioData`. |
| `print_summary()` | Prints loaded sources, demands, and validation issues. | Console output. |
| `main()` | Provides command-line execution. | Strict or preview run. |

---

## 4. Inputs and structured output

### Scenario JSON sections

| Section | Expected type | Used for |
|---|---:|---|
| `scenario_id` | String | Scenario identifier. |
| `scenario_name` | String | Display name. |
| `status` | String | Scenario state. |
| `description` | String | Scenario explanation. |
| `data_source` | Object | Supabase view and estimation policy. |
| `validation` | Object | Validation switches. |
| `sources` | Array | Source selection, activation cost, and availability overrides. |
| `network` | Object | Plants, zones, and links. |
| `quality_limits` | Object | Raw blend-quality bounds. |
| `treatment` | Object | Reserved treatment configuration. |

### Returned dataclasses

| Dataclass | Represents | Important values |
|---|---|---|
| `SourceInput` | One enabled source. | Availability, fixed and variable costs, raw quality, provenance, and estimation status. |
| `PlantInput` | One enabled plant. | Minimum flow, capacity, activation cost, and treatment cost. |
| `DemandZoneInput` | One enabled zone. | Demand and demand policy. |
| `SourcePlantLinkInput` | One source-to-plant connection. | Endpoints and maximum flow. |
| `PlantZoneLinkInput` | One plant-to-zone connection. | Endpoints and maximum flow. |
| `ScenarioData` | Complete loaded scenario. | All entities, limits, treatment data, and validation issues. |

The dataclasses are frozen to reduce accidental changes after loading.

Some numeric fields allow `None` so preview mode can represent incomplete data. Strict mode prevents unresolved required values from continuing to preprocessing.

`ScenarioData.is_ready` is `True` only when `validation_issues` is empty.

---

## 5. Supabase view contract

The relation configured in `data_source.view` must expose the columns used by `_fetch_sources()`.

The relation can be a physical table or a view that combines multiple source tables.

| Column group | Expected columns | Purpose |
|---|---|---|
| Identity | `source_id`, `source_name`, `source_type`, `is_active` | Identifies source records and filters inactive rows. |
| Availability | `storage_capacity_ml`, `reference_flow_ml_per_day`, `max_available_ml_per_day` | Supplies source capacity and availability information. |
| Variable cost | `cost_per_ml` | Supplies source cost per ML. |
| Water quality | `representative_ph`, `representative_alkalinity_mg_l_caco3`, `representative_turbidity_ntu` | Supplies representative raw quality values. |
| Estimated flags | `storage_capacity_is_estimated`, `reference_flow_is_estimated`, `max_available_is_estimated`, `cost_is_estimated`, `alkalinity_is_estimated` | Marks estimated or derived values. |
| Provenance | `storage_capacity_provenance`, `reference_flow_provenance`, `max_available_provenance`, `cost_provenance`, `alkalinity_provenance` | Records where source values came from. |
| Readiness | `availability_status`, `model_ready` | Carries database readiness metadata. |

The source fixed activation cost is loaded from `sources[].fixed_activation_cost` in the scenario JSON rather than from Supabase.

The query only returns requested active sources:

```sql
WHERE source_id = ANY(%s)
  AND is_active = TRUE
```

### Database safety

| Control | Behaviour |
|---|---|
| Relation-name validation | Only a safe `view` or `schema.view` format is accepted. |
| Quoted relation names | Each relation component is quoted before use. |
| Parameterised source IDs | Source IDs are passed through `%s`, not inserted into SQL text. |
| Connection timeout | The database connection uses a ten-second timeout. |
| Error wrapping | Database failures are returned as `DataLoadError`. |

---

## 6. Validation strategy

The loader uses two validation behaviours.

| Behaviour | Used when | Examples |
|---|---|---|
| Immediate error | Loading cannot safely continue. | Invalid JSON, wrong JSON type, blank required identifier, duplicate ID, invalid number, unsafe view name, or database failure. |
| Collected issue | The object can still be represented for diagnosis. | Missing availability, negative cost, missing quality, missing demand, unknown link endpoint, or invalid quality limit. |

In strict mode, collected issues are combined and raised as one `DataLoadError`. In preview mode, they are returned in `ScenarioData.validation_issues`.

### Numeric validation

`_to_float()`:

- Converts integers, floats, numeric strings, and `Decimal` values.
- Preserves `None` so missing-value rules can be applied separately.
- Rejects invalid values.
- Rejects `NaN` and infinite values.

`_append_required_non_negative_issue()` records:

- Missing required values.
- Values below zero.

Required model values are not silently replaced with zero.

---

## 7. Validation matrix

### Top-level configuration

| Item | Validation |
|---|---|
| Scenario file | Must exist and contain valid JSON. |
| JSON root | Must be one object. |
| `data_source` | Must be an object. |
| `data_source.type` | Must equal `supabase`. |
| `data_source.view` | Must be present and name-safe. |
| `validation` | Must be an object. |
| `sources` | Must be an array. |
| `network` | Must be an object. |
| `quality_limits` | Must be an object. |
| `treatment` | Must be an object when provided. |

### Sources

| Item | Validation |
|---|---|
| Enabled filtering | Sources with `enabled: false` are excluded. |
| `source_id` | Required and unique among enabled sources. |
| Database match | Must resolve to an active database row when the validation flag requires it. |
| Availability | Required unless forced inactive; must be finite and non-negative. |
| Availability override | Takes priority over the database value and must be finite and non-negative. |
| `fixed_activation_cost` | Required, finite, and non-negative. |
| `cost_per_ml` | Required from the database, finite, and non-negative. |
| pH | Required when configured and must be between 0 and 10. |
| Alkalinity | Required when configured and must be non-negative. |
| Turbidity | Required when configured and must be non-negative. |
| Estimated values | Rejected when `allow_estimated_values` is false. |
| Provenance | Passed through to `SourceInput`. |
| `model_ready` | Stored but not currently used as a blocking rule. |

A source is marked as estimated when its cost, alkalinity, or availability is estimated, or when a scenario availability override is used.

### Plants

| Item | Validation |
|---|---|
| Enabled filtering | Plants with `enabled: false` are excluded. |
| `plant_id` | Required and unique. |
| `name` | Required. |
| Minimum operating flow | Defaults to `0` when omitted; a supplied value must be finite and non-negative. |
| Maximum processing capacity | Required, finite, and non-negative. |
| Fixed activation cost | Required, finite, and non-negative. |
| Treatment cost per ML | Required, finite, and non-negative. |

### Demand zones

| Item | Validation |
|---|---|
| Enabled filtering | Zones with `enabled: false` are excluded. |
| `zone_id` | Required and unique. |
| `name` | Required. |
| Demand | Must be finite and non-negative when supplied. |
| Missing demand | Reported when demand must be met and `fail_if_demand_missing` is enabled. |
| `demand_must_be_met` | Defaults to `true`. |

### Source-to-plant links

| Item | Validation |
|---|---|
| Enabled filtering | Disabled links are excluded. |
| `source_id` and `plant_id` | Both are required. |
| Duplicate pair | `(source_id, plant_id)` must be unique. |
| Maximum flow | Required, finite, and non-negative. |
| Source reference | Must resolve to a loaded source. |
| Plant reference | Must resolve to an enabled plant. |

### Plant-to-zone links

| Item | Validation |
|---|---|
| Enabled filtering | Disabled links are excluded. |
| `plant_id` and `zone_id` | Both are required. |
| Duplicate pair | `(plant_id, zone_id)` must be unique. |
| Maximum flow | Required, finite, and non-negative. |
| Plant reference | Must resolve to an enabled plant. |
| Zone reference | Must resolve to an enabled demand zone. |

### Quality limits

The current file validates this structure:

```json
{
  "quality_limits": {
    "ph": {
      "minimum": 6.5,
      "maximum": 8.5
    },
    "alkalinity_mg_l_caco3": {
      "minimum": 20,
      "maximum": 100
    },
    "turbidity_ntu": {
      "minimum": 0,
      "maximum": 8
    }
  }
}
```

| Parameter | Validation |
|---|---|
| pH | Block exists; minimum and maximum are present and finite; minimum does not exceed maximum; both bounds are between 0 and 10. |
| Alkalinity | Block exists; bounds are present, finite, ordered correctly, and non-negative. |
| Turbidity | Block exists; bounds are present, finite, ordered correctly, and non-negative. |

Raw pH is retained by the loader. Its conversion to hydrogen-ion concentration is a preprocessing responsibility.

---

## 8. Strict and preview modes

| Behaviour | Strict | Preview |
|---|---:|---:|
| Reads JSON and database data | Yes | Yes |
| Collects semantic issues | Yes | Yes |
| Raises when collected issues exist | Yes | No |
| Returns incomplete raw fields | No | Yes |
| Allows malformed JSON or unsafe relation names | No | No |

### Strict loading

```python
from MILP.src.data_loader import load_scenario

scenario = load_scenario(
    "config/scenarios/base_scenarios_v1.json",
    strict=True,
)
```

Use strict mode before preprocessing and model construction.

### Preview loading

```python
from MILP.src.data_loader import load_scenario

scenario = load_scenario(
    "config/scenarios/base_scenarios_v1.json",
    strict=False,
)

for issue in scenario.validation_issues:
    print("-", issue)
```

Use preview mode to inspect several data issues in one run.

---

## 9. Relationship to the MILP

| MILP input | Meaning | Loader value |
|---|---|---|
| `D_z` | Demand at zone `z` | `DemandZoneInput.demand_ml_per_day` |
| `F_s` | Fixed source activation cost | `SourceInput.fixed_activation_cost` |
| `F_t` | Fixed plant activation cost | `PlantInput.fixed_activation_cost` |
| `C_s` | Variable source cost | `SourceInput.cost_per_ml` |
| `C_t` | Treatment cost per ML | `PlantInput.treatment_cost_per_ml` |
| `W_s` | Maximum source availability | `SourceInput.max_available_ml_per_day` |
| `V_t` | Maximum plant throughput | `PlantInput.maximum_processing_capacity_ml_per_day` |
| `L_st` | Source-to-plant capacity | `SourcePlantLinkInput.maximum_flow_ml_per_day` |
| `L_tz` | Plant-to-zone capacity | `PlantZoneLinkInput.maximum_flow_ml_per_day` |
| `Q_sp` | Source quality | Raw quality fields, transformed later where required. |
| Quality bounds | Permitted blend range | `ScenarioData.quality_limits` |

### Responsibility boundary

| Responsibility | Loader | Preprocessing |
|---|:---:|:---:|
| Read JSON and Supabase data | Yes | No |
| Validate raw identifiers and values | Yes | Additional cross-field validation only |
| Filter disabled entries | Yes | No |
| Preserve provenance and estimation flags | Yes | Uses them as required |
| Convert pH to hydrogen-ion concentration | No | Yes |
| Transform pH bounds | No | Yes |
| Apply forced-inactive effective capacity | No | Yes |
| Compare minimum plant flow with maximum capacity | No | Yes |
| Check total connected supply against demand | No | Yes |
| Build complete model-parameter dictionaries | No | Yes |

---

## 10.Additional validation coverage

| Reviewer concern | Implemented handling |
|---|---|
| `_build_network()` required stronger validation | Plants, zones, and links now use explicit numeric, duplicate, and reference checks. |
| Duplicate plant and zone IDs | Rejected. |
| Missing or negative plant capacity | Reported and blocked in strict mode. |
| Missing or negative plant costs | Reported and blocked in strict mode. |
| Required nulls converted to `0.0` | Removed for required capacities and costs. |
| Missing or negative link capacity | Reported for both link types. |
| Missing source fixed activation cost `F_s` | Required from scenario JSON. |
| Cost included as a quality value | Cost is validated separately from quality. |
| Raw pH is not linear | Raw pH is retained for preprocessing conversion. |
| Quality bounds were not validated | Presence, finiteness, ordering, ranges, and non-negativity are checked. |
| Disabled handling was inconsistent | Disabled sources, plants, zones, and links are filtered consistently. |

---

## 11. Running the loader

### Install dependencies

```bash
pip install python-dotenv "psycopg[binary]"
```

### Configure the database connection

Create a local `.env` file:

```text
DATABASE_URL=postgresql://username:password@host:port/database
```

Do not commit `.env` or database credentials to Git.

### Run the default scenario in strict mode

```bash
python MILP/src/data_loader.py
```

The default path is:

```text
config/scenarios/base_scenarios_v1.json
```

### Run a specific scenario in strict mode

```bash
python MILP/src/data_loader.py path/to/scenario.json
```

Strict mode stops when collected validation issues exist.

### Run preview mode

```bash
python MILP/src/data_loader.py path/to/scenario.json --preview
```

Preview output includes:

- Scenario name and ID
- Readiness status
- Loaded source availability
- Demand-zone values
- Collected validation issues

### Import the loader in Python

```python
from MILP.src.data_loader import DataLoadError, load_scenario

try:
    scenario = load_scenario(
        "config/scenarios/base_scenarios_v1.json",
        strict=True,
    )
    print(f"Loaded {len(scenario.sources)} sources")
except DataLoadError as exc:
    print(exc)
```

### Common failure messages

| Problem | Example message |
|---|---|
| Missing file | `Scenario file not found: ...` |
| Invalid JSON | `Invalid JSON ... line X, column Y` |
| Missing credentials | `DATABASE_URL is missing...` |
| Unsafe relation name | `Unsafe database view name: ...` |
| Duplicate ID | `The scenario contains duplicate ... IDs.` |
| Missing required value | `... is missing.` |
| Negative model value | `... must be greater than or equal to zero.` |
| Unknown link endpoint | `... references unknown ...` |
| Invalid bounds | `... minimum quality limit exceeds its maximum.` |
