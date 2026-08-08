# Demand, Source Activation and Source Capacity Constraints

## Purpose

This module implements the demand, source-activation and source-capacity
constraints for the AquaBlend MILP model. It uses the formulation-ready
`ModelParameters` object produced by `preprocessing.py`.

The implementation does not define a second input contract and does not
create duplicate decision variables. The required variables are created by
the shared model-building layer and passed to the constraint function.

## Input contract

The constraint implementation accepts `ModelParameters` and uses the
following fields:

- `source_ids`
- `zone_ids`
- `source_plant_arcs`
- `plant_zone_arcs`
- `demand_by_zone`
- `source_min_withdrawal`
- `source_max_withdrawal`

Validation and normalisation of scenario data are handled by
`data_loader.py` and `preprocessing.py`.

## Required decision variables

The model-building layer must provide these variable groups:

- `source_to_plant_flow[(source_id, plant_id)]`
- `plant_to_zone_flow[(plant_id, zone_id)]`
- `source_active[source_id]`

The flow variables are non-negative continuous variables. The source
activation variables are binary.

## Zone demand constraint

Demand is represented separately for every demand zone. For each zone
\(z\), the total flow entering the zone must satisfy its demand:

\[
\sum_{t:(t,z)\in A_{TZ}} x_{tz} \geq D_z
\]

where:

- \(x_{tz}\) is the flow from treatment plant \(t\) to demand zone \(z\);
- \(A_{TZ}\) is the set of enabled plant-to-zone arcs;
- \(D_z\) is the demand of zone \(z\).

Demand values are not aggregated into one system-wide value. This prevents
one zone from receiving excess flow while another zone remains
under-supplied.

## Source activation and capacity constraints

For each source \(s\), total withdrawal is calculated from its outgoing
source-to-plant arcs:

\[
W_s = \sum_{t:(s,t)\in A_{ST}} x_{st}
\]

The withdrawal is linked to the binary source-activation variable:

\[
W_s \geq \underline{W}_s y_s
\]

\[
W_s \leq \overline{W}_s y_s
\]

where:

- \(x_{st}\) is the flow from source \(s\) to plant \(t\);
- \(y_s\) is 1 when source \(s\) is active and 0 otherwise;
- \(\underline{W}_s\) is the optional minimum withdrawal;
- \(\overline{W}_s\) is the maximum source capacity.

If a source is inactive, its withdrawal must be zero. If it is active, its
withdrawal must remain between its configured minimum and maximum values.

## Responsibility boundaries

This module is responsible only for:

- zone-level demand satisfaction;
- source activation;
- optional minimum source withdrawal;
- maximum source capacity.

Plant throughput, plant flow balance, water-quality constraints, objective
terms and solver invocation are handled by their corresponding model
components.

## Tests

The test suite checks:

- expected constraint creation;
- feasible multi-zone demand;
- separate enforcement of demand for each zone;
- zero withdrawal from an inactive source;
- minimum withdrawal from an active source;
- maximum source capacity;
- clear errors for missing arc variables;
- clear errors for missing activation variables.

Run the tests with:

```bash
python -m pytest MILP/tests/test_constraints.py -v