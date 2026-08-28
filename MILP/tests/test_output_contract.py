"""Test that json sample contracts match the classes in the output contract"""

import src.contracts as contracts

import dataclasses
import json
import typing
from pathlib import Path

# Add all relevant contract paths here
CONTRACT_PATHS = sorted(
    (Path(__file__).parent.parent / "json_contracts").glob("output_*.json")
)

def check(json_obj, dc_type, path="root"):
    """
    Recursive check to ensure that the json object matches the data contract.
    It accounts for tuples and object types in the data contract.
    """
    hints = typing.get_type_hints(dc_type)
    expected_keys = set(hints.keys())
    actual_keys = set(json_obj.keys())
    assert actual_keys == expected_keys, (
        f"Key mismatch at '{path}' ({dc_type.__name__}):\n"
        f"  in JSON but not class: {sorted(actual_keys - expected_keys)}\n"
        f"  in class but not JSON: {sorted(expected_keys - actual_keys)}"
    )

    for name, field_type in hints.items():
        value = json_obj[name]
        origin = typing.get_origin(field_type)

        if origin in (tuple, list):
            (item_type,) = typing.get_args(field_type)[:1] or (None,)
            if dataclasses.is_dataclass(item_type) and value:
                for i, item in enumerate(value):
                    check(item, item_type, path=f"{path}.{name}[{i}]")


        elif dataclasses.is_dataclass(field_type) and isinstance(value, dict):
            check(value, field_type, path=f"{path}.{name}")


def test_all_keys_present():
    """Every key in output_contract_v1.json must match a field on the
    corresponding solved_data.py dataclass, at every nested level."""

    for contract_path in CONTRACT_PATHS:
        with contract_path.open() as f:
            contract = json.load(f)

        check(contract, contracts.SolvedScenario)