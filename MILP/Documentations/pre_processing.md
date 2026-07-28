# AquaBlend Preprocessing Guide

## 1. Purpose

`preprocessing.py` converts a strictly validated `ScenarioData` object into the exact numerical sets and parameters required by the AquaBlend MILP formulation.

It does not reload JSON, reconnect to Supabase, create optimisation variables, define constraints, or run the solver. Its role is to transform validated input into model-ready data and reject scenarios that are structurally incompatible with the current formulation.

```text
Scenario JSON + Supabase
          ↓
     data_loader.py
 Loads and validates inputs
          ↓
    preprocessing.py
Transforms and checks model readiness
          ↓
     ModelParameters
          ↓
     model_builder
```

## 2. Input and output

### Input

`preprocess_scenario()` receives one `ScenarioData` object from `data_loader.py`.

The scenario must have no remaining loader validation issues. When issues are present, preprocessing stops immediately rather than attempting to repair incomplete data.

### Output

The function returns a frozen `ModelParameters` object containing the formulation sets, parameters, network arcs, quality units, and non-blocking warnings.

| Output group | Content |
|---|---|
| Sets | Sources, plants, demand zones, and quality parameters |
| Costs | Source and plant fixed costs, source unit costs, and treatment costs |
| Capacities | Source withdrawal, plant throughput, and link capacities |
| Demand | Required delivery for each demand zone |
| Quality | Transformed source quality values and transformed lower/upper limits |
| Network metadata | Valid source–plant and plant–zone arcs |
| Supporting metadata | Quality units and preprocessing warnings |

## 3. Formulation parameter mapping

The returned object follows the notation used in the mathematical formulation.

| Formulation | Meaning | `ModelParameters` field |
|---|---|---|
| `S` | Water sources | `source_ids` |
| `T` | Treatment plants | `plant_ids` |
| `Z` | Demand zones | `zone_ids` |
| `P` | Quality parameters | `quality_parameter_ids` |
| `D_z` | Demand for zone `z` | `demand_by_zone` |
| `F_s` | Source activation cost | `source_fixed_cost` |
| `F_t` | Plant activation cost | `plant_fixed_cost` |
| `C_s` | Source unit withdrawal cost | `source_unit_cost` |
| `C_t` | Plant treatment cost | `plant_unit_treatment_cost` |
| `W_s` | Maximum source withdrawal | `source_max_withdrawal` |
| `V_t` | Maximum plant throughput | `plant_max_throughput` |
| `L_st` | Source-to-plant link capacity | `source_plant_link_capacity` |
| `L_tz` | Plant-to-zone link capacity | `plant_zone_link_capacity` |
| `Q_sp` | Quality value `p` for source `s` | `source_quality` |
| Lower `Q_p` | Lower quality limit | `quality_lower_bound` |
| Upper `Q_p` | Upper quality limit | `quality_upper_bound` |

The method below returns only these formulation sets and parameters:

```python
formulation_data = parameters.as_formulation_dict()
```

## 4. Processing flow

```text
Validated ScenarioData
        ↓
Validate quality contract
        ↓
Transform pH values and limits
        ↓
Build source parameters
        ↓
Build plant and demand parameters
        ↓
Build network arc capacities
        ↓
Check network capacity feasibility
        ↓
Check basic quality feasibility
        ↓
Return ModelParameters
```

| Function | Responsibility |
|---|---|
| `ph_to_hydrogen_ion()` | Converts raw pH into hydrogen-ion concentration |
| `_normalise_quality_rules()` | Validates quality names, units, bounds, and transforms |
| `_transform_quality_bounds()` | Produces model-ready lower and upper quality limits |
| `_build_source_parameters()` | Builds `S`, `F_s`, `C_s`, `W_s`, and `Q_sp` |
| `_build_plant_parameters()` | Builds `T`, `F_t`, `C_t`, and `V_t` |
| `_build_demand_parameters()` | Builds `Z` and `D_z` |
| `_build_link_parameters()` | Builds arc sets, `L_st`, and `L_tz` |
| `_validate_capacity_feasibility()` | Checks whether the network can route all demand |
| `_validate_quality_feasibility()` | Screens for obviously impossible quality conditions |
| `preprocess_scenario()` | Coordinates the complete preprocessing sequence |

## 5. Quality transformations

### pH

Raw pH does not blend linearly by volume. It is converted into hydrogen-ion concentration before it is added to `Q_sp`:

```text
[H+] = 10^(-pH)
```

The model parameter name is:

```text
hydrogen_ion_concentration_mol_l
```

Because the pH scale is inverse, its limits exchange order after transformation:

```text
Transformed lower limit = 10^(-raw pH maximum)
Transformed upper limit = 10^(-raw pH minimum)
```

### Alkalinity and turbidity

Alkalinity and turbidity use the `identity` transform because the current formulation treats them as linearly blendable in their supplied units.

| Input parameter | Required input unit | Transform | Model unit |
|---|---|---|---|
| pH | `pH` | `ph_to_hydrogen_ion` | `mol/L` |
| Alkalinity | `mg/L CaCO3` | `identity` | `mg/L CaCO3` |
| Turbidity | `NTU` | `identity` | `NTU` |

