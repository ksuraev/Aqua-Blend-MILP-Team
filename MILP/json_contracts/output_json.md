# AquaBlend MILP Output JSON Contract

**Contract file:** `MILP/examples/results/output_contract_v1.json`  
**Contract version:** `1.0`  
**Purpose:** Define the stable machine-readable result that the AquaBlend MILP must expose after model construction and solver execution.

---

## 1. Why this output contract exists

AquaBlend has several technical streams that need to consume the optimisation result without depending on the internal implementation of the Pyomo model.

The agreed pipeline is:

```text
Scenario JSON
    +
Supabase / inline source data
            ↓
      data_loader.py
            ↓
        ScenarioData
            ↓
      preprocessing.py
            ↓
       ModelParameters
            ↓
         model.py
   variables + objective
   + constraints + solver
            ↓
       solved result
            ↓
   Output JSON contract
       ↙      ↓      ↘
   Backend    AI    App / Frontend
```

The output contract is therefore a **cross-team integration boundary**. It translates solver decisions and already-validated model context into stable JSON field names.

This contract does **not** define how post-processing code must be implemented. It only defines the result structure that the model/output implementation must eventually produce.

The main design rule is:

> Internal Pyomo variable names may change. The external JSON field names should remain stable.

This matters because the Backend, AI and App teams should not have to change every time `model.py` is refactored.

---

## 2. Contract status

`output_contract_v1.json` is a **contract template**, not a claim that the current scenario has already been solved.

For that reason, model-derived fields are currently represented by `null`, the solver status is `NOT_SOLVED`, and validation-result fields are marked `NOT_RUN`.

At runtime, the model/output implementation replaces these placeholders with actual:

- solver status;
- source activation and withdrawal decisions;
- plant activation and throughput;
- network-link activation and flows;
- cost totals;
- quality results;
- binding-constraint evidence;
- validation/check results.

Known scenario values are already populated in the template where they are deterministic from the supplied scenario configuration.

---

## 3. Alignment with the current scenario

The contract is aligned to the supplied scenario:

```text
scenario_id: scenario_2026_07_17_001
scenario_name: Toy model — single plant, single zone

Sources:
- silvan_reservoir
- yarra_kew
- groundwater_bore_1

Plant:
- facility_1

Demand zone:
- zone_1

Demand:
- 500 ML/day
```

The configured network capacities are preserved exactly:

```text
silvan_reservoir -> facility_1       350 ML/day
yarra_kew -> facility_1              300 ML/day
groundwater_bore_1 -> facility_1      60 ML/day
facility_1 -> zone_1                 600 ML/day
```

The treatment plant keeps the scenario naming convention:

```text
minimum_processing_capacity_ml_per_day
maximum_processing_capacity_ml_per_day
fixed_activation_cost
treatment_cost_per_ml
```

The three quality parameters are:

```text
pH
alkalinity
turbidity
```

and quality is assessed at:

```text
blend_at_plant_inflow
```

---

## 4. Important naming convention for pH

The supplied scenario used the label:

```text
hydrogenic
```

for the pH transform.

The current loader/preprocessing contract uses the canonical transform name:

```text
ph_to_hydrogen_ion
```

The Output JSON therefore uses:

```json
"transform": "ph_to_hydrogen_ion"
```

and the model-facing parameter identifier:

```text
hydrogen_ion_concentration_mol_l
```

This is intentional. The output must follow the canonical loader/preprocessing naming rather than introduce another transform name.

The raw pH limits remain human-readable:

```text
6.5 <= pH <= 8.5
```

Preprocessing converts both source pH values and the bounds using:

```text
[H+] = 10^(-pH)
```

and reorders the transformed endpoints because hydrogen-ion concentration decreases as pH increases.

For the current pH limits the model-space bounds are approximately:

```text
lower = 3.1622776601683795e-09 mol/L
upper = 3.162277660168379e-07 mol/L
```

The Output JSON reports both the input-space pH limits and model-space hydrogen-ion limits so downstream users can see what the model actually enforced.

---

## 5. Relationship to `ScenarioData`

`ScenarioData` is the validated boundary between `data_loader.py` and `preprocessing.py`.

