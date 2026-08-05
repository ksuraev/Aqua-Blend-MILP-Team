# AquaBlend Preprocessing and `ModelParameters` Contract

**Implementation file:** `MILP/src/preprocessing.py`
**Documentation file:** `MILP/docs/preprocessing.md`
**Pipeline role:** Convert a validated `ScenarioData` object into the complete, model-ready `ModelParameters` contract consumed by the model-building layer.

---

## 1. Purpose

`preprocessing.py` is the boundary between validated scenario input and mathematical model construction.

```text
Scenario JSON + Supabase
          |
          v
    data_loader.py
 loads, normalises and validates
          |
          v
      ScenarioData
     input contract
          |
          v
    preprocessing.py
 transforms and checks
          |
          v
    ModelParameters
     model contract
          |
          v
       model.py
 builds the PuLP model and invokes HiGHS
```

The purpose of preprocessing is to ensure that `model.py` receives only complete, consistent and formulation-ready values.

The model-building layer must not:

- reload scenario JSON;
- query Supabase;
- repeat field-level input validation;
- transform pH;
- infer missing capacities or costs;
- decide which sources, plants or links are usable;
- reconstruct mathematical sets from raw scenario objects.

These responsibilities belong to `data_loader.py` and `preprocessing.py`.

---

## 2. Contract boundary

### Input contract

`preprocess_scenario()` receives one validated `ScenarioData` object.

`ScenarioData` is defined separately in:

```text
MILP/src/contracts/scenario_data.py
```

The shared `ScenarioData` contract and its documentation are maintained separately. This preprocessing change depends on that contract but does not redefine or duplicate it.

A scenario must not contain unresolved loader issues:

```python
scenario.validation_issues == ()
```

When blocking validation issues remain, preprocessing raises `PreprocessingError` rather than attempting to repair or silently default the values.

### Output contract

`preprocess_scenario()` returns one immutable, slotted `ModelParameters` object:

```python
@dataclass(frozen=True, slots=True)
class ModelParameters:
    ...
```

`ModelParameters` contains only the sets, parameters, transformed quality values, capacities and warnings required by the optimisation layer.

It contains no:

- PuLP variables;
- objective expression;
- constraints;
- solver configuration;
- solver status;
- solved values;
- reporting JSON;
- postprocessing output.

---

## 3. `ModelParameters` fields

### 3.1 Sets

| Field | Type | Meaning |
|---|---|---|
| `source_ids` | `tuple[str, ...]` | Usable water-source identifiers forming set \(S\). |
| `plant_ids` | `tuple[str, ...]` | Enabled treatment-plant identifiers forming set \(T\). |
| `zone_ids` | `tuple[str, ...]` | Demand-zone identifiers forming set \(Z\). |
| `quality_parameter_ids` | `tuple[str, ...]` | Model-facing quality identifiers forming set \(P\). |

### 3.2 Network arc sets

| Field | Type | Meaning |
|---|---|---|
| `source_plant_arcs` | `tuple[tuple[str, str], ...]` | Enabled and usable source-to-plant arcs \(A_{ST}\). |
| `plant_zone_arcs` | `tuple[tuple[str, str], ...]` | Enabled and usable plant-to-zone arcs \(A_{TZ}\). |

Each arc is represented as a two-element tuple:

```python
(source_id, plant_id)
```

or:

```python
(plant_id, zone_id)
```

The model builder should create flow and activation variables only for arcs present in these sets.

### 3.3 Demand and cost parameters

| Field | Type | Formulation notation | Meaning |
|---|---|---|---|
| `demand_by_zone` | `dict[str, float]` | \(D_z\) | Required delivery to each demand zone in ML/day. |
| `source_fixed_cost` | `dict[str, float]` | \(F_s\) | Fixed cost of activating each source. |
| `plant_fixed_cost` | `dict[str, float]` | \(F_t\) | Fixed cost of activating each treatment plant. |
| `source_unit_cost` | `dict[str, float]` | \(C_s\) | Variable withdrawal cost per ML for each source. |
| `plant_unit_treatment_cost` | `dict[str, float]` | \(C_t\) | Treatment cost per ML for each plant. |