The quality contract must apply to:

```json
"applies_to": "blend_at_plant_inflow"
```

## 6. Validation rules

Preprocessing performs checks that depend on relationships between already loaded fields. It does not duplicate routine JSON parsing or database validation from `data_loader.py`.

| Area | Validation performed |
|---|---|
| Loader handoff | Rejects scenarios with remaining loader validation issues |
| Required values | Rejects missing, non-numeric, `NaN`, or infinite model inputs |
| Costs | Requires `F_s`, `F_t`, `C_s`, and `C_t` to be non-negative |
| Capacities | Requires `W_s`, `V_t`, `L_st`, and `L_tz` to be non-negative |
| Demand | Requires `D_z` to be present, finite, and non-negative |
| IDs | Defensively rejects duplicate or empty source, plant, and zone IDs |
| Arcs | Rejects duplicate links and unknown source, plant, or zone endpoints |
| Quality contract | Validates parameter names, units, transforms, and ordered limits |
| Source quality | Validates pH range and non-negative alkalinity/turbidity values |
| Forced-inactive sources | Removes them from `S`, source parameters, and source–plant arcs |
| Soft demand | Rejects `demand_must_be_met: false` because the formulation requires full demand satisfaction |
| Minimum plant flow | Rejects non-zero minimum flow because the formulation has no minimum-throughput constraint |
| Network capacity | Uses a maximum-flow check to confirm that all demand can be routed |
| Quality feasibility | Rejects conditions where no available source range can satisfy a required bound |

### Capacity feasibility

A maximum-flow network is constructed using:

- Source withdrawal capacities `W_s`
- Source-to-plant capacities `L_st`
- Plant throughput capacities `V_t`
- Plant-to-zone capacities `L_tz`
- Zone demands `D_z`

If the maximum routable flow is below total demand, preprocessing raises `PreprocessingError` before the solver is called.

### Quality feasibility

The quality check is an early screening test, not a replacement for Equation 13 or the solver.

It checks whether connected source values make a compliant blend theoretically possible. The final optimisation model still determines whether one common flow allocation can satisfy all demand, capacity, cost, and quality constraints simultaneously.

## 7. Forced-inactive sources and warnings

A source marked `forced_inactive` is excluded from:

- `S`
- `F_s`
- `C_s`
- `W_s`
- `Q_sp`
- Source-to-plant arcs

This prevents the model builder from creating activation or flow decisions for a source that the scenario explicitly disables.

Preprocessing also returns warnings for non-blocking conditions, including:

- A database source not marked `model_ready`
- A source with no outgoing source-to-plant link
- A plant disconnected on its incoming or outgoing side
- An arc removed because its source was forced inactive

Warnings do not stop preprocessing when all required model parameters remain valid.

## 8. Responsibility boundary

| File | Responsibility |
|---|---|
| `data_loader.py` | Read JSON, query Supabase, validate individual fields, and return `ScenarioData` |
| `preprocessing.py` | Transform quality values, validate cross-field relationships, and return `ModelParameters` |
| `model_builder` | Create decision variables, objective terms, and MILP constraints |
| Solver layer | Solve the completed optimisation model |
| Postprocessing layer | Interpret and present model results |

`preprocessing.py` does not create:

- Binary or continuous decision variables
- Objective expressions
- Demand or flow-conservation constraints
- Activation or link constraints
- Water-quality constraints
- Pyomo, PuLP, or solver objects

## 9. Running preprocessing

### Required environment variable

```env
DATABASE_URL=postgresql://user:password@host:port/database
```

### Install loader dependencies

```bash
pip install "psycopg[binary]" python-dotenv
```

### Run as a module

From the repository root:

```bash
python -m MILP.src.preprocessing path/to/scenario.json
```

When no path is supplied, the script uses:

```text
config/scenarios/base_scenarios_v1.json
```

### Run directly

```bash
python MILP/src/preprocessing.py path/to/scenario.json
```

### Use from Python

```python
from pathlib import Path
from MILP.src.data_loader import load_scenario
from MILP.src.preprocessing import preprocess_scenario

scenario = load_scenario(Path("path/to/scenario.json"), strict=True)
parameters = preprocess_scenario(scenario)

model_input = parameters.as_formulation_dict()
```

The command-line summary reports the number of sources, plants, zones, quality parameters, arcs, total demand, total source capacity, and any warnings.

## 10. Common failures

| Error | Meaning |
|---|---|
| Loader validation issues remain | The scenario must first pass `load_scenario(..., strict=True)` |
| Unsupported quality transform | The JSON transform does not match the implemented rule |
| Invalid quality unit | The quality-limit unit does not match the source data |
| Non-zero minimum plant flow | The input uses a rule not represented in the current formulation |
| Unmet demand allowed | The input requests soft demand, but no shortfall variable exists |
| Unknown arc endpoint | A link references a source, plant, or zone outside the model sets |
| No usable source remains | All sources are forced inactive or unavailable |
| Network cannot route demand | Capacities and links cannot deliver the complete `D_z` requirement |
| No feasible quality range | Available source qualities cannot reach a required quality bound |