The Output JSON may reuse reporting/audit fields that are already present in the validated source records, including:

```text
source_id
name
source_type
enabled
forced_inactive
minimum_withdrawal_ml_per_day
maximum_withdrawal_ml_per_day
withdrawal_bounds_origin
fixed_activation_cost
cost_per_ml
has_estimated_values
database_model_ready
availability_status
provenance
```

These are not new optimisation decisions.

The output must not go back to raw database columns and independently reconstruct values that were already resolved by the loader.

For example, if a scenario override and a database value were combined into the effective source withdrawal bounds, the Output JSON should report the **effective validated bounds**, together with `withdrawal_bounds_origin`.

---

## 6. Relationship to `ModelParameters`

`preprocessing.py` produces the formulation-ready `ModelParameters` contract.

The following canonical Python fields are the source of model-facing context:

| Formulation role             | `ModelParameters` field      |
| ---------------------------- | ---------------------------- |
| Sources \(S\)                | `source_ids`                 |
| Plants \(T\)                 | `plant_ids`                  |
| Zones \(Z\)                  | `zone_ids`                   |
| Quality parameters \(P\)     | `quality_parameter_ids`      |
| Source→plant arcs \(A_ST\)   | `source_plant_arcs`          |
| Plant→zone arcs \(A_TZ\)     | `plant_zone_arcs`            |
| Demand \(D_z\)               | `demand_by_zone`             |
| Source fixed cost \(F_s\)    | `source_fixed_cost`          |
| Plant fixed cost \(F_t\)     | `plant_fixed_cost`           |
| Source unit cost \(C_s\)     | `source_unit_cost`           |
| Plant treatment cost \(C_t\) | `plant_unit_treatment_cost`  |
| Source minimum withdrawal    | `source_min_withdrawal`      |
| Source maximum withdrawal    | `source_max_withdrawal`      |
| Plant minimum throughput     | `plant_min_throughput`       |
| Plant maximum throughput     | `plant_max_throughput`       |
| Source→plant capacity        | `source_plant_link_capacity` |
| Plant→zone capacity          | `plant_zone_link_capacity`   |
| Source quality \(Q_sp\)      | `source_quality`             |
| Quality lower bound          | `quality_lower_bound`        |
| Quality upper bound          | `quality_upper_bound`        |
| Quality unit                 | `quality_units`              |

The output must treat these values as the final model-facing parameters.

It must not repeat preprocessing transformations inside the output contract.

---

## 7. Pyomo variable names are not fixed yet

The final internal Pyomo variable names in `model.py` are **not fixed yet**.

This must not block agreement on the external Output JSON.

Until the final names are confirmed, the following descriptive convention should be used when discussing the mapping:

| Formulation variable | Meaning                 | Provisional model convention                 |
| -------------------- | ----------------------- | -------------------------------------------- |
| `alpha_s`            | source activation       | `source_active[source_id]`                   |
| `a_s`                | source withdrawal       | `source_withdrawal[source_id]`               |
| `beta_t`             | plant activation        | `plant_active[plant_id]`                     |
| `gamma_st`           | source→plant activation | `source_plant_active[(source_id, plant_id)]` |
| `b_st`               | source→plant flow       | `source_plant_flow[(source_id, plant_id)]`   |
| `delta_tz`           | plant→zone activation   | `plant_zone_active[(plant_id, zone_id)]`     |
| `c_tz`               | plant→zone flow         | `plant_zone_flow[(plant_id, zone_id)]`       |

Solver-level values are referred to descriptively as:

```text
solver_status
objective_value
```

### Required integration rule

When `model.py` is finalised, the team may either:

1. follow this provisional naming convention; or
2. use different internal Pyomo names and map those names to the fields in this Output JSON.

The JSON must **not** be renamed simply to mirror internal Pyomo identifiers.

For example, regardless of whether `model.py` calls a source withdrawal variable `a`, `draw`, `withdrawal`, or `source_draw`, the external field remains:

```json
"withdrawal_ml_per_day": 250.0
```

---

## 8. Top-level structure

Version `1.0` uses these top-level keys:

