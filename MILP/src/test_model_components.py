"""Unit tests for AquaBlend Pyomo sets, variables, and objective."""

import pyomo.environ as pyo
import pytest

import src.model as model_module
from src.model import define_sets, define_variables, set_objective
from src.preprocessing import ModelParameters


@pytest.fixture
def parameters() -> ModelParameters:
    """Return a small complete parameter object for component testing."""
    return ModelParameters(
        source_ids=("source_1", "source_2"),
        plant_ids=("plant_1",),
        zone_ids=("zone_1",),
        quality_parameter_ids=("turbidity",),
        source_plant_arcs=(
            ("source_1", "plant_1"),
            ("source_2", "plant_1"),
        ),
        plant_zone_arcs=(("plant_1", "zone_1"),),
        demand_by_zone={"zone_1": 30.0},
        source_fixed_cost={"source_1": 100.0, "source_2": 200.0},
        plant_fixed_cost={"plant_1": 50.0},
        source_unit_cost={"source_1": 2.0, "source_2": 3.0},
        plant_unit_treatment_cost={"plant_1": 0.5},
        source_min_withdrawal={"source_1": 0.0, "source_2": 0.0},
        source_max_withdrawal={"source_1": 100.0, "source_2": 100.0},
        plant_min_throughput={"plant_1": 0.0},
        plant_max_throughput={"plant_1": 200.0},
        source_plant_link_capacity={
            ("source_1", "plant_1"): 100.0,
            ("source_2", "plant_1"): 100.0,
        },
        plant_zone_link_capacity={("plant_1", "zone_1"): 200.0},
        source_quality={
            ("source_1", "turbidity"): 1.0,
            ("source_2", "turbidity"): 2.0,
        },
        quality_lower_bound={"turbidity": 0.0},
        quality_upper_bound={"turbidity": 5.0},
        quality_units={"turbidity": "NTU"},
        warnings=(),
    )


@pytest.fixture
def model(parameters: ModelParameters) -> pyo.ConcreteModel:
    """Return a model containing only the components owned by this PR."""
    instance = pyo.ConcreteModel(name="aquablend_component_test")
    define_sets(instance, parameters)
    define_variables(instance)
    return instance


def test_sets_match_model_parameters(
    model: pyo.ConcreteModel,
    parameters: ModelParameters,
) -> None:
    assert tuple(model.S) == parameters.source_ids
    assert tuple(model.T) == parameters.plant_ids
    assert tuple(model.Z) == parameters.zone_ids
    assert tuple(model.P) == parameters.quality_parameter_ids
    assert tuple(model.ST) == parameters.source_plant_arcs
    assert tuple(model.TZ) == parameters.plant_zone_arcs


def test_shared_variables_have_exact_indexes(
    model: pyo.ConcreteModel,
    parameters: ModelParameters,
) -> None:
    assert set(model.alpha) == set(parameters.source_ids)
    assert set(model.a) == set(parameters.source_ids)
    assert set(model.beta) == set(parameters.plant_ids)
    assert set(model.gamma) == set(parameters.source_plant_arcs)
    assert set(model.b) == set(parameters.source_plant_arcs)
    assert set(model.delta) == set(parameters.plant_zone_arcs)
    assert set(model.c) == set(parameters.plant_zone_arcs)


def test_flow_variables_are_non_negative_and_continuous(
    model: pyo.ConcreteModel,
) -> None:
    for variable in model.a.values():
        assert variable.is_continuous()
        assert variable.lb == 0
        assert variable.ub is None

    for variable in model.b.values():
        assert variable.is_continuous()
        assert variable.lb == 0
        assert variable.ub is None

    for variable in model.c.values():
        assert variable.is_continuous()
        assert variable.lb == 0
        assert variable.ub is None


def test_activation_variables_are_binary(
    model: pyo.ConcreteModel,
) -> None:
    assert all(variable.is_binary() for variable in model.alpha.values())
    assert all(variable.is_binary() for variable in model.beta.values())
    assert all(variable.is_binary() for variable in model.gamma.values())
    assert all(variable.is_binary() for variable in model.delta.values())


