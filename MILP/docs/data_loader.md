# AquaBlend Data Loader

**File:** `MILP/src/data_loader.py`
**Role:** Load one AquaBlend scenario, obtain source rows from Supabase or an inline offline fixture, validate the result, and return a `ScenarioData` object for `preprocessing.py`.

## 1. Purpose

`data_loader.py` is the input boundary for the MILP pipeline. It keeps scenario parsing, optional database access, source-row normalisation, and field-level validation in one place so downstream code receives one consistent contract. The same validation path is used for Supabase and offline inline scenarios.

```mermaid
flowchart LR
    A[Scenario JSON] --> C[data_loader.py]
    B[Supabase source view] --> C
    H[Inline source rows] --> C
    C --> D[Validated ScenarioData]
    D --> E[preprocessing.py]
    E --> F[ModelParameters]
    F --> G[model builder and constraints]
```

The loader does **not** transform quality values into model-space values, create optimisation variables, build constraints, run a solver, or return optimisation results.

---

## 2. Data sources

| Input | What it provides |
|---|---|
| Scenario JSON | Scenario identity, selected sources, source overrides, plants, demand zones, network links, costs, capacities, validation policy, and quality-parameter definitions. |
| Supabase view | Source identity, availability, variable cost, raw quality values, estimation flags, provenance, and readiness metadata when `data_source.type` is `supabase`. |
| Inline source rows | Row-shaped source records embedded under `data_source.source_rows` when `data_source.type` is `inline`. |
| `.env` | `DATABASE_URL` for Supabase scenarios only. Inline scenarios do not require database credentials. |

Source rows are joined to scenario entries using `source_id`.

The source fixed activation cost, \(F_s\), is scenario-specific and is read from `sources[].fixed_activation_cost`. When this property is omitted, the loader uses `0.0`. An explicitly supplied `null`, non-finite value, or negative value is invalid.

### 2.1 Supabase view contract

The configured relation must provide the core source fields used by the loader:

| Group | Core fields |
|---|---|
| Identity | `source_id`, `source_name`, `source_type`, `is_active` |
| Withdrawal bounds | `minimum_withdrawal_ml_per_day`, `max_available_ml_per_day` |
| Cost | `cost_per_ml` |
| Estimated-value metadata | `cost_is_estimated`, `minimum_withdrawal_is_estimated`, `max_available_is_estimated` when available |
| Provenance | `storage_capacity_provenance`, `reference_flow_provenance`, `minimum_withdrawal_provenance`, `max_available_provenance`, `cost_provenance` when available |
| Readiness | `availability_status`, `model_ready` |

Quality database columns are not hardcoded into `SourceInput`. Each entry under `quality_limits.parameters` identifies its source column through `source_field`.

For backward compatibility, these defaults are recognised when `source_field` is omitted:

| Quality parameter key | Default source field |
|---|---|
| `pH` | `representative_ph` |
| `alkalinity` | `representative_alkalinity_mg_l_caco3` |
| `turbidity` | `representative_turbidity_ntu` |

The query intentionally selects the complete source row so quality definitions can reference additional source-view columns without another loader change:

```sql
SELECT *
FROM configured_relation
WHERE source_id = ANY(%s)
  AND is_active = TRUE
ORDER BY source_id;
```

The relation name is restricted to a safe `view` or `schema.view` identifier and is quoted before interpolation. Source IDs remain query parameters.


### 2.2 Inline source-row contract

Offline scenarios use:

```json
{
  "data_source": {
    "type": "inline",
    "allow_estimated_values": false,
    "source_rows": [
      {
        "source_id": "225103",
        "source_name": "Thomson Reservoir",
        "source_type": "reservoir",
        "minimum_withdrawal_ml_per_day": 100.0,
        "max_available_ml_per_day": 700.0,
        "cost_per_ml": 1.1,
        "representative_ph": 7.2,
        "representative_alkalinity_mg_l_caco3": 50.0,
        "representative_turbidity_ntu": 1.0,
        "model_ready": true
      }
    ]
  }
}
```

`data_source.rows` is accepted as a compact compatibility alias, but `source_rows` is the canonical property.

Each inline row follows the same shape expected from the configured Supabase view. The loader passes both source types through `_build_sources()`, so duplicate IDs, missing values, costs, bounds, quality keys, provenance, estimated-value policy, and readiness checks behave consistently.

Inline mode:

- does not read `DATABASE_URL`;
- does not import or connect through `psycopg`;
- is intended for deterministic tests, onboarding, CI, and comparison against known toy-model results;
- does not replace Supabase as the production source-data path.

---

## 3. Loading flow

