# MILP model output contract — specification

Companion to [`model_output_contract.json`](model_output_contract.json). It defines what every field in that file reports: meaning, unit, and the consistency rules a producer must satisfy.

**Ground truth is the formulation.** 

## 1. Where each decision variable is reported

| Formulation | Path |
|---|---|
| $\alpha_s$ | membership of `sources.selected[]` vs `sources.unused[]` |
| $a_s$ | `sources.selected[].volume_drawn_ml_per_day` |
| $\beta_t$ | membership of `plants.active[]` vs `plants.inactive[]` |
| $\gamma_{st}$ | `transfer_paths.source_to_plant[].active` |
| $b_{st}$ | `transfer_paths.source_to_plant[].flow_ml_per_day` |
| $\delta_{tz}$ | `transfer_paths.plant_to_zone[].active` |
| $c_{tz}$ | `transfer_paths.plant_to_zone[].flow_ml_per_day` |

| Objective term | Path |
|---|---|
| $\sum_s F_s \alpha_s$ | `objective.cost_breakdown.source_activation_cost` |
| $\sum_t F_t \beta_t$ | `objective.cost_breakdown.plant_activation_cost` |
| $\sum_s C_s a_s$ | `objective.cost_breakdown.source_draw_cost` |
| $\sum_t C_t \sum_s b_{st}$ | `objective.cost_breakdown.plant_treatment_cost` |

The four terms are the whole objective, so they must sum to `objective.total_cost` exactly.

## 2. Conventions

- **Naming follows the `data_loader.py`.** `plant_id`, `ml_per_day`, `cost_per_ml` — the same keys the input contract
  uses, so `source_id`, `plant_id` and `zone_id` join across the two files without a mapping layer.
- **Single period.** One representative day. Every `_ml_per_day` figure is a volume for that day; every cost
  is a cost for that day.
- **Only solved quantities appear.** A field here is a decision variable, a term of the objective, a
  constraint residual, or a value echoed from the input for readability. Nothing is inferred from outside the
  model.
- **`null` means not applicable**, not zero. `constraints[].binding` is `null` for equality constraints,
  which are always tight and for which the concept carries no information.

## 3. Field specification

### 3.1 Top level

| Field | Type | Rule |
|---|---|---|
| `scenario_id` | string | Must equal the input contract's `scenario_id`. |
| `solved_at` | string | ISO 8601 UTC with `Z`. When the solve finished. |
| `status` | string | Solver status. `"OPTIMAL"` is the only value for which the rest of the file is meaningful; consumers gate on it (Task 6 §6). Other values: `INFEASIBLE`, `UNBOUNDED`, `TIME_LIMIT`, `ERROR`. |
| `objective` | object | §3.2. |
| `demand_zones` | array | §3.3. |
| `sources` | object | §3.4. |
| `transfer_paths` | object | §3.5. |
| `plants` | object | §3.6. |
| `water_quality` | object | §3.7. |
| `constraints` | array | §3.8. |
| `binding_constraints_summary` | array | §3.8. |
| `alternative_feasible_solutions` | array | §3.9. |
| `sensitivity_to_key_assumptions` | array | §3.9. |
| `explanation` | string | §3.9. |
| `diagnostics` | object | §3.10. |
| `data_flags` | object | §3.11. |

When `status` is not `OPTIMAL`, `objective` and the solution blocks should be omitted rather than filled with
zeros — a zero-cost blend reads as a valid answer.

### 3.2 `objective`

| Field | Type | Rule |
|---|---|---|
| `total_cost` | number | Objective value. Must equal the sum of `cost_breakdown`. |
| `currency` | string | ISO 4217. Matches the currency the input costs were given in. |
| `unit` | string | `"cost for one representative day"`. |
| `cost_breakdown.source_activation_cost` | number | $\sum_s F_s \alpha_s$. Currently always `0.00` — see §6. |
| `cost_breakdown.plant_activation_cost` | number | $\sum_t F_t \beta_t$. |
| `cost_breakdown.source_draw_cost` | number | $\sum_s C_s a_s$. Must equal the sum of `sources.selected[].draw_cost`. |
| `cost_breakdown.plant_treatment_cost` | number | $\sum_t C_t \sum_s b_{st}$, charged on plant **inflow**. Must equal the sum of `plants.active[].treatment_cost`. |

