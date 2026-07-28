# Aqua Blend Data Loader

**File:** `MILP/src/data_loader.py`  
**Role:** Load one Aqua Blend scenario, combine its JSON configuration with source records from Supabase, validate the result, and return structured data for `preprocessing.py`.

## 1. Purpose

`data_loader.py` is the entry point for scenario data used by the MILP pipeline. It keeps database access, JSON parsing, and input validation in one place so that preprocessing and model construction receive a consistent structure.

```mermaid
flowchart LR
    A[Scenario JSON] --> C[data_loader.py]
    B[Supabase source view] --> C
    C --> D[Validated ScenarioData]
    D --> E[preprocessing.py]
    E --> F[ModelParameters]
    F --> G[model_builder.py]
```

The loader does **not** transform pH, create optimisation variables, build constraints, or run the solver.

## 2. Data sources

| Input | What it provides |
|---|---|
| Scenario JSON | Selected sources, source activation costs, availability overrides, plants, demand zones, network links, capacities, costs, and quality limits. |
| Supabase view | Source identity, daily availability, variable cost, representative quality values, estimation flags, provenance, and readiness metadata. |
| `.env` | The `DATABASE_URL` used to connect to Supabase PostgreSQL. |

Source records are joined using `source_id`. The source fixed activation cost, \(F_s\), is scenario-specific and is therefore read from `sources[].fixed_activation_cost` in the JSON rather than from the database view.

### Supabase view contract

The relation named in `data_source.view` must expose the columns selected by `_fetch_sources()`.

| Column group | Expected values |
|---|---|
| Identity | `source_id`, `source_name`, `source_type`, `is_active` |
| Availability | `storage_capacity_ml`, `reference_flow_ml_per_day`, `max_available_ml_per_day` |
| Cost | `cost_per_ml` |
| Quality | `representative_ph`, `representative_alkalinity_mg_l_caco3`, `representative_turbidity_ntu` |
| Estimated flags | `*_is_estimated` fields used by the loader |
| Provenance | `*_provenance` fields used by the loader |
| Readiness | `availability_status`, `model_ready` |

The query returns only requested sources that are active in the database:

```sql
WHERE source_id = ANY(%s)
  AND is_active = TRUE
```

The view name is validated before being placed into SQL, and source IDs are passed as query parameters.

## 3. Loading flow

```mermaid
flowchart TD
    A[Read scenario JSON] --> B[Validate required JSON sections]
    B --> C[Collect enabled source IDs]
    C --> D[Load DATABASE_URL]
    D --> E[Fetch active source rows]
    E --> F[Build and validate sources]
    F --> G[Build and validate network]
    G --> H[Validate link references]
    H --> I[Validate quality limits]
    I --> J{Strict mode and issues?}
    J -->|Yes| K[Raise DataLoadError]
    J -->|No| L[Return ScenarioData]
```

For source availability, a scenario override takes priority over the database value:

```text
max_available_ml_per_day_override provided
        ├─ Yes → use scenario override
        └─ No  → use database max_available_ml_per_day
```

The selected origin is stored as either `scenario_override` or `database`.

## 4. Returned structures

| Dataclass | Contains |
|---|---|
| `SourceInput` | Source identity, availability, fixed and variable costs, raw quality values, estimation status, readiness metadata, and provenance. |
| `PlantInput` | Plant identity, minimum operating flow, maximum throughput, fixed activation cost, and treatment cost. |
| `DemandZoneInput` | Zone identity, demand, and demand policy. |
| `SourcePlantLinkInput` | Source-to-plant endpoints and maximum link flow. |
| `PlantZoneLinkInput` | Plant-to-zone endpoints and maximum link flow. |
| `ScenarioData` | The complete loaded scenario and any collected validation issues. |

Some numeric fields allow `None` so preview mode can represent incomplete input. With `strict=True`, unresolved required values prevent the scenario from continuing to preprocessing.

## 5. Validation rules

### Configuration and database

| Area | Validation |
|---|---|
| Scenario file | Must exist and contain valid JSON. |
| JSON root | Must be one JSON object. |
| `data_source` | Must be an object. |
| `data_source.type` | Must equal `supabase`. |
| `data_source.view` | Required and restricted to a safe `view` or `schema.view` name. |
| `validation` | Must be an object when provided. |
| `sources` | Must be an array of JSON objects. |
| `network` | Must be an object. |
| `quality_limits` | Must be an object. |
| `treatment` | Must be an object when provided. |
| Database connection | `DATABASE_URL` must be available and the query must complete successfully. |

### Sources

| Field or rule | Validation |
|---|---|
| Enabled filtering | Entries with `enabled: false` are excluded. |
| `source_id` | Required and unique among enabled sources. |
| Database record | Must resolve to an active row when missing-source validation is enabled. |
| Availability | Required unless the source is forced inactive; must be finite and non-negative. |
| Availability override | Must be finite and non-negative and takes priority over the database value. |
| `fixed_activation_cost` | Required for every enabled source, finite, and non-negative. |
| `cost_per_ml` | Required from the database, finite, and non-negative. |
| pH | Required under the configured validation policy and must be between 0 and 10. |
| Alkalinity | Required under the configured validation policy and must be non-negative. |
| Turbidity | Required under the configured validation policy and must be non-negative. |
| Estimated values | Rejected when estimated or overridden values are present and `allow_estimated_values` is false. |
| `forced_inactive` | Preserved as source metadata for preprocessing. |