```mermaid
flowchart TD
    A[Read scenario JSON] --> B[Validate root and required sections]
    B --> C[Normalise quality-limit definitions]
    C --> D[Collect enabled source IDs]
    D --> E{data_source.type}
    E -->|supabase| F[Load DATABASE_URL and fetch active rows]
    E -->|inline| G[Read data_source.source_rows]
    F --> H[Build and validate SourceInput objects]
    G --> H
    H --> I[Build and validate plants, zones, and links]
    I --> J[Validate all link endpoints]
    J --> K[Create ScenarioData]
    K --> L{Strict mode and issues?}
    L -->|Yes| M[Raise DataLoadError]
    L -->|No| N[Return ScenarioData]
```

### 3.1 Withdrawal-bound precedence

Scenario overrides take priority over database values.

```text
minimum_withdrawal_ml_per_day_override
    ├─ provided → use scenario override
    └─ absent   → use database minimum_withdrawal_ml_per_day

maximum_withdrawal_ml_per_day_override
    ├─ provided → use scenario override
    └─ absent   → use database max_available_ml_per_day
```

The loader also accepts the earlier aliases:

- `minimum_withdrawal_ml_per_day`
- `max_available_ml_per_day_override`

The selected origin is stored as:

- `database`
- `scenario_override`
- `mixed`

---

## 4. Returned structures

| Dataclass | Contains |
|---|---|
| `SourceInput` | Source identity, lower and upper withdrawal bounds, fixed and variable costs, parameter-keyed raw quality values, estimation status, readiness metadata, and provenance. |
| `PlantInput` | Plant identity, lower and upper processing capacities, fixed activation cost, and treatment cost. |
| `DemandZoneInput` | Zone identity and required demand. |
| `SourcePlantLinkInput` | Enabled source-to-plant endpoint pair and maximum flow. |
| `PlantZoneLinkInput` | Enabled plant-to-zone endpoint pair and maximum flow. |
| `ScenarioData` | Complete scenario metadata, inputs, normalised quality limits, and collected validation issues. |

The revised contract does not contain:

- hardcoded `ph`, `alkalinity_mg_l_caco3`, or `turbidity_ntu` fields;
- a `demand_must_be_met` field;
- an unused top-level `treatment` dictionary.

Raw quality data is represented as:

```python
quality: dict[str, float]
```

For each usable source, its key set must match:

```python
quality_limits["parameters"]
```

Example:

```python
source.quality == {
    "pH": 7.2,
    "alkalinity": 45.0,
    "turbidity": 1.1,
}
```

Some scalar fields allow `None` in the contract so preview mode can represent incomplete input. Strict loading prevents unresolved required values from reaching preprocessing.

---

## 5. Validation rules

### 5.1 Configuration and database

| Area | Validation |
|---|---|
| Scenario path | Must exist and point to a readable file. |
| Scenario JSON | Must be valid JSON with one object at the root. |
| Scenario identity | `scenario_id`, `scenario_name`, and `status` must be non-blank. |
| `data_source` | Must be an object with `type: "supabase"` or `type: "inline"`. |
| Supabase configuration | Requires a non-blank safe `view`; loads `DATABASE_URL` only for this mode. |
| Inline configuration | Requires a non-empty `source_rows` array of row-shaped objects; `rows` is accepted as an alias. |
| `allow_estimated_values` | Must be a JSON boolean when provided. |
| `validation` | Must be an object when provided. |
| `sources` | Must be an array containing only objects. |
| `network` | Must be an object. |
| `quality_limits` | Must be an object with a non-empty `parameters` object. |
| Source rows | Supabase results and inline records must be objects with unique, non-blank `source_id` values. |
| Database connection | Required only in Supabase mode; `DATABASE_URL` must exist and the query must complete successfully. |

### 5.2 Numeric and boolean parsing

The loader rejects:

- booleans used as numeric values;
- non-numeric values;
- `NaN`;
- positive or negative infinity;
- non-boolean values used for boolean configuration properties.

Required text identifiers and names must not be blank.

### 5.3 Sources

| Field or rule | Validation |
|---|---|
| Enabled filtering | Entries with `enabled: false` are not loaded. |
| Enabled source count | A scenario with no enabled source records receives a validation issue. |
| `source_id` | Required and unique among enabled source entries. |
| Source row | Must resolve from the selected Supabase or inline source data when missing-source validation is enabled. |
| Source-row duplicates | Multiple Supabase or inline rows for the same `source_id` are rejected immediately. |
| `source_name`, `source_type` | Required and non-blank. |
| Minimum withdrawal | Required for usable sources under the configured policy; finite and non-negative. |
| Maximum withdrawal | Required for usable sources under the configured policy; finite and non-negative. |
| Bound order | Minimum withdrawal must not exceed maximum withdrawal. |
| Fixed activation cost | Defaults to `0.0` only when omitted; otherwise must be present, finite, and non-negative. |
| Cost per ML | Required for usable sources; finite and non-negative. |
| `forced_inactive` | Preserved in the contract; missing availability, cost, and quality requirements are relaxed for explicitly excluded sources. |
| Estimated or overridden values | Rejected for usable sources when `allow_estimated_values` is `false`. |
| Quality keys | Every usable source must have exactly the same raw parameter keys as `quality_limits.parameters`. |
| Quality values | Values must be numeric and finite. Transform-specific checks are repeated in preprocessing. |
| `model_ready` | Must be boolean when supplied by the selected source data. |
| Provenance | Core provenance and configured quality provenance are copied into `SourceInput.provenance`. |

