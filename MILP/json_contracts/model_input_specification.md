# MILP model input contract — specification

Companion to [`model_input_contract.json`](model_input_contract.json). It defines what every placeholder in
that file is expected to contain: type, unit, required/optional, and the rules a producer must satisfy.

**Ground truth is `data_loader.py`.** The notation is adopted from the official mathematical documentation. This specification md aligns with two aforementioned files, committed in $26^\text{th}$ July 2026.


## 1. Where each formulation parameter comes from

Only about half the model's parameters live in this file. Source attributes are read from a Supabase view; the scenario file selects *which* sources are in play and describes the network around them.

| Formulation | Source of truth | Path |
|---|---|---|
| $D_z$ | this file | `network.demand_zones[].demand_ml_per_day` |
| $F_t$ | this file | `network.plants[].fixed_activation_cost` |
| $C_t$ | this file | `network.plants[].treatment_cost_per_ml` |
| $\underline{V}_t$ | this file | `network.plants[].minimum_processing_capacity_ml_per_day` |
| $\overline{V}_t$ | this file | `network.plants[].maximum_processing_capacity_ml_per_day` |
| $\overline{L}_{st}$ | this file | `network.source_to_plant_links[].maximum_flow_ml_per_day` |
| $\overline{L}_{tz}$ | this file | `network.plant_to_zone_links[].maximum_flow_ml_per_day` |
| $\underline{Q}_p, \overline{Q}_p$ | this file | `quality_limits.parameters.<p>.min` / `.max` |
| $\mathcal{S}$ | this file | `sources[].source_id` (selection only) |
| $C_s$ | **database** | `cost_per_ml` |
| $\underline{W}_s$ | this file | `sources[].minimum_withdrawal_ml_per_day`  |
| $\overline{W}_s$ | **database** | `max_available_ml_per_day`, overridable per §3.3 |
| $Q_{sp}$ | **database** | `representative_ph`, `representative_alkalinity_mg_l_caco3`, `representative_turbidity_ntu` |
| $F_s$ | **nowhere** | see §6 in the maths documentation |


## 2. Conventions

- **Naming follows the loader.** The output contract now does too, so the two join without translation.

- **Single period.** One representative day, no time index. `ml_per_day` quantities are volumes for that day;
  costs are for that day.
- **IDs are the join keys.** `source_id` must match the database view's `source_id`. `plant_id` and `zone_id`
  are defined here and referenced by the link arrays. The example keeps the id *value* `facility_1`, which the
  output contract carries under the same `plant_id` key.
- **Absence of a link means no arc.** A source-plant pair with no entry in `network.source_to_plant_links`
  has no $b_{st}$ variable. Do not write an entry with `maximum_flow_ml_per_day: 0` to mean the same thing.
- **`null` means "not supplied", and is accepted** the validation is of `data_loader.py`'s responsibility.

## 3. Field specification

### 3.1 Top level

| Field | Type | Required | Rule |
|---|---|---|---|
| `scenario_id` | string | yes | Identifies the run; must match the output contract's `scenario_id`. Read as `str(...).strip()`. |
| `scenario_name` | string | yes | Human-readable label, shown by `print_summary()`. |
| `status` | string | no | Defaults to `"draft"`. Free text; the loader does not branch on it. |
| `description` | string | no | Defaults to `""`. |
| `data_source` | object | **yes** | Missing or non-object raises immediately. See §3.2. |
| `validation` | object | no | Defaults to `{}`, which means every flag takes its default of `true`. See §3.2. |
| `sources` | array | **yes** | Selection list. See §3.3. |
| `network` | object | **yes** | See §3.4–3.6. |
| `quality_limits` | object | **yes** | Must be an object; its *shape* is not checked by the loader. See §3.7. |
| `treatment` | object | no | Defaults to `{}`. Carried through to `ScenarioData.treatment` and otherwise unused. |

### 3.2 `data_source` and `validation`

| Field | Type | Required | Rule |
|---|---|---|---|
| `data_source.type` | string | yes | Must be exactly `"supabase"`; anything else raises. |
| `data_source.view` | string | yes | Schema-qualified view name. Must match `^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$` or it is rejected as unsafe. The view must expose all 23 columns the loader selects. |
| `data_source.allow_estimated_values` | boolean | no | Defaults to `false`. **The example sets `true` out of necessity:** every source in the reference data carries `cost_is_estimated`, so `false` fails all three with "contains estimated or overridden values". Setting `true` is an acknowledgement that the run is built on placeholder data. |
| `validation.fail_if_source_missing_from_database` | boolean | no | Default `true`. When `false`, missing sources are reported as a "Skipped database sources" note instead. |
| `validation.fail_if_daily_availability_missing` | boolean | no | Default `true`. Skipped for a source marked `forced_inactive`. |
| `validation.fail_if_required_quality_value_missing` | boolean | no | Default `true`. Checks pH, alkalinity, turbidity **and cost** — the loader groups cost under this flag. |
| `validation.fail_if_demand_missing` | boolean | no | Default `true`. Only applies to zones with `demand_must_be_met: true`. |


### 3.3 `sources[]` — selection, not definition

| Field | Type | Required | Rule |
|---|---|---|---|
| `source_id` | string | yes | Must exist in the database view **and** have `is_active = TRUE` there — the fetch filters on it, so a deactivated row simply does not come back and reads as "not found in the database". |
| `enabled` | boolean | no | Default `true`. `false` drops the source before the database query; it is not fetched and not modelled. |
| `forced_inactive` | boolean | no | Default `false`. The source is still loaded, but is expected to be held at $\alpha_s = 0$. It also exempts the source from the missing-availability check. |
| `minimum_withdrawal_ml_per_day` | number | no | $\underline{W}_s$, defaults to `0`. Floor on the draw *when the source is activated*: $a_s \ge \underline{W}_s \alpha_s$. At `0` the row reduces to $a_s \ge 0$, so it is inert and the model behaves exactly as before. Must satisfy $\underline{W}_s \le \overline{W}_s$.|
| `max_available_ml_per_day_override` | number or null | no | `null` (or absent) uses the database value. A number replaces $\overline{W}_s$ and marks the source as containing estimated values, which fails the load when `allow_estimated_values` is `false`. Use it for what-if runs, not to patch bad data. |


### 3.4 `network.plants[]` — $t \in \mathcal{T}$

| Field | Type | Required | Rule |
|---|---|---|---|
| `plant_id` | string | yes | Unique. Referenced by both link arrays. |
| `name` | string | **yes** | Read as `str(item["name"])` with no default — omitting it raises `KeyError`, not a friendly error. |
| `enabled` | boolean | no | Default `true`. |
| `minimum_processing_capacity_ml_per_day` | number | no | $\underline{V}_t$, minimum daily throughput, defaults to `0`. Floor on inflow *when the plant is activated*: $\sum_s b_{st} \ge \underline{V}_t \beta_t$. At `0` the row reduces to $\sum_s b_{st} \ge 0$, so it is inert. Must satisfy $\underline{V}_t \le \overline{V}_t$. Renamed from `minimum_operating_flow_ml_per_day`. |
| `maximum_processing_capacity_ml_per_day` | number or null | no | $\overline{V}_t$. `null` is accepted by the loader and means unbounded, which the model should reject. |
| `fixed_activation_cost` | number | no | $F_t$, defaults to `0`. `0` is correct for the toy case, where the single plant is always on and its fixed cost is a constant the objective drops. |
| `treatment_cost_per_ml` | number | no | $C_t$, defaults to `0`. Charged on inflow $\sum_s b_{st}$. Chemical and energy costs fold into this one rate — the formulation has no separate dosing or energy term. |

### 3.5 `network.demand_zones[]` — $z \in \mathcal{Z}$

| Field | Type | Required | Rule |
|---|---|---|---|
| `zone_id` | string | yes | Unique. |
| `name` | string | **yes** | Same `KeyError` behaviour as `plants[].name`. |
| `demand_ml_per_day` | number or null | no | $D_z$. Enters as $\sum_t c_{tz} \ge D_z$, so it is a floor, not a target. |
| `demand_must_be_met` | boolean | no | Default `true`. `false` exempts the zone from the missing-demand check and implies soft demand, which the formulation does not model — see §6. |

### 3.6 `network.source_to_plant_links[]` and `network.plant_to_zone_links[]`

| Field | Type | Required | Rule |
|---|---|---|---|
| `source_id` / `plant_id` / `zone_id` | string | yes | Must resolve; the loader checks link endpoints against the loaded plants, zones and sources and reports unknown references. |
| `enabled` | boolean | no | Default `true`. A disabled link should be treated as absent by the model — the loader keeps it in the tuple either way. |
| `maximum_flow_ml_per_day` | number or null | no | $\overline{L}_{st}$ / $\overline{L}_{tz}$. `null` means unbounded. |


### 3.7 `quality_limits` — $p \in \mathcal{P}$


| Field | Type | Rule |
|---|---|---|
| `applies_to` | string | `"blend_at_plant_inflow"`. Records where the limit binds — see §6, this is not the same as the regulatory post-treatment limit. |
| `parameters.<p>.unit` | string | Unit of the limits and of the database's representative value. |
| `parameters.<p>.min` | number | $\underline{Q}_p$. Use a value the blend cannot realistically breach (e.g. `0`) when only an upper limit is regulated. |
| `parameters.<p>.max` | number | $\overline{Q}_p \ge$ `min`. |
| `parameters.<p>.transform` | string | `"identity"` if the parameter is kept identical to what it provided in the json; otherwise name the transform and supply transformed values, e.g. `"hydrogenic"` for `pH` to be linearised into $[H^+]$. |