Cost is validated separately from water quality because it belongs to the objective function, not the quality parameter set.

### Plants

| Field or rule | Validation |
|---|---|
| Enabled filtering | Entries with `enabled: false` are excluded. |
| `plant_id` | Required and unique. |
| `name` | Required. |
| Minimum operating flow | Defaults to `0` only when omitted; an explicit invalid, non-finite, or negative value is rejected. |
| Maximum processing capacity | Required, finite, and non-negative. |
| Fixed activation cost | Required, finite, and non-negative. |
| Treatment cost per ML | Required, finite, and non-negative. |

### Demand zones

| Field or rule | Validation |
|---|---|
| Enabled filtering | Entries with `enabled: false` are excluded. |
| `zone_id` | Required and unique. |
| `name` | Required. |
| Demand | Must be finite and non-negative when provided. |
| Missing demand | Reported when demand must be met and `fail_if_demand_missing` is enabled. |
| `demand_must_be_met` | Defaults to `true`. Formulation compatibility is checked later in preprocessing. |

### Network links

| Link type | Validation |
|---|---|
| Source-to-plant | Source and plant IDs are required; each pair must be unique; maximum flow is required, finite, and non-negative. |
| Plant-to-zone | Plant and zone IDs are required; each pair must be unique; maximum flow is required, finite, and non-negative. |
| Endpoint checks | Links must reference known loaded sources, plants, and demand zones. |
| Enabled filtering | Links with `enabled: false` are excluded. |

### Quality limits

The loader validates the agreed `quality_limits.parameters` structure.

| Item | Required value or rule |
|---|---|
| `applies_to` | Must equal `blend_at_plant_inflow`. |
| Required parameters | `pH`, `alkalinity`, and `turbidity`. |
| Unit | pH: `pH`; alkalinity: `mg/L CaCO3`; turbidity: `NTU`. |
| Transform | pH: `ph_to_hydrogen_ion`; alkalinity and turbidity: `identity`. |
| `min` and `max` | Both required, numeric, and finite. |
| Bound order | `min` must be less than or equal to `max`. |
| pH bounds | Both must be between 0 and 10. |
| Other bounds | Must be non-negative. |

The loader validates the transformation contract but keeps source pH in its raw form. `preprocessing.py` performs the actual conversion to hydrogen-ion concentration and transforms the pH limits.

## 6. Error behaviour

| Mode | Behaviour |
|---|---|
| `strict=True` | Combines collected validation issues and raises `DataLoadError`. This is the normal mode before preprocessing and model construction. |
| `strict=False` / `--preview` | Returns `ScenarioData` with `validation_issues` so incomplete scenarios can be inspected. |

Some failures stop immediately because safe loading cannot continue, including invalid JSON, malformed objects, missing identifiers, duplicate IDs, unsafe relation names, invalid numeric values, and database failures.

Other field-level problems are collected so the user can see multiple issues in one run, including missing capacities, negative costs, unavailable sources, unknown link endpoints, and invalid quality limits.

## 7. Responsibility boundary

| File | Responsibility |
|---|---|
| `data_loader.py` | Read JSON, query Supabase, combine values, validate individual fields and references, and return `ScenarioData`. |
| `preprocessing.py` | Transform pH, apply formulation compatibility rules, perform cross-field and network-feasibility checks, and build model-ready parameters. |
| `model_builder.py` | Create decision variables, the objective function, and MILP constraints. |

The loader intentionally does not check whether total supply can meet total demand or whether a feasible quality blend exists. Those checks require relationships between multiple validated inputs and therefore belong in preprocessing or the optimisation model.

## 8. Running the loader

### Install dependencies

```bash
pip install "psycopg[binary]" python-dotenv
```

### Configure the database connection

Create a local `.env` file:

```env
DATABASE_URL=postgresql://username:password@host:5432/database
```

Do not commit `.env` to version control.

### Run in strict mode

From the repository root:

```bash
python -m MILP.src.data_loader path/to/scenario.json
```

When no path is provided, the loader uses:

```text
config/scenarios/base_scenarios_v1.json
```

### Run in preview mode

```bash
python -m MILP.src.data_loader path/to/scenario.json --preview
```

Preview mode prints the loaded sources, demand zones, readiness state, and all collected validation issues.

### Call from Python

```python
from MILP.src.data_loader import load_scenario

scenario = load_scenario("path/to/scenario.json", strict=True)
```

## 9. Common errors

| Message type | Meaning |
|---|---|
| Duplicate ID or link | The same source, plant, zone, or arc has been defined more than once. |
| Source not found | An enabled JSON source did not resolve to an active row in the configured Supabase view. |
| Missing required value | A required availability, capacity, cost, demand, or quality value is `null` or absent. |
| Value must be non-negative | A cost, demand, capacity, flow limit, alkalinity, or turbidity value is below zero. |
| Unknown endpoint | A link references an unavailable source or an undefined plant or zone. |
| Invalid quality contract | The application point, unit, transform, bounds, or parameter structure does not match the agreed scenario contract. |
| Unsafe database view name | `data_source.view` does not use an accepted `view` or `schema.view` identifier. |
| Database connection error | The credentials, network connection, relation name, or expected database columns need to be checked. |