### 5.4 Plants

| Field or rule | Validation |
|---|---|
| Enabled filtering | Plants with `enabled: false` are excluded. |
| `plant_id` | Required and unique among enabled plants. |
| `name` | Required and non-blank. |
| Minimum processing capacity | Required, finite, and non-negative. The earlier `minimum_operating_flow_ml_per_day` key is accepted as an alias. |
| Maximum processing capacity | Required for a formulation-ready scenario, finite, and non-negative. |
| Bound order | Minimum processing capacity must not exceed maximum processing capacity. |
| Fixed activation cost | Required, finite, and non-negative. |
| Treatment cost per ML | Required, finite, and non-negative. |
| Missing required scalars | Invalid plants are not silently repaired with zero-valued costs or capacities. |

### 5.5 Demand zones

| Field or rule | Validation |
|---|---|
| `zone_id` | Required and unique. |
| `name` | Required and non-blank. |
| Demand | Finite and non-negative when provided. |
| Missing demand | Recorded when `fail_if_demand_missing` is enabled. |
| Legacy `demand_must_be_met` input | It is not stored in the contract. When supplied as `false`, the loader records an issue because the current formulation requires complete demand satisfaction. |

### 5.6 Network links

| Link type | Validation |
|---|---|
| Source-to-plant | Enabled links require non-blank source and plant IDs, a unique endpoint pair, and a finite non-negative maximum flow. |
| Plant-to-zone | Enabled links require non-blank plant and zone IDs, a unique endpoint pair, and a finite non-negative maximum flow. |
| Endpoint checks | Links must reference loaded sources and valid plants or zones. |
| Enabled filtering | Links with `enabled: false` are excluded. |
| Required topology | At least one valid plant, demand zone, source-to-plant link, and plant-to-zone link is required. |

### 5.7 Quality-limit definitions

`quality_limits.parameters` is parameter-driven rather than fixed to three dataclass fields.

Each parameter requires:

| Property | Rule |
|---|---|
| Parameter key | Non-blank and unique as a JSON-object key. |
| `min`, `max` | Required, numeric, finite, and ordered so `min <= max`. |
| `unit` | Required and non-blank. |
| `transform` | Must be `identity` or `ph_to_hydrogen_ion`. Defaults to `identity`. |
| `source_field` | Database field containing the raw value. Required unless a backward-compatible default exists. |
| `model_name` | Model-facing parameter identifier. Defaults to the raw key, except pH transformation defaults to `hydrogen_ion_concentration_mol_l`. Must be unique. |
| `model_unit` | Model-facing unit. Defaults to the raw unit, except pH transformation defaults to `mol/L`. |
| `estimated_field` | Optional database boolean field used by estimated-value policy. |
| `provenance_field` | Optional database field copied into source provenance. |

Example:

```json
{
  "quality_limits": {
    "applies_to": "blend_at_plant_inflow",
    "parameters": {
      "pH": {
        "min": 6.5,
        "max": 8.5,
        "unit": "pH",
        "transform": "ph_to_hydrogen_ion",
        "source_field": "representative_ph",
        "model_name": "hydrogen_ion_concentration_mol_l",
        "model_unit": "mol/L"
      },
      "alkalinity": {
        "min": 20,
        "max": 200,
        "unit": "mg/L CaCO3",
        "transform": "identity",
        "source_field": "representative_alkalinity_mg_l_caco3"
      }
    }
  }
}
```

`applies_to` is retained as scenario metadata. The current loader validates the parameter definitions but does not enforce a specific `applies_to` value.

The loader keeps quality values in their raw units. `preprocessing.py` performs configured transformations for both source values and limits.

---

## 6. Error behaviour

| Mode | Behaviour |
|---|---|
| `strict=True` | Aggregates collected validation issues and raises `DataLoadError`. This is the normal mode before preprocessing. |
| `strict=False` / `--preview` | Returns a `ScenarioData` object containing `validation_issues` for inspection. |

Some failures stop immediately because safe parsing cannot continue, including:

- malformed JSON or object structure;
- unsafe relation names;
- blank required identifiers;
- invalid booleans;
- invalid, non-finite numeric values;
- duplicate database rows;
- unsupported quality transforms;
- duplicate model-facing quality identifiers;
- database connection or query failures.

Other field-level problems are collected so one run can report several issues, including:

- missing source rows;
- missing or negative capacities;
- invalid cost values;
- missing quality values;
- quality-key mismatches;
- duplicate configured plants, zones, or links;
- unknown link endpoints;
- estimated-value policy violations.

Collected issue messages are de-duplicated while preserving their first occurrence order.

---

## 7. Responsibility boundary

| File | Responsibility |
|---|---|
| `data_loader.py` | Parse JSON, select Supabase or inline source rows, normalise source-specific fields, validate individual values and references, and return `ScenarioData`. |
| `preprocessing.py` | Transform generic quality values, filter excluded entities, build formulation-ready dictionaries, and perform cross-record feasibility checks. |
| Model/constraint layer | Create decision variables, objective terms, and mathematical constraints. |
| Solver layer | Solve the completed optimisation problem. |
| Postprocessing layer | Interpret and present solver results. |

The loader intentionally does not determine whether the complete network can route all demand or whether a compliant blend is possible. Those checks depend on relationships across several validated inputs and belong in preprocessing or the optimisation model.

---

## 8. Running the loader

### Install dependencies

From the repository root:

```bash
python -m pip install -r MILP/requirements.txt
```

`psycopg` and `python-dotenv` are only exercised by Supabase scenarios. Inline scenarios can run without a database connection. Ruff is included so contributors can run the repository checks described below.

### Configure the database connection

Create a local `.env` file:

```env
DATABASE_URL=postgresql://username:password@host:5432/database
```

Do not commit `.env`.

### Run the default Supabase scenario

The default path is derived from `data_loader.py`, not from the current working directory. Both commands therefore resolve the same file.

From the repository root:

```bash
python -m MILP.src.data_loader
```

From the `MILP` directory:

```bash
python -m src.data_loader
```

An explicit path may still be supplied:

```bash
python -m MILP.src.data_loader \
  MILP/config/scenarios/base_scenarios_v1.json
```

### Run without Supabase

From the repository root:

```bash
python -m MILP.src.data_loader \
  MILP/config/scenarios/toy_scenario.json
```

From the `MILP` directory:

```bash
python -m src.data_loader config/scenarios/toy_scenario.json
```

The offline scenario must declare `data_source.type: "inline"`; it does not use `.env` or `DATABASE_URL`.

### Run in preview mode

```bash
python -m src.data_loader config/scenarios/base_scenarios_v1.json --preview
```

### Call from Python

```python
from src.data_loader import load_scenario

scenario = load_scenario(
    "config/scenarios/base_scenarios_v1.json",
    strict=True,
)
```

The summary prints:

- scenario identity and readiness;
- source lower and upper withdrawal bounds;
- withdrawal-bound origin;
- source quality keys;
- zone demand;
- collected validation issues in preview mode.

---

## 9. Common errors

| Message type | Meaning |
|---|---|
| Duplicate ID or link | A source, plant, zone, database row, or enabled arc appears more than once. |
| Source not found | An enabled source did not resolve in the configured Supabase view or inline source rows. |
| Missing required value | A required availability, capacity, cost, demand, or quality value is absent. |
| Must be finite | A value is `NaN`, infinite, or cannot be safely converted. |
| Must be greater than or equal to zero | A cost, withdrawal, demand, capacity, or link limit is negative. |
| Quality keys do not match | A source is missing a configured quality parameter or contains an unexpected one. |
| Unsupported quality transform | A quality definition requests a transformation other than `identity` or `ph_to_hydrogen_ion`. |
| Unknown endpoint | A link references an unavailable source or an invalid plant or zone. |
| Unsafe database view name | `data_source.view` is not a valid `view` or `schema.view` identifier. |
| Missing inline source rows | `data_source.type` is `inline`, but `data_source.source_rows` is absent or malformed. |
| Database connection error | In Supabase mode, credentials, connectivity, relation naming, or expected columns need checking. |

---

## 10. Review-alignment summary

The current loader incorporates the requested robustness improvements:

- plant, zone, source-row, and link duplicate checks;
- missing and negative plant-capacity validation;
- missing and negative plant-cost validation;
- missing and negative link-capacity validation;
- no silent conversion of required plant values from `None` to `0.0`;
- dynamic source-quality dictionaries keyed identically to `quality_limits.parameters`;
- removal of unused contract fields;
- defensive validation before data reaches preprocessing;
- execution-location-independent default scenario resolution;
- an offline `inline` source-data path that reuses the same `_build_sources()` validation logic.
