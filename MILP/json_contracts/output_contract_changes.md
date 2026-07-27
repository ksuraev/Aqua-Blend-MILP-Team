# Output contract — what changed and why

Records the differences between the original `model_output_contract.json` and the corrected version, so the
edits can be reviewed one at a time rather than as one large diff.

Three causes: fields that reported things the formulation does not model, fields the formulation produces that
were never reported, and naming that did not match `data_loader.py`. One arithmetic error was found along the
way.


## 1. Corrected values

| Field | Was | Now | Why |
|---|---|---|---|
| `sources.selected[yarra_kew].cost_contribution` | `68250.00` | `68150.00` | **Arithmetic error.** 290 ML × \$235/ML = 68,150. |
| `objective.cost_breakdown.source_draw_cost` | `152250.00` | `152150.00` | Follows from the above; 84,000 + 68,150. |
| `objective.total_cost` | `184250.00` | `184150.00` | Follows from the above. |
| `alternative_feasible_solutions[0].cost_difference_from_optimal` | `5150.00` | `5250.00` | 189,400 − 184,150. |
| `diagnostics.num_continuous_variables` | `4` | `7` | $a_s$ (3) + $b_{st}$ (3) + $c_{tz}$ (1). |
| `diagnostics.num_binary_variables` | `4` | `8` | $\alpha_s$ (3) + $\beta_t$ (1) + $\gamma_{st}$ (3) + $\delta_{tz}$ (1). |
| `diagnostics.num_integer_variables` | `1` | `0` | The formulation has no general integer variables. The `1` counted `treatment_batches`, which is not a model variable. |
| `diagnostics.num_constraints` | `8` | `20` | The original omitted flow conservation, link capacity and link-activation rows. |

The original's cost breakdown summed to the original total, so the error was internally consistent and would
not have been caught by a sum check — only by recomputing from the per-source figures.

## 2. Removed — not produced by the model

Each of these described something the formulation has no variable or term for. Reporting them as solver output
presents an assumption as a result.

| Field | Why removed |
|---|---|
| `objective.cost_breakdown.chemical_addition_cost`, `energy_cost` | The formulation charges treatment at a single rate $C_t$ per ML of inflow. Splitting it into chemical and energy is a decomposition the model never performs. Replaced by `plant_treatment_cost`, which is the same 32,000 total. |
| `objective.energy_estimate_kWh` | No energy variable exists. |
| `treatment_facilities.active[].treatment_batches` | No batch variable. Treatment is continuous in ML. |
| `treatment_facilities.active[].chemical_addition[]` | No dosing variable. |
| `treatment_facilities.active[].treatment_removal[]` | No removal term. This is the field that made post-treatment quality look derivable. |
| `water_quality.after_treatment` | With no removal term, the model has no post-treatment state. Its values (pH 7.4, turbidity 2.1) were not solver output. |
| `constraints[].name = "facility_1_batch_capacity"` | No batch constraint. The real one is $\sum_s b_{st} \le \overline{V}_t \beta_t$, now `plant_capacity_facility_1`. |
| `constraints[].name = "groundwater_bore_1_activation"` | Activation and capacity are one constraint, $a_s \le \overline{W}_s \alpha_s$. There is no separate activation row to report. |
| `data_flags.estimated_fields[]` | Hand-written list, superseded by provenance echoed from the database view — see §5. |

## 3. Added — produced by the model but never reported

| Field | Formulation |
|---|---|
| `objective.cost_breakdown.source_activation_cost` | $\sum_s F_s \alpha_s$, the objective's first term. Absent entirely. Reports `0.00` because no input path exists for $F_s$ — see the specification §6. |
| `objective.cost_breakdown.plant_activation_cost` | $\sum_t F_t \beta_t$. Absent entirely. |
| `transfer_paths.source_to_plant[].flow_ml_per_day` | $b_{st}$. The original reported only the binary $\gamma_{st}$, so the volume on each arc was unrecoverable. |
| `transfer_paths.plant_to_zone[]` | $\delta_{tz}$ and $c_{tz}$. The original had no plant-to-zone layer at all, so the delivery side of the network was invisible. |
| `constraints[]`: `source_flow_conservation_*` (3), `plant_flow_conservation_*` (1) | $a_s = \sum_t b_{st}$ and $\sum_z c_{tz} = \sum_s b_{st}$. |
| `constraints[]`: `link_capacity_*` (4) | $b_{st} \le \overline{L}_{st}\gamma_{st}$, $c_{tz} \le \overline{L}_{tz}\delta_{tz}$. |
| `constraints[]`: `link_requires_active_*` (4) | $\gamma_{st} \le \alpha_s$, $\delta_{tz} \le \beta_t$. |
| `constraints[].type` | New field distinguishing `inequality` / `equality` / `logical` / `ranged`, so `binding` can be interpreted correctly — an equality is always tight and a satisfied link-activation row is tight whenever the link is used. Without it, `binding_constraints_summary` would fill with structural noise. |
| `demand_zones[].zone_name` | Echoed for readability, matching `source_name` and `plant_name`. |

## 4. Renamed — breaking for the explanation layer

Naming now follows `data_loader.py`, so `source_id`, `plant_id` and `zone_id` join across the input and output
contracts without a translation layer. **This breaks every explanation template that reads the old paths.**

| Was | Now |
|---|---|
| `treatment_facilities` | `plants` |
| `facility_id`, `facility_name` | `plant_id`, `plant_name` |
| `volume_drawn_ML` | `volume_drawn_ml_per_day` |
| `required_volume_ML` | `demand_ml_per_day` |
| `volume_supplied_ML` | `volume_supplied_ml_per_day` |
| `volume_processed_ML` | `volume_processed_ml_per_day` |
| `cost_per_ML` | `cost_per_ml` |
| `cost_contribution` | `draw_cost` |
| `transfer_paths[]` (flat array) | `transfer_paths.source_to_plant[]`, `transfer_paths.plant_to_zone[]` |
| `water_quality.after_blending`, `water_quality.after_treatment` | `water_quality.by_plant.<plant_id>` |
| `{source}_capacity` | `source_capacity_{source}` |
| `{parameter}_range` | `quality_range_{parameter}_{plant}` |
| `{facility}_batch_capacity` | `plant_capacity_{plant}` |
| `{source}_activation` | *(removed — see §2)* |

Constraint names are now `<constraint>_<entity ids>` throughout, so a name can be parsed back to the entity it
came from. `demand_satisfaction_{zone}` is unchanged.

### Templates that need updating

**Task 6, `Template_SourceSelection.md`** — `volume_drawn_ML`, `cost_per_ML`, the `{source_id}_capacity`
lookup in `binding_constraints_summary`, and `data_flags.estimated_fields` (§8, estimated-value disclosure).
`source_id`, `source_name`, `percent_of_blend`, `reason` and `status` are unchanged.

**Task 7, `Template_BindingConstraints.md`** — every section is affected: the Source-capacity, Treatment-capacity
and Water-quality name patterns, the Source-activation section (its constraint no longer exists),
`treatment_facilities.active[].facility_name`, `volume_processed_ML`, `treatment_batches` (removed, so that
clause needs deleting rather than renaming), the `water_quality.after_treatment.{parameter}` path in the
Water-quality section, and the estimated-value disclosure table.

**Task 8, quality margins** — `after_treatment` no longer exists; margins are now computed on the blend at
plant inflow, against raw-blend limits, using a single defined formula (specification §4). The original
`safety_margin_percent` had no consistent definition: alkalinity (47.7) and turbidity (58.0) both follow
`(max − value) / max`, but pH (21.4) follows neither that nor any other rule that reproduces the other two.

This is an interface change between MILP and AI & Analysis. Worth agreeing before it lands, rather than
landing it and letting three templates break.

## 5. Semantics that changed

**Quality limits are now the blend limits, not the regulatory limits.** `turbidity.constraint_max` moves from
`5.0` to `8.0`. The formulation constrains the blend arriving at a plant and cannot model treatment rescuing an
out-of-limit blend, so the number checked against must be a raw-blend limit — the same value the input
contract carries. 5.0 NTU is the post-treatment regulatory limit and belongs to a stage the model does not
represent. The reported values change accordingly:

| Parameter | Was (`after_treatment`) | Now (blend at plant inflow) |
|---|---|---|
| pH | 7.4 | 7.11 |
| alkalinity | 52.3 | 38.04 |
| turbidity | 2.1 | 5.28 |

The original's `after_blending` figures (7.1, 38.0, 5.3) were already correct — they match the volume-weighted
blend to rounding. Only the invented post-treatment values are gone.

**Quality is now reported per plant.** `water_quality.by_plant.<plant_id>` replaces the flat structure, because
the constraint is per plant per parameter and each plant blends differently once there is more than one. With a
single plant the content is the same, one level deeper.

**Constraint slacks are recomputed** against the blend rather than the post-treatment values, and ranged
constraints now use the distance to the nearer bound. `pH_range` slack moves from 1.1 to 0.61.

**`data_flags` now echoes the database.** Per-source `has_estimated_values`, `availability_origin` and the five
`provenance` strings come straight from `SourceInput`, so they cannot drift from the data. `notes[]` carries
what per-source provenance cannot express — including that `source_activation_cost` is structurally zero, and
that plant costs, capacities, link capacities and quality limits have no provenance mechanism at all.

## 6. Unchanged

`scenario_id`, `solved_at`, `status`, `currency`, `sources.selected` / `unused` structure, `percent_of_blend`,
`reason`, `binding_constraints_summary` as a concept, `alternative_feasible_solutions`,
`sensitivity_to_key_assumptions`, `explanation`, and the `diagnostics` solver fields.

The analysis blocks are kept as-is in shape, but the specification now records that they are produced by the
analysis layer rather than the solver — a bare solve may leave them empty.