```text
schema_version
scenario
validation
solver
summary
sources
plants
flows
demand_zones
quality
binding_constraints_summary
warnings
```

Each section has one responsibility.

---

### 8.1. `scenario`

The scenario section identifies the exact input that produced the result.

```json
"scenario": {
  "scenario_id": "scenario_2026_07_17_001",
  "scenario_name": "Toy model — single plant, single zone",
  "status": "draft",
  "description": "...",
  "data_source": {
    "type": "supabase",
    "view": "public.source_model_inputs",
    "allow_estimated_values": true
  }
}
```

`status` is the **scenario status**, not the solver status.

The solver result lives separately in `solver.status`.

---

### 8.2. `validation`

Validation is retained in the Output JSON because downstream systems need to know whether the result came through the expected data and mathematical readiness gates.

The output contract separates validation into three stages:

```text
validation.input_policy
validation.loader
validation.preprocessing
validation.output_consistency
```

#### 8.2.1 Input policy

The scenario currently enables these blocking policies:

```text
fail_if_source_missing_from_database
fail_if_daily_availability_missing
fail_if_required_quality_value_missing
fail_if_demand_missing
```

The contract also retains:

```text
allow_estimated_values
```

inside `scenario.data_source`.

These fields describe the policy used to construct the scenario. They are not solver decisions.

#### 8.2.2 Loader validation

`validation.loader` records whether the external input passed the loader contract.

The listed checks cover the existing loader responsibilities:

- valid scenario identity;
- source IDs present and unique;
- source exists in the configured data source;
- required withdrawal bounds available;
- withdrawal bounds finite, non-negative and ordered;
- source cost available, finite and non-negative;
- estimated/overridden values allowed by policy;
- exact quality-key alignment;
- finite source-quality values;
- plant IDs present and unique;
- plant bounds finite, non-negative and ordered;
- plant costs finite and non-negative;
- demand-zone IDs present and unique;
- demand present, finite and non-negative;
- unique valid source→plant links;
- unique valid plant→zone links;
- finite non-negative link capacities;
- valid `quality_limits.parameters` definitions.

The Output JSON should **record the result of these upstream checks**. It should not bypass `data_loader.py` and attempt to validate raw database data independently.

#### 8.2.3 Preprocessing validation

`validation.preprocessing` records the mathematical-readiness checks performed before model construction:

- complete model parameter dictionaries;
- source lower/upper bound consistency;
- plant lower/upper bound consistency;
- capacity-feasibility screening;
- necessary quality-feasibility screening.

Preprocessing warnings are kept separately from blocking failures.

Passing the preliminary capacity and quality screens does **not** prove full MILP feasibility. The solver remains the final authority on whether all constraints can be satisfied simultaneously.

#### 8.2.4 Output consistency validation

The final output should be checked against the solved formulation before it is handed downstream.

Version `1.0` reserves explicit checks for:

```text
decision_values_non_negative
source_activation_and_withdrawal_bounds
plant_activation_and_throughput_bounds
source_flow_conservation
plant_flow_conservation
source_to_plant_link_capacity
plant_to_zone_link_capacity
demand_satisfaction
plant_inflow_quality_limits
objective_cost_reconciliation
summary_total_reconciliation
reported_ids_and_arcs_match_model_parameters
```

These checks correspond directly to the formulation and prevent a reporting bug from producing JSON that disagrees with the actual solved model.

The default numeric tolerance in the contract is:

```text
1e-6
```

The final implementation may centralise this tolerance if the solver integration establishes a project-wide numerical tolerance.

---

### 8.3. `solver`

```json
"solver": {
  "status": "NOT_SOLVED",
  "is_feasible": null,
  "is_optimal": null,
  "objective_value": null
}
```

The external contract should use a small stable solver vocabulary:

```text
OPTIMAL
INFEASIBLE
UNBOUNDED
NOT_SOLVED
UNDEFINED
```

Raw Pyomo or HiGHS status values should be normalised into this field.

#### Feasibility gate

Backend, AI and App consumers should use `is_feasible` and `is_optimal` rather than interpreting solver-specific integer codes.

If the result is infeasible or unsolved:

- no normal source-selection explanation should be produced;
- decision fields should not be presented as a valid solution;
- `objective_value` should be `null` where no valid solved objective exists.