All four keys are always present. A term that evaluates to zero is reported as `0.00`, not omitted, so the
breakdown always has the same shape as the objective.

### 3.3 `demand_zones[]`

| Field | Type | Rule |
|---|---|---|
| `zone_id` | string | Joins to the input's `network.demand_zones[].zone_id`. |
| `zone_name` | string | Echoed from the input's `name`. |
| `demand_ml_per_day` | number | $D_z$, echoed from the input. |
| `volume_supplied_ml_per_day` | number | $\sum_t c_{tz}$. Must be $\ge$ `demand_ml_per_day`; demand is a floor, not a target, so a strict excess is legal. |

Every zone in the input appears here, whether or not it was supplied above its floor.

### 3.4 `sources`

`selected[]` holds sources with $\alpha_s = 1$; `unused[]` holds $\alpha_s = 0$. Every source in the input
appears in exactly one of the two.

| Field | Array | Rule |
|---|---|---|
| `source_id`, `source_name`, `source_type` | both | Echoed from the database view via the loader. |
| `volume_drawn_ml_per_day` | selected | $a_s$. Must equal $\sum_t b_{st}$ for that source (source flow conservation). |
| `percent_of_blend` | selected | Share of total drawn volume — see §4. |
| `cost_per_ml` | selected | $C_s$, echoed from the view. |
| `draw_cost` | selected | $C_s a_s$. Must equal `volume_drawn_ml_per_day` × `cost_per_ml`. |
| `reason` | unused | Free text explaining non-selection. Consumed verbatim by the explanation layer (Task 6). |

A source with $\alpha_s = 1$ but $a_s = 0$ is legal in principle but should be reported in `selected[]` with
a zero volume, not moved to `unused[]` — the two arrays report the binary, not the flow.

### 3.5 `transfer_paths`

Two arrays, one per arc layer, mirroring the input's `source_to_plant_links` / `plant_to_zone_links`. Every
link in the input appears here, including inactive ones.

| Field | Rule |
|---|---|
| `path_id` | Unique across both arrays. Convention `<from>_to_<to>` using the endpoint ids. |
| `source_id` / `plant_id` / `zone_id` | Endpoints; must resolve in the input contract. |
| `active` | $\gamma_{st}$ or $\delta_{tz}$ as a boolean. |
| `flow_ml_per_day` | $b_{st}$ or $c_{tz}$. Must be `0` whenever `active` is `false`. |

### 3.6 `plants`

`active[]` holds plants with $\beta_t = 1$, `inactive[]` those with $\beta_t = 0$.

| Field | Rule |
|---|---|
| `plant_id`, `plant_name` | Joins to the input's `network.plants[].plant_id` / `name`. |
| `volume_processed_ml_per_day` | $\sum_s b_{st}$, the plant's inflow. Must equal $\sum_z c_{tz}$ (plant flow conservation) and must not exceed the input's `maximum_processing_capacity_ml_per_day`. |
| `treatment_cost_per_ml` | $C_t$, echoed from the input. |
| `treatment_cost` | $C_t \sum_s b_{st}$. |

### 3.7 `water_quality`

| Field | Rule |
|---|---|
| `applies_to` | `"blend_at_plant_inflow"`. The formulation constrains the blend arriving at a plant and has no treatment-removal term, so there is no post-treatment figure to report. |
| `by_plant.<plant_id>.<parameter>.value` | $\left(\sum_s Q_{sp} b_{st}\right) / \sum_s b_{st}$, the volume-weighted blend at that plant. |
| `by_plant.<plant_id>.<parameter>.unit` | Echoed from the input's `quality_limits.parameters.<p>.unit`. |
| `by_plant.<plant_id>.<parameter>.constraint_min` / `constraint_max` | $\underline{Q}_p$, $\overline{Q}_p$, echoed from the input. |
| `by_plant.<plant_id>.<parameter>.status` | `"PASS"` when the value lies within the limits, `"FAIL"` otherwise. `FAIL` can only appear on an infeasible or unsolved report. |
| `by_plant.<plant_id>.<parameter>.safety_margin_percent` | See §4. |