def test_objective_is_minimisation(
    model: pyo.ConcreteModel,
    parameters: ModelParameters,
) -> None:
    set_objective(model, parameters)
    assert model.total_cost.sense == pyo.minimize
    assert model.total_cost.expr.polynomial_degree() == 1


def test_objective_uses_all_four_cost_components(
    model: pyo.ConcreteModel,
    parameters: ModelParameters,
) -> None:
    set_objective(model, parameters)

    model.a["source_1"].set_value(10.0)
    model.a["source_2"].set_value(20.0)
    model.alpha["source_1"].set_value(1)
    model.alpha["source_2"].set_value(0)
    model.beta["plant_1"].set_value(1)
    model.b["source_1", "plant_1"].set_value(10.0)
    model.b["source_2", "plant_1"].set_value(20.0)

    # Withdrawal: 2*10 + 3*20 = 80
    # Activation: 100*1 + 200*0 = 100
    # Plant activation: 50*1 = 50
    # Treatment: 0.5*(10 + 20) = 15
    assert pyo.value(model.total_cost.expr) == pytest.approx(245.0)


def test_objective_supports_one_source_connected_to_multiple_plants() -> None:
    parameters = ModelParameters(
        source_ids=("source_1",),
        plant_ids=("plant_1", "plant_2"),
        zone_ids=("zone_1",),
        quality_parameter_ids=("turbidity",),
        source_plant_arcs=(
            ("source_1", "plant_1"),
            ("source_1", "plant_2"),
        ),
        plant_zone_arcs=(
            ("plant_1", "zone_1"),
            ("plant_2", "zone_1"),
        ),
        demand_by_zone={"zone_1": 15.0},
        source_fixed_cost={"source_1": 7.0},
        plant_fixed_cost={"plant_1": 0.0, "plant_2": 0.0},
        source_unit_cost={"source_1": 2.0},
        plant_unit_treatment_cost={"plant_1": 1.0, "plant_2": 3.0},
        source_min_withdrawal={"source_1": 0.0},
        source_max_withdrawal={"source_1": 20.0},
        plant_min_throughput={"plant_1": 0.0, "plant_2": 0.0},
        plant_max_throughput={"plant_1": 10.0, "plant_2": 10.0},
        source_plant_link_capacity={
            ("source_1", "plant_1"): 10.0,
            ("source_1", "plant_2"): 10.0,
        },
        plant_zone_link_capacity={
            ("plant_1", "zone_1"): 10.0,
            ("plant_2", "zone_1"): 10.0,
        },
        source_quality={("source_1", "turbidity"): 1.0},
        quality_lower_bound={"turbidity": 0.0},
        quality_upper_bound={"turbidity": 5.0},
        quality_units={"turbidity": "NTU"},
        warnings=(),
    )

    model = pyo.ConcreteModel(name="multiple_plant_objective_test")
    define_sets(model, parameters)
    define_variables(model)
    set_objective(model, parameters)

    model.a["source_1"].set_value(15.0)
    model.alpha["source_1"].set_value(1)
    model.beta["plant_1"].set_value(1)
    model.beta["plant_2"].set_value(1)
    model.b["source_1", "plant_1"].set_value(10.0)
    model.b["source_1", "plant_2"].set_value(5.0)

    # Withdrawal: 2*(10 + 5) = 30
    # Activation: 7*1 = 7
    # Treatment: 1*10 + 3*5 = 25
    assert pyo.value(model.total_cost.expr) == pytest.approx(62.0)


def test_build_model_assembles_owned_components(
    monkeypatch: pytest.MonkeyPatch,
    parameters: ModelParameters,
) -> None:
    """Exercise assembly while the separately owned constraints remain a stub."""
    monkeypatch.setattr(
        model_module,
        "set_constraints",
        lambda model, model_parameters: None,
    )

    model = model_module.build_model(parameters)

    assert tuple(model.ST) == parameters.source_plant_arcs
    assert tuple(model.TZ) == parameters.plant_zone_arcs
    for variable_name in ("alpha", "a", "beta", "gamma", "b", "delta", "c"):
        assert hasattr(model, variable_name)
    assert hasattr(model, "total_cost")


def test_build_model_rejects_missing_parameters() -> None:
    with pytest.raises(TypeError, match="ModelParameters cannot be None"):
        model_module.build_model(None)  # type: ignore[arg-type]