"""Component tests for the AquaBlend Pyomo constraints."""

from dataclasses import replace

import pyomo.environ as pyo
import pytest
from src.model import build_model, solve
from src.preprocessing import ModelParameters


@pytest.fixture
def parameters() -> ModelParameters:
    """Create a small parameter object for constraint testing."""
    return ModelParameters(
        source_ids=("S1", "S2"),
        plant_ids=("T1",),
        zone_ids=("Z1",),
        quality_parameter_ids=("turbidity",),
        source_plant_arcs=(
            ("S1", "T1"),
            ("S2", "T1"),
        ),
        plant_zone_arcs=(("T1", "Z1"),),
        demand_by_zone={"Z1": 30.0},
        source_fixed_cost={
            "S1": 10.0,
            "S2": 20.0,
        },
        plant_fixed_cost={
            "T1": 5.0,
        },
        source_unit_cost={
            "S1": 1.0,
            "S2": 2.0,
        },
        plant_unit_treatment_cost={
            "T1": 0.5,
        },
        source_min_withdrawal={
            "S1": 5.0,
            "S2": 0.0,
        },
        source_max_withdrawal={
            "S1": 40.0,
            "S2": 30.0,
        },
        plant_min_throughput={
            "T1": 0.0,
        },
        plant_max_throughput={
            "T1": 60.0,
        },
        source_plant_link_capacity={
            ("S1", "T1"): 40.0,
            ("S2", "T1"): 30.0,
        },
        plant_zone_link_capacity={
            ("T1", "Z1"): 60.0,
        },
        source_quality={
            ("S1", "turbidity"): 1.0,
            ("S2", "turbidity"): 4.0,
        },
        quality_lower_bound={
            "turbidity": 0.0,
        },
        quality_upper_bound={
            "turbidity": 5.0,
        },
        quality_units={
            "turbidity": "NTU",
        },
        warnings=(),
    )


@pytest.fixture
def model(parameters: ModelParameters) -> pyo.ConcreteModel:
    """Build the Pyomo model used by each test."""
    return build_model(parameters)


def test_all_constraint_families_are_created(
    model: pyo.ConcreteModel,
) -> None:
    """Check that every expected constraint family is present."""
    expected = {
        "demand_satisfaction",
        "source_min_withdrawal",
        "source_max_withdrawal",
        "source_flow_balance",
        "plant_min_throughput",
        "plant_max_throughput",
        "plant_flow_balance",
        "source_plant_link_capacity",
        "plant_zone_link_capacity",
        "source_link_activation",
        "plant_link_activation",
        "quality_lower_bound",
        "quality_upper_bound",
    }

    actual = set(model.component_map(pyo.Constraint))

    assert expected <= actual


def test_constraint_indices_match_network(
    model: pyo.ConcreteModel,
) -> None:
    """Check that constraints use only configured nodes and arcs."""
    assert set(model.demand_satisfaction) == {"Z1"}

    assert set(model.source_flow_balance) == {
        "S1",
        "S2",
    }

    assert set(model.plant_flow_balance) == {
        "T1",
    }

    assert set(model.source_plant_link_capacity) == {
        ("S1", "T1"),
        ("S2", "T1"),
    }

    assert set(model.plant_zone_link_capacity) == {
        ("T1", "Z1"),
    }

    assert set(model.quality_upper_bound) == {
        ("T1", "turbidity"),
    }


def test_feasible_values_satisfy_all_constraints(
    model: pyo.ConcreteModel,
) -> None:
    """Check a manually assigned feasible solution."""
    model.alpha["S1"].set_value(1)
    model.alpha["S2"].set_value(0)

    model.a["S1"].set_value(30.0)
    model.a["S2"].set_value(0.0)

    model.beta["T1"].set_value(1)

    model.gamma["S1", "T1"].set_value(1)
    model.gamma["S2", "T1"].set_value(0)

    model.b["S1", "T1"].set_value(30.0)
    model.b["S2", "T1"].set_value(0.0)

    model.delta["T1", "Z1"].set_value(1)
    model.c["T1", "Z1"].set_value(30.0)

    for constraint in model.component_data_objects(
        pyo.Constraint,
        active=True,
    ):
        body = pyo.value(constraint.body)

        if constraint.has_lb():
            assert body >= pyo.value(constraint.lower) - 1e-8

        if constraint.has_ub():
            assert body <= pyo.value(constraint.upper) + 1e-8


def test_inactive_source_forces_zero_withdrawal(
    model: pyo.ConcreteModel,
) -> None:
    """Check that an inactive source cannot have positive withdrawal."""
    model.alpha["S1"].set_value(0)
    model.a["S1"].set_value(1.0)

    constraint = model.source_max_withdrawal["S1"]

    assert pyo.value(constraint.body) > pyo.value(constraint.upper)


def test_source_balance_detects_unrouted_withdrawal(
    model: pyo.ConcreteModel,
) -> None:
    """Check that source withdrawal must equal source-to-plant flow."""
    model.a["S1"].set_value(20.0)
    model.b["S1", "T1"].set_value(15.0)

    constraint = model.source_flow_balance["S1"]

    assert pyo.value(constraint.body) != pytest.approx(0.0)


def test_demand_constraint_rejects_shortfall(
    model: pyo.ConcreteModel,
) -> None:
    """Check that delivered flow cannot be lower than zone demand."""
    model.c["T1", "Z1"].set_value(29.0)

    constraint = model.demand_satisfaction["Z1"]

    assert pyo.value(constraint.body) < pyo.value(constraint.lower)


def test_plant_balance_rejects_unequal_flows(
    model: pyo.ConcreteModel,
) -> None:
    """Check that plant inflow must equal plant outflow."""
    model.b["S1", "T1"].set_value(30.0)
    model.b["S2", "T1"].set_value(0.0)
    model.c["T1", "Z1"].set_value(25.0)

    constraint = model.plant_flow_balance["T1"]

    assert pyo.value(constraint.body) != pytest.approx(0.0)


def test_closed_source_link_rejects_positive_flow(
    model: pyo.ConcreteModel,
) -> None:
    """Check that flow cannot use an inactive source-to-plant link."""
    model.gamma["S1", "T1"].set_value(0)
    model.b["S1", "T1"].set_value(1.0)

    constraint = model.source_plant_link_capacity["S1", "T1"]

    assert pyo.value(constraint.body) > pyo.value(constraint.upper)


def test_solver_finds_expected_optimum(
    parameters: ModelParameters,
) -> None:
    """Solve the toy model and verify its cost and network flows."""

    pytest.importorskip("highspy")

    model, results = solve(parameters)

    assert results.solver.termination_condition == pyo.TerminationCondition.optimal
    assert pyo.value(model.total_cost) == pytest.approx(60.0)
    assert pyo.value(model.a["S1"]) == pytest.approx(30.0)
    assert pyo.value(model.a["S2"]) == pytest.approx(0.0)
    assert pyo.value(model.b["S1", "T1"]) == pytest.approx(30.0)
    assert pyo.value(model.b["S2", "T1"]) == pytest.approx(0.0)
    assert pyo.value(model.c["T1", "Z1"]) == pytest.approx(30.0)


@pytest.fixture
def two_plant_parameters() -> ModelParameters:
    """Two sources, two plants, one zone, with every arc present.

    Both sources flow to both plants, and both plants flow to the zone.
    """
    return ModelParameters(
        source_ids=("S1", "S2"),
        plant_ids=("T1", "T2"),
        zone_ids=("Z1",),
        quality_parameter_ids=("turbidity",),
        source_plant_arcs=(
            ("S1", "T1"),
            ("S1", "T2"),
            ("S2", "T1"),
            ("S2", "T2"),
        ),
        plant_zone_arcs=(("T1", "Z1"), ("T2", "Z1")),
        demand_by_zone={"Z1": 30.0},
        source_fixed_cost={"S1": 10.0, "S2": 20.0},
        plant_fixed_cost={"T1": 5.0, "T2": 5.0},
        source_unit_cost={"S1": 1.0, "S2": 2.0},
        plant_unit_treatment_cost={"T1": 0.5, "T2": 0.5},
        source_min_withdrawal={"S1": 0.0, "S2": 0.0},
        source_max_withdrawal={"S1": 40.0, "S2": 30.0},
        plant_min_throughput={"T1": 0.0, "T2": 0.0},
        plant_max_throughput={"T1": 60.0, "T2": 60.0},
        source_plant_link_capacity={
            ("S1", "T1"): 40.0,
            ("S1", "T2"): 40.0,
            ("S2", "T1"): 30.0,
            ("S2", "T2"): 30.0,
        },
        plant_zone_link_capacity={("T1", "Z1"): 60.0, ("T2", "Z1"): 60.0},
        source_quality={("S1", "turbidity"): 1.0, ("S2", "turbidity"): 4.0},
        quality_lower_bound={"turbidity": 0.0},
        quality_upper_bound={"turbidity": 5.0},
        quality_units={"turbidity": "NTU"},
        warnings=(),
    )


@pytest.fixture
def two_plant_model(two_plant_parameters: ModelParameters) -> pyo.ConcreteModel:
    """A built, unsolved model over the two-plant network."""
    return build_model(two_plant_parameters)


@pytest.fixture
def separate_plants_parameters(
    two_plant_parameters: ModelParameters,
) -> ModelParameters:
    """The same two plants as in two_plant_parameters, but each is fed by one source and
    each plant feeds one zone. This is to test constraints that should be per plant but
    are written over the whole network.

    S2 is far higher in turbidity than S1 and the only route to Z2 is through T2, so
    T2's blend is S2 alone. A quality constraint written over the whole network would
    accept this, but a per-plant constraint should not.
    """
    return replace(
        two_plant_parameters,
        zone_ids=("Z1", "Z2"),
        source_plant_arcs=(("S1", "T1"), ("S2", "T2")),
        plant_zone_arcs=(("T1", "Z1"), ("T2", "Z2")),
        demand_by_zone={"Z1": 30.0, "Z2": 10.0},
        source_plant_link_capacity={("S1", "T1"): 40.0, ("S2", "T2"): 30.0},
        plant_zone_link_capacity={("T1", "Z1"): 60.0, ("T2", "Z2"): 60.0},
        source_quality={("S1", "turbidity"): 1.0, ("S2", "turbidity"): 9.0},
    )


def test_expensive_plant_is_switched_off(
    two_plant_parameters: ModelParameters,
) -> None:
    """A plant that costs more to operate than the other is not used, and its activation binary is zero.

    Nothing else makes the model choose beta = 0, so without this the
    activation binary could be dropped from the throughput bounds unnoticed.
    """
    parameters = replace(
        two_plant_parameters,
        plant_fixed_cost={"T1": 5.0, "T2": 500.0},
    )
    model, results = solve(parameters)

    assert results.solver.termination_condition == pyo.TerminationCondition.optimal
    assert pyo.value(model.beta["T2"]) == pytest.approx(0.0)
    assert pyo.value(model.c["T2", "Z1"]) == pytest.approx(0.0)
    assert sum(pyo.value(model.b[s, "T2"]) for s in model.S) == pytest.approx(0.0)


def test_unused_source_is_not_forced_to_its_minimum(
    two_plant_parameters: ModelParameters,
) -> None:
    """A source the model does not want to use does not draw anything, even if its
    minimum withdrawal is nonzero.

    Minimum withdrawal is conditional on activation. Without the binary an unused source
    would be forced to draw its minimum.
    """
    parameters = replace(
        two_plant_parameters,
        source_min_withdrawal={"S1": 0.0, "S2": 12.0},
        source_unit_cost={"S1": 1.0, "S2": 9.0},
    )
    model, results = solve(parameters)

    assert results.solver.termination_condition == pyo.TerminationCondition.optimal
    assert pyo.value(model.alpha["S2"]) == pytest.approx(0.0)
    assert pyo.value(model.a["S2"]) == pytest.approx(0.0)


def test_plant_minimum_throughput_forces_oversupply(
    two_plant_parameters: ModelParameters,
) -> None:
    """Water entering a plant must leave it, even when demand is already met.

    Since demand is only a lower bound, the plant minimum throughput may force excess
    water to be delivered. This verifies that plant flow balance is an equality rather
    than an inequality that could absorb surplus flow.
    """
    parameters = replace(
        two_plant_parameters,
        plant_ids=("T1",),
        source_plant_arcs=(("S1", "T1"), ("S2", "T1")),
        plant_zone_arcs=(("T1", "Z1"),),
        plant_fixed_cost={"T1": 5.0},
        plant_unit_treatment_cost={"T1": 0.5},
        plant_min_throughput={"T1": 45.0},
        plant_max_throughput={"T1": 60.0},
        source_plant_link_capacity={("S1", "T1"): 40.0, ("S2", "T1"): 30.0},
        plant_zone_link_capacity={("T1", "Z1"): 60.0},
    )
    model, results = solve(parameters)

    assert results.solver.termination_condition == pyo.TerminationCondition.optimal
    inflow = sum(pyo.value(model.b[s, "T1"]) for s in model.S)
    delivered = pyo.value(model.c["T1", "Z1"])
    assert inflow == pytest.approx(45.0), "the plant minimum is respected"
    assert delivered == pytest.approx(inflow), "nothing is lost inside the plant"
    assert delivered > parameters.demand_by_zone["Z1"], "the surplus is delivered"


def test_closed_plant_zone_link_rejects_positive_flow(
    two_plant_model: pyo.ConcreteModel,
) -> None:
    """A closed plant-to-zone link cannot carry positive flow.

    Complements the corresponding source-to-plant test by checking the plant-to-zone
    capacity constraint.
    """
    two_plant_model.delta["T1", "Z1"].set_value(0)
    two_plant_model.c["T1", "Z1"].set_value(1.0)

    constraint = two_plant_model.plant_zone_link_capacity["T1", "Z1"]

    assert pyo.value(constraint.body) > pyo.value(constraint.upper)


def test_arc_cannot_be_open_when_its_node_is_off(
    two_plant_model: pyo.ConcreteModel,
) -> None:
    """An arc cannot be active when its upstream node is inactive.

    Setting the arc binary to one while its upstream node binary is zero must violate
    the activation constraint on both sides of the network.
    """
    two_plant_model.gamma["S1", "T1"].set_value(1)
    two_plant_model.alpha["S1"].set_value(0)
    source_side = two_plant_model.source_link_activation["S1", "T1"]
    assert pyo.value(source_side.body) > pyo.value(source_side.upper)

    two_plant_model.delta["T1", "Z1"].set_value(1)
    two_plant_model.beta["T1"].set_value(0)
    plant_side = two_plant_model.plant_link_activation["T1", "Z1"]
    assert pyo.value(plant_side.body) > pyo.value(plant_side.upper)


def test_zone_with_demand_and_no_supply_is_infeasible(
    two_plant_parameters: ModelParameters,
) -> None:
    """A zone with positive demand and no incoming supply is infeasible.

    This checks that a zone with no incoming arcs is marked infeasible rather than
    having its demand constraint skipped.
    """
    parameters = replace(
        two_plant_parameters,
        zone_ids=("Z1", "Z2"),
        demand_by_zone={"Z1": 30.0, "Z2": 10.0},
    )
    _, results = solve(parameters)

    assert results.solver.termination_condition == pyo.TerminationCondition.infeasible


def test_upper_quality_limits_apply_per_plant_not_network_wide(
    separate_plants_parameters: ModelParameters,
) -> None:
    """Upper quality limits must be enforced separately at each plant.

    T2 receives only S2 at 9 NTU, exceeding its limit of 5 NTU, even though the network-
    wide flow-weighted average is 3.0 NTU. A quality constraint written over every arc
    rather than per plant would accept this.
    """
    _, results = solve(separate_plants_parameters)

    assert results.solver.termination_condition == pyo.TerminationCondition.infeasible


def test_lower_quality_limits_also_apply_per_plant(
    separate_plants_parameters: ModelParameters,
) -> None:
    """Lower quality limits must also be enforced separately at each plant.

    T2 receives only S2 at 30 mg/L, below its lower limit of 40 mg/L, while the network-
    wide average is 52.5 mg/L. A network-wide lower-bound constraint would therefore
    incorrectly accept this solution.
    """
    parameters = replace(
        separate_plants_parameters,
        quality_parameter_ids=("alkalinity",),
        source_quality={("S1", "alkalinity"): 60.0, ("S2", "alkalinity"): 30.0},
        quality_lower_bound={"alkalinity": 40.0},
        quality_upper_bound={"alkalinity": 200.0},
        quality_units={"alkalinity": "mg/L CaCO3"},
    )
    _, results = solve(parameters)

    assert results.solver.termination_condition == pyo.TerminationCondition.infeasible


def test_flow_is_conserved_per_plant_not_network_wide(
    separate_plants_parameters: ModelParameters,
) -> None:
    """Flow must be conserved independently at each plant.

    Since Z2 can only be supplied through T2, meeting its demand requires flow from S2
    through T2. A network-wide balance alone could incorrectly allow T2 to deliver water
    that entered through T1.
    """
    parameters = replace(
        separate_plants_parameters,
        source_quality={("S1", "turbidity"): 1.0, ("S2", "turbidity"): 1.0},
    )
    model, results = solve(parameters)

    assert results.solver.termination_condition == pyo.TerminationCondition.optimal
    assert pyo.value(model.a["S2"]) == pytest.approx(10.0)
    assert pyo.value(model.b["S2", "T2"]) == pytest.approx(10.0)
    assert pyo.value(model.c["T2", "Z2"]) == pytest.approx(10.0)


PH_PARAMETER = "hydrogen_ion_concentration_mol_l"


def _uniform_ph(
    parameters: ModelParameters,
    source_ph: float,
    lower_ph: float,
    upper_ph: float,
    scale: float,
) -> ModelParameters:
    """Set every source to the same pH and convert the pH limits to concentration.

    Hydrogen ion concentration is 10**(-pH), so higher pH means lower
    concentration. The bounds therefore reverse when converted: the upper pH
    limit becomes the lower concentration bound, and the lower pH limit becomes
    the upper concentration bound.

    scale controls the concentration units: 1.0 for mol/L and 1e9 for nmol/L.
    """
    return replace(
        parameters,
        quality_parameter_ids=(PH_PARAMETER,),
        source_quality={
            (source_id, PH_PARAMETER): 10.0**-source_ph * scale
            for source_id in parameters.source_ids
        },
        quality_lower_bound={PH_PARAMETER: 10.0**-upper_ph * scale},
        quality_upper_bound={PH_PARAMETER: 10.0**-lower_ph * scale},
        quality_units={PH_PARAMETER: "mol/L" if scale == 1.0 else "nmol/L"},
    )


def test_ph_scaling_changes_constraint_enforcement(
    two_plant_parameters: ModelParameters,
) -> None:
    """Poor pH scaling can make an infeasible model appear feasible.

    All sources are at pH 8.6, above the permitted maximum of 8.5. The mol/L formulation
    is accepted, while the same physical problem expressed in nmol/L is correctly
    rejected.
    """
    mol_l_parameters = _uniform_ph(
        two_plant_parameters,
        source_ph=8.6,
        lower_ph=6.5,
        upper_ph=8.5,
        scale=1.0,
    )
    _, mol_l_results = solve(mol_l_parameters)

    nmol_l_parameters = _uniform_ph(
        two_plant_parameters,
        source_ph=8.6,
        lower_ph=6.5,
        upper_ph=8.5,
        scale=1e9,
    )
    _, nmol_l_results = solve(nmol_l_parameters)

    assert (
        mol_l_results.solver.termination_condition == pyo.TerminationCondition.optimal
    )

    assert (
        nmol_l_results.solver.termination_condition
        == pyo.TerminationCondition.infeasible
    )


@pytest.mark.parametrize("scale", [1.0, 1e9])
def test_ph_within_limits_solves_at_either_scale(
    two_plant_parameters: ModelParameters,
    scale: float,
) -> None:
    """A pH within the permitted range remains feasible at either scale.

    This verifies that rescaling the hydrogen ion concentration changes only the
    numerical scale of the quality constraint, not whether a compliant pH is
    accepted.
    """
    parameters = _uniform_ph(
        two_plant_parameters,
        source_ph=7.5,
        lower_ph=6.5,
        upper_ph=8.5,
        scale=scale,
    )
    _, results = solve(parameters)

    assert results.solver.termination_condition == pyo.TerminationCondition.optimal