Keyed by plant because the constraint is per plant per parameter — with several plants, each blends
differently. A plant with zero inflow has no defined blend and is omitted.

### 3.8 `constraints[]` and `binding_constraints_summary`

One entry per constraint row in the model. Names are `<constraint>_<entity ids>` so they can be parsed back
to the entity they came from.

| `type` | Formulation | Naming |
|---|---|---|
| `inequality` | demand, source capacity, plant capacity, link capacity | `demand_satisfaction_<zone>`, `source_capacity_<source>`, `plant_capacity_<plant>`, `link_capacity_<from>_to_<to>` |
| `equality` | source and plant flow conservation | `source_flow_conservation_<source>`, `plant_flow_conservation_<plant>` |
| `logical` | $\gamma_{st} \le \alpha_s$, $\delta_{tz} \le \beta_t$ | `link_requires_active_source_<from>_to_<to>`, `link_requires_active_plant_<from>_to_<to>` |
| `ranged` | two-sided quality limits | `quality_range_<parameter>_<plant>` |

| Field | Rule |
|---|---|
| `type` | One of the four above. |
| `status` | `"PASS"`, or `"INACTIVE"` when the constraint's entity is deactivated so both sides are zero and tightness carries no meaning. |
| `slack` | Distance from the binding point, in the constraint's own units — see §4. |
| `binding` | `true` when `slack` is zero and the row genuinely limits the solution. `null` for `equality` rows, which are always tight by construction. |

`binding_constraints_summary` is the list of names where `binding` is `true` **and** `type` is `inequality`
or `ranged`. Equality rows are excluded because they are always tight, and `logical` rows because a satisfied
$\gamma_{st} = \alpha_s = 1$ is tight without limiting anything — including either would bury the genuinely
limiting constraints in structural noise. This list is the sole input to the binding-constraints explanation
template.

### 3.9 Analysis blocks

`alternative_feasible_solutions[]`, `sensitivity_to_key_assumptions[]` and `explanation` are **not solver
output**. They are produced by the analysis layer from the solved model, and a bare solve may legitimately
leave the arrays empty and the string absent.

| Field | Rule |
|---|---|
| `alternative_feasible_solutions[].description` | What the alternative changes, in plain language. |
| `alternative_feasible_solutions[].total_cost` | Objective value of the alternative. |
| `alternative_feasible_solutions[].cost_difference_from_optimal` | `total_cost` minus the optimal; must be $\ge 0$ and must reconcile with `objective.total_cost`. |
| `alternative_feasible_solutions[].notes` | Why the alternative might be preferred despite costing more. |
| `sensitivity_to_key_assumptions[].assumption` | Names an input field and why it is uncertain. |
| `sensitivity_to_key_assumptions[].impact` | What changes if the assumption is wrong. |
| `explanation` | Operator-readable summary. Must not contradict the structured fields. |

### 3.10 `diagnostics`

| Field | Rule |
|---|---|
| `solver` | Solver name. |
| `solve_time_seconds` | Wall clock. |
| `optimality_gap` | Final MIP gap; `0.0` for a proven optimum. |
| `num_continuous_variables` | $a_s$, $b_{st}$, $c_{tz}$ — one per source, per source-plant arc, per plant-zone arc. |
| `num_binary_variables` | $\alpha_s$, $\beta_t$, $\gamma_{st}$, $\delta_{tz}$. |
| `num_integer_variables` | General integers. The formulation has none, so `0`. |
| `num_constraints` | Must equal `len(constraints)`. Counts ranged quality rows once, and excludes variable bounds and non-negativity. |

### 3.11 `data_flags`