---

### 8.4. `summary`

The summary provides a compact result for consumers that do not need every model entity.

```text
total_demand_ml_per_day
total_withdrawal_ml_per_day
total_treated_ml_per_day
total_delivered_ml_per_day
selected_source_count
active_plant_count
costs
```

For the current scenario, demand is already known:

```text
total_demand_ml_per_day = 500
```

The remaining totals depend on the solved model and remain `null` in the contract template.

#### Cost breakdown

The current objective contains four cost components:

```text
source_fixed_cost
source_variable_cost
plant_fixed_cost
plant_treatment_cost
```

The JSON reports them separately and also reports:

```text
total_cost
```

`total_cost` should reconcile to `solver.objective_value` within the agreed numerical tolerance.

---

### 8.5. `sources`

The output contains one record per scenario source so source identity remains stable from input to result.

Current source IDs are:

```text
silvan_reservoir
yarra_kew
groundwater_bore_1
```

Each source contains four categories of information.

#### 8.5.1 Scenario/input context

```text
source_id
source_name
source_type
enabled_in_scenario
forced_inactive
fixed_activation_cost
```

#### 8.5.2 Resolved model context

```text
model_included
minimum_withdrawal_ml_per_day
maximum_withdrawal_ml_per_day
withdrawal_bounds_origin
cost_per_ml
database_model_ready
availability_status
has_estimated_values
provenance
```

The current scenario specifies a minimum withdrawal of `0` for all three sources.

The maximum withdrawal is intentionally left `null` in the contract template because each scenario entry has:

```text
max_available_ml_per_day_override = null
```

Therefore, the effective upper withdrawal must come from the configured source data and then flow through `ScenarioData` and `ModelParameters`.

It must **not** be copied from the source→plant link capacity.

For example:

```text
source maximum withdrawal
```

and:

```text
silvan_reservoir -> facility_1 maximum flow = 350 ML/day
```

are different parameters and may have different values.

#### 8.5.3 Solver decision

```text
activated
withdrawal_ml_per_day
selection_status
```

Recommended `selection_status` values are:

```text
PENDING
SELECTED
UNUSED
EXCLUDED
```

Meaning:

- `PENDING`: contract template or solver not completed;
- `SELECTED`: source entered the model and is activated/used in the solved result;
- `UNUSED`: source entered the model but receives zero solved withdrawal;
- `EXCLUDED`: source was removed before model construction, for example because it was disabled or forced inactive.

An excluded source is not a source the solver "rejected". The solver never considered it.

#### 8.5.4 Derived reporting/evidence

```text
utilisation_percent
variable_withdrawal_cost
total_source_cost
decision_evidence
```

`decision_evidence` contains deterministic facts that can support the AI explanation layer:

```text
unit_cost_rank
at_minimum_withdrawal
at_maximum_withdrawal
binding_constraints
```

`unit_cost_rank` is context only. It must not be presented as proof that cost alone caused selection.

---

### 8.6. `plants`

The current plant is:

```text
facility_1
```

The output preserves the scenario's capacity naming:

```text
minimum_processing_capacity_ml_per_day
maximum_processing_capacity_ml_per_day
```

and reports solved:

```text
activated
throughput_ml_per_day
utilisation_percent
```

Plant throughput is derived from source→plant inflow:

```text
throughput[t] = sum(source_plant_flow[s, t])
```

When the plant is active, its solved throughput must satisfy the lower and upper plant bounds used by the final formulation.

The current scenario has:

```text
minimum_processing_capacity_ml_per_day = 0
maximum_processing_capacity_ml_per_day = 600
fixed_activation_cost = 0
treatment_cost_per_ml = 64
```

---

### 8.7. `flows.source_to_plant`

Every item corresponds to an arc in the model set `A_ST`.

The current scenario has:

```text
silvan_reservoir -> facility_1       capacity 350
yarra_kew -> facility_1              capacity 300
groundwater_bore_1 -> facility_1     capacity 60
```

Each result reports:

```text
source_id
plant_id
enabled_in_scenario
activated
flow_ml_per_day
maximum_flow_ml_per_day
utilisation_percent
```

This maps directly to the source→plant binary/continuous formulation pair:

```text
gamma_st
b_st
```

The solved flow must never exceed the corresponding link capacity.

---

### 8.8. `flows.plant_to_zone`

The current scenario contains:

```text
facility_1 -> zone_1
maximum_flow_ml_per_day = 600
```

Each result maps to:

```text
delta_tz
c_tz
```

and reports:

```text
plant_id
zone_id
enabled_in_scenario
activated
flow_ml_per_day
maximum_flow_ml_per_day
utilisation_percent
```

---

### 8.9. `demand_zones`

The current demand-zone result uses:

```text
zone_id
zone_name
demand_must_be_met
demand_ml_per_day
delivered_ml_per_day
surplus_ml_per_day
unmet_demand_ml_per_day
demand_satisfied
```

For the current scenario:

```text
zone_1 demand = 500 ML/day
```

The current mathematical formulation uses hard demand satisfaction:

```text
sum_t plant_zone_flow[t, z] >= demand[z]
```

Therefore a feasible solved result must have:

```text
delivered_ml_per_day >= demand_ml_per_day
```

`surplus_ml_per_day` is:

```text
max(0, delivered - demand)
```

`unmet_demand_ml_per_day` should be `0` for a valid feasible result under the current hard-demand formulation.

---

### 8.10. `quality`

Quality is evaluated at:

```text
blend_at_plant_inflow
```

The formulation constrains the blended incoming water at each treatment plant.

The Output JSON therefore reports quality by:

```text
plant
    -> parameter
```

rather than by demand zone.

#### pH

The output keeps both:

- human-readable raw/reporting pH;
- model-facing hydrogen-ion concentration.

Fields include:

```text
parameter_id
model_parameter_id
transform
input_unit
input_min
input_max
model_unit
model_value
model_min
model_max
reported_value
reported_unit
within_limits
binding_lower
binding_upper
```

For pH:

```text
parameter_id = pH
model_parameter_id = hydrogen_ion_concentration_mol_l
transform = ph_to_hydrogen_ion
```

The model-facing value must be calculated from the same transformed values already used in `ModelParameters`. The output layer must not apply a second independent preprocessing transform.

#### Alkalinity and turbidity

These use:

```text
transform = identity
```

so input-space and model-space bounds are the same.

Current limits are:

```text
alkalinity: 20 to 100 mg/L CaCO3
turbidity: 0 to 8 NTU
```

---

### 8.11. `binding_constraints_summary`

This section provides deterministic evidence about active limits where solver/constraint information is available.

Example runtime item:

```json
{
  "constraint_type": "source_max_withdrawal",
  "source_id": "silvan_reservoir",
  "plant_id": null,
  "zone_id": null,
  "parameter_id": null,
  "current_value": 350.0,
  "bound_value": 350.0,
  "slack": 0.0,
  "is_binding": true
}
```

This section is useful to the AI layer because it allows explanations to be grounded in actual optimisation evidence.

It should not contain invented causal claims.

Safe:

```text
The source is operating at its maximum withdrawal bound.
```

Unsafe without additional evidence:

```text
The source was selected because it is the cheapest.
```

---

## 8.12. `warnings`

`warnings` carries non-blocking information that is still relevant to downstream interpretation.

It may include:

- preprocessing structural warnings;
- source database readiness warnings;
- estimated/overridden-value warnings where policy allows them;
- output/reporting warnings that do not invalidate the solved result.

Blocking loader or preprocessing failures should prevent normal model execution rather than appear as an otherwise-valid optimal result.

---

## 9. Formulation consistency checks

The final runtime Output JSON should agree with the mathematical model.

### 9.1 Non-negativity

All continuous solved volumes must be non-negative within tolerance:

```text
withdrawal >= 0
source_to_plant flow >= 0
plant_to_zone flow >= 0
```

### 9.2 Source activation and bounds

For each model source:

```text
minimum_withdrawal * activated
    <= withdrawal
    <= maximum_withdrawal * activated
```

When a source is inactive, its withdrawal should be zero within tolerance.

### 9.3 Plant activation and bounds

For each model plant:

```text
minimum_throughput * activated
    <= incoming throughput
    <= maximum_throughput * activated
```

### 9.4 Source flow conservation

For each source:

```text
withdrawal[source]
=
sum(source_to_plant_flow[source, plant])
```

### 9.5 Plant flow conservation

For each plant:

```text
sum(source_to_plant_flow[source, plant])
=
sum(plant_to_zone_flow[plant, zone])
```

### 9.6 Link capacities

For each model arc:

```text
flow <= maximum_flow * activated
```

### 9.7 Demand

For each zone:

```text
delivered >= demand
```

### 9.8 Quality

For each plant and model quality parameter, the blended incoming value must remain between the transformed lower and upper limits.

### 9.9 Objective reconciliation

The reported cost breakdown must reconcile to:

```text
solver.objective_value
```

within numerical tolerance.

### 9.10 Identifier and arc consistency

Every reported model source, plant, zone and active arc must correspond to the final `ModelParameters` sets and arc sets.

The output must not invent a route that was not part of the model.

---

## 22. Behaviour for infeasible or unsolved models

The same top-level JSON contract should still be used.

For example:

```json
"solver": {
  "status": "INFEASIBLE",
  "is_feasible": false,
  "is_optimal": false,
  "objective_value": null
}
```

When no valid solution exists:

- model-derived decision values should remain `null` or be omitted only if a later contract version explicitly allows omission;
- no optimal-selection claim should be generated;
- `binding_constraints_summary` should only contain meaningful solver diagnostics if they are genuinely available;
- warnings/diagnostics should explain that there is no valid solved allocation.

Using one stable shape reduces special-case logic in the Backend, AI and App layers.

---

## 23. Why this output matters to downstream teams

### Backend

The Backend receives one predictable result structure rather than Pyomo objects or mathematical notation.

### AI

The AI layer receives deterministic evidence such as:

```text
activated
withdrawal
cost
utilisation
binding constraints
solver status
estimated-value metadata
```

This supports explanation without asking an LLM to infer solver logic from incomplete information.

### App / Frontend

The App can directly display:

- solver status;
- total cost;
- selected/unused sources;
- source withdrawals;
- plant throughput;
- network flows;
- demand satisfaction;
- blended water quality;
- warnings and traceability information.

### MILP

The model team can change internal variable names without breaking downstream consumers, provided the mapping to this JSON contract remains stable.

---

## 24. PR integration note

Recommended PR paths:

```text
MILP/
├── docs/
│   └── output_json.md
└── examples/
    └── results/
        └── output_contract_v1.json
```

This PR defines the contract only.

It does not require:

```text
postprocessing.py
```

and does not take ownership of the model-output implementation.

The team implementing `model.py` / result serialization should use this document as the agreed boundary.

---

## 25. Final integration checklist

Before the Output JSON is treated as production-ready, confirm:

- [ ] final `model.py` Pyomo variable names are known;
- [ ] those variables are mapped to the stable JSON fields;
- [ ] solver status is normalised;
- [ ] loader validation status/issues are carried forward;
- [ ] preprocessing warnings/check results are carried forward;
- [ ] source IDs match `ModelParameters.source_ids`;
- [ ] plant IDs match `ModelParameters.plant_ids`;
- [ ] zone IDs match `ModelParameters.zone_ids`;
- [ ] source→plant arcs match `ModelParameters.source_plant_arcs`;
- [ ] plant→zone arcs match `ModelParameters.plant_zone_arcs`;
- [ ] source withdrawals reconcile with source→plant flows;
- [ ] plant inflow reconciles with plant outflow;
- [ ] demand results satisfy the current hard-demand formulation;
- [ ] pH uses the preprocessed hydrogen-ion model representation;
- [ ] objective components reconcile to the solver objective;
- [ ] AI explanations consume evidence, not invented reasons;
- [ ] Backend and App consumers agree on contract version `1.0`.

---

## 26. Contract principle

The purpose of this file is not to freeze the internal implementation of the MILP.

It freezes the **boundary between the MILP and the rest of AquaBlend**.

That boundary should remain stable even while the formulation, Pyomo variable names, solver configuration and internal model structure continue to evolve.