### 3.4 Source and plant bounds

| Field | Type | Formulation notation | Meaning |
|---|---|---|---|
| `source_min_withdrawal` | `dict[str, float]` | \(\underline{W}_s\) | Minimum source withdrawal when the source is active. |
| `source_max_withdrawal` | `dict[str, float]` | \(\overline{W}_s\) | Maximum source withdrawal. |
| `plant_min_throughput` | `dict[str, float]` | \(\underline{V}_t\) | Minimum plant throughput when the plant is active. |
| `plant_max_throughput` | `dict[str, float]` | \(\overline{V}_t\) | Maximum plant throughput. |

For every included source and plant:

```text
0 <= minimum <= maximum
```

No value in these dictionaries is nullable.

### 3.5 Link-capacity parameters

| Field | Type | Formulation notation | Meaning |
|---|---|---|---|
| `source_plant_link_capacity` | `dict[tuple[str, str], float]` | \(\overline{L}_{st}\) | Maximum flow on each source-to-plant arc. |
| `plant_zone_link_capacity` | `dict[tuple[str, str], float]` | \(\overline{L}_{tz}\) | Maximum flow on each plant-to-zone arc. |

The keys of each capacity dictionary must match the corresponding arc set exactly.

### 3.6 Water-quality parameters

| Field | Type | Formulation notation | Meaning |
|---|---|---|---|
| `source_quality` | `dict[tuple[str, str], float]` | \(Q_{sp}\) | Transformed quality value for source \(s\) and model parameter \(p\). |
| `quality_lower_bound` | `dict[str, float]` | \(\underline{Q}_p\) | Lower permitted model-space quality bound. |
| `quality_upper_bound` | `dict[str, float]` | \(\overline{Q}_p\) | Upper permitted model-space quality bound. |
| `quality_units` | `dict[str, str]` | — | Model-facing unit for each quality parameter. |

Quality keys use:

```python
(source_id, quality_parameter_id)
```

The model builder should use the transformed values directly. It must not reapply pH or other configured transformations.

### 3.7 Supporting metadata

| Field | Type | Meaning |
|---|---|---|
| `warnings` | `tuple[str, ...]` | Non-blocking notes that do not prevent model construction. |

Warnings may describe issues such as a source not being marked `model_ready` by the database after required scenario overrides and validation have still produced a usable record.

Warnings are informational. Blocking problems raise `PreprocessingError` before a `ModelParameters` object is returned.

---

## 4. Formal contract invariants

A valid `ModelParameters` object satisfies the following rules.

### Identifier coverage

```text
keys(demand_by_zone) = Z

keys(source_fixed_cost)
= keys(source_unit_cost)
= keys(source_min_withdrawal)
= keys(source_max_withdrawal)
= S

keys(plant_fixed_cost)
= keys(plant_unit_treatment_cost)
= keys(plant_min_throughput)
= keys(plant_max_throughput)
= T
```

### Arc coverage

```text
keys(source_plant_link_capacity) = A_ST
keys(plant_zone_link_capacity) = A_TZ
```

Every source-to-plant arc contains a source in \(S\) and a plant in \(T\).

Every plant-to-zone arc contains a plant in \(T\) and a zone in \(Z\).

### Quality coverage

```text
keys(quality_lower_bound) = P
keys(quality_upper_bound) = P
keys(quality_units) = P
```

For each source \(s \in S\) and quality parameter \(p \in P\):

```text
(s, p) exists in source_quality
```

### Numeric validity

All numerical values passed to `model.py` are:

- present;
- numeric;
- finite;
- non-negative where required;
- ordered correctly when they form lower and upper bounds.

The model builder can therefore consume the contract without applying defaults.

---

## 5. pH transformation

Raw pH is retained in `ScenarioData` because pH is the natural source-data representation.

pH is not linearly additive, so preprocessing converts it into hydrogen-ion concentration before creating `ModelParameters`:

\[
[\mathrm{H}^+] = 10^{-\mathrm{pH}}
\]

Example:

```text
Raw scenario key: pH
Raw unit: pH
Transform: ph_to_hydrogen_ion
Model parameter: hydrogen_ion_concentration_mol_l
Model unit: mol/L
```

The same transformation is applied to:

- every source pH value;
- the configured minimum pH limit;
- the configured maximum pH limit.

Because hydrogen-ion concentration decreases as pH increases, the transformed endpoints reverse direction. Preprocessing safely orders the transformed bounds:

```python
lower = min(transformed_min, transformed_max)
upper = max(transformed_min, transformed_max)
```

The resulting hydrogen-ion values are stored in:

```python
source_quality
quality_lower_bound
quality_upper_bound
quality_units
```

`model.py` must treat these values as final model-space parameters.

---

## 6. Other quality transformations

The currently supported transforms are:

| Transform | Behaviour |
|---|---|
| `identity` | Keeps a finite value unchanged. |
| `ph_to_hydrogen_ion` | Converts pH to hydrogen-ion concentration using \(10^{-\mathrm{pH}}\). |

Alkalinity and turbidity currently use identity transformation.

The quality configuration may provide a model-facing name and unit. Therefore, the identifiers in `quality_parameter_ids` may differ from the raw quality keys present in `ScenarioData`.

For each active source, preprocessing expects exact raw-key alignment between:

```python
source.quality
```

and:

```python
scenario.quality_limits["parameters"]
```

Missing or unexpected quality keys are rejected before model construction.

---

## 7. Entity and arc filtering

Preprocessing determines which records are usable by the mathematical model.

### Sources

A source is included in \(S\) only when it is enabled and not forced inactive.

An excluded source is removed from:

- `source_ids`;
- all source cost and bound dictionaries;
- `source_quality`;
- outgoing source-to-plant arcs.

### Plants

A plant is included in \(T\) only when it is enabled.

A disabled plant is removed from:

- `plant_ids`;
- plant cost and throughput dictionaries;
- connected source-to-plant arcs;
- connected plant-to-zone arcs.

### Links

Only enabled links whose endpoints remain in the active model sets are included.

This means `model.py` does not need to inspect `enabled` or `forced_inactive` flags. Those decisions are already reflected in the returned sets and dictionaries.

---

## 8. Pre-solver checks

Preprocessing performs checks that are safer and clearer before constructing the PuLP model.

### Capacity feasibility

The preprocessing layer may perform a maximum-flow check across:

```text
sources -> plants -> demand zones
```

This verifies whether the usable network has enough routable capacity to satisfy total demand before solver execution.

This is a preliminary structural check. It does not replace the final optimisation feasibility result.

### Quality feasibility

Preprocessing may apply necessary range checks to identify cases where no combination of available source qualities can satisfy a configured quality bound.

These checks are conservative and do not replace the final water-quality constraints in the optimisation model.

### Structural warnings

Non-blocking warnings may identify:

- disconnected usable sources;
- plants without usable incoming or outgoing arcs;
- demand zones without usable incoming arcs;
- database records not marked model-ready.

---

## 9. Formulation mapping

| Mathematical object | Python field |
|---|---|
| \(S\) | `source_ids` |
| \(T\) | `plant_ids` |
| \(Z\) | `zone_ids` |
| \(P\) | `quality_parameter_ids` |
| \(A_{ST}\) | `source_plant_arcs` |
| \(A_{TZ}\) | `plant_zone_arcs` |
| \(D_z\) | `demand_by_zone` |
| \(F_s\) | `source_fixed_cost` |
| \(F_t\) | `plant_fixed_cost` |
| \(C_s\) | `source_unit_cost` |
| \(C_t\) | `plant_unit_treatment_cost` |
| \(\underline{W}_s\) | `source_min_withdrawal` |
| \(\overline{W}_s\) | `source_max_withdrawal` |
| \(\underline{V}_t\) | `plant_min_throughput` |
| \(\overline{V}_t\) | `plant_max_throughput` |
| \(\overline{L}_{st}\) | `source_plant_link_capacity` |
| \(\overline{L}_{tz}\) | `plant_zone_link_capacity` |
| \(Q_{sp}\) | `source_quality` |
| \(\underline{Q}_p\) | `quality_lower_bound` |
| \(\overline{Q}_p\) | `quality_upper_bound` |

`as_formulation_dict()` provides stable mathematical-style aliases for integrations that prefer notation-oriented keys.

The descriptive dataclass attributes remain the canonical Python interface.

---

## 10. Responsibility boundaries

| Component | Responsibility |
|---|---|
| `data_loader.py` | Parse scenario input, retrieve source data, normalise external names, validate individual values and references, and return `ScenarioData`. |
| `preprocessing.py` | Transform quality data, filter unusable entities, build sets and parameter dictionaries, perform cross-record checks, and return `ModelParameters`. |
| `model.py` | Create PuLP variables, objective terms and constraints from `ModelParameters`, then invoke HiGHS. |
| `postprocessing.py` | Read the solved model and produce the agreed output contract and JSON results. |

`preprocessing.py` does not define optimisation constraints.

`model.py` does not repeat preprocessing.

`postprocessing.py` does not alter the model input contract.

---

## 11. Expected model-builder usage

```python
from src.preprocessing import ModelParameters, preprocess_scenario

parameters: ModelParameters = preprocess_scenario(scenario)
```

The model builder should then use the contract directly:

```python
for source_id in parameters.source_ids:
    maximum = parameters.source_max_withdrawal[source_id]
    minimum = parameters.source_min_withdrawal[source_id]
```

```python
for source_id, plant_id in parameters.source_plant_arcs:
    capacity = parameters.source_plant_link_capacity[
        (source_id, plant_id)
    ]
```

```python
for source_id in parameters.source_ids:
    for quality_id in parameters.quality_parameter_ids:
        value = parameters.source_quality[(source_id, quality_id)]
```

No raw `ScenarioData` transformation should occur inside `model.py`.

---

## 12. Error behaviour

`PreprocessingError` is raised when validated scenario input still cannot be converted safely into model parameters.

Typical causes include:

- unresolved loader validation issues;
- missing required model values;
- blank or duplicate identifiers;
- invalid lower and upper bounds;
- negative costs, demand, capacities or flows;
- unsupported quality transforms;
- source-quality key mismatches;
- duplicate or invalid arcs;
- insufficient network capacity;
- structurally infeasible quality ranges.

Warnings do not raise an exception and remain available through:

```python
parameters.warnings
```

---

## 13. Validation commands

From the repository root:

```bash
python -m ruff check \
  MILP/src/data_loader.py \
  MILP/src/preprocessing.py

python -m ruff format --check \
  MILP/src/data_loader.py \
  MILP/src/preprocessing.py
```

After the shared `ScenarioData` contract is present:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m py_compile \
  MILP/src/data_loader.py \
  MILP/src/preprocessing.py
```

Run the configured scenario:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m MILP.src.preprocessing \
  MILP/config/scenarios/base_scenarios_v1.json
```

---

## 14. Scope of this documentation

This document defines the preprocessing stage and the `ModelParameters` contract.

It does not define:

- the `ScenarioData` contract;
- PuLP variables;
- MILP constraints;
- objective construction;
- HiGHS solver settings;
- solved-problem output;
- the postprocessing result contract.

Those interfaces are documented and reviewed separately.