| Field | Rule |
|---|---|
| `sources[].source_id` | Every source in the solve. |
| `sources[].has_estimated_values` | Echoed from `SourceInput.has_estimated_values`. |
| `sources[].availability_origin` | `"database"` or `"scenario_override"`, echoed from the loader. |
| `sources[].provenance` | The five provenance strings the view supplies, echoed unchanged. |
| `notes[]` | Free text for anything the per-source provenance cannot express. |

Source provenance is echoed from the database rather than hand-written, so it cannot drift. Fields defined in
the scenario file — plant costs, capacities, link capacities, quality limits — have no provenance mechanism,
which is what `notes[]` currently covers.

## 4. Derived values and rounding

- `percent_of_blend` = $a_s / \sum_{s'} a_{s'} \times 100$, over selected sources only. One decimal place.
- `slack`, by constraint type:
  - `inequality` of the form $\text{lhs} \le \text{rhs}$: `rhs - lhs`, in the constraint's own units.
  - `ranged` $\underline{Q}_p \le v \le \overline{Q}_p$: `min(v - min, max - v)` — the distance to whichever
    bound is nearer, so a single number still means "how much room is left".
  - `equality`: always `0.0`.
  - `logical`: `alpha - gamma` or `beta - delta`.
- `safety_margin_percent` = `min(value - constraint_min, constraint_max - value) / (constraint_max - constraint_min) × 100`.
  Zero means the blend sits exactly on a limit; 50 means it sits at the centre of the window. One decimal
  place, rounding half up.
- Volumes: whole ML where the solver returns integers, otherwise as solved.
- Costs: two decimal places.
- Quality values: two decimal places, matching the precision the blend arithmetic supports.

## 5. Consistency rules a producer must satisfy

Mechanically checkable, and worth asserting before publishing a result:

1. `cost_breakdown` sums to `total_cost`.
2. Each selected source's `draw_cost` = `volume_drawn_ml_per_day` × `cost_per_ml`; their sum = `source_draw_cost`.
3. Each active plant's `treatment_cost` = `volume_processed_ml_per_day` × `treatment_cost_per_ml`; their sum = `plant_treatment_cost`.
4. `percent_of_blend` sums to 100 across selected sources.
5. Source flow conservation: each source's `volume_drawn_ml_per_day` equals the sum of its outgoing `flow_ml_per_day`.
6. Plant flow conservation: each plant's inflow equals its outflow equals `volume_processed_ml_per_day`.
7. Each zone's `volume_supplied_ml_per_day` $\ge$ `demand_ml_per_day`.
8. `flow_ml_per_day` is `0` on every path with `active: false`.
9. `binding_constraints_summary` equals the `inequality` and `ranged` rows with `binding: true`.
10. `len(constraints)` equals `diagnostics.num_constraints`.
11. Every `source_id`, `plant_id` and `zone_id` resolves in the input contract, and the quality limits echoed
    here match the input's `quality_limits`.

## 6. Known gaps

- **`source_activation_cost` is structurally zero.** The formulation charges $F_s$ per activated source, but
  the loader has no input path for it. The term is reported as `0.00` rather than dropped, so the breakdown
  keeps the same shape as the objective and the gap stays visible. It becomes a real number once the source
  view and `SourceInput` carry the field.
- **Quality is pre-treatment only.** With no removal term in the formulation, there is nothing to report
  after treatment. Any post-treatment figure a stakeholder needs has to come from elsewhere, and must not be
  presented as a model output.
- **Limits are global, not per-plant.** `by_plant` is keyed by plant so the *blend* is reported per plant, but
  the limits it is checked against are the same everywhere, because $\underline{Q}_p, \overline{Q}_p$ carry no
  plant index.
- **`binding` on `logical` rows is honest but not useful.** A satisfied link-activation constraint is tight
  whenever the link is used. It is reported as `binding: true` for fidelity and filtered out of the summary;
  if that distinction proves confusing, the alternative is to stop reporting the rows at all.
- **No shadow prices.** `slack` says how much room a constraint has, not what relaxing it is worth. Ranking
  constraints by economic impact needs dual values, which this contract does not carry.
