from __future__ import annotations

import argparse
import json
import math
import os
import re
from pathlib import Path
from typing import Any

try:
    # Package import used when the module is imported through src.
    from .contracts import (
        DemandZoneInput,
        PlantInput,
        PlantZoneLinkInput,
        ScenarioData,
        SourceInput,
        SourcePlantLinkInput,
    )
except ImportError:  # pragma: no cover - supports direct script execution.
    from contracts import (
        DemandZoneInput,
        PlantInput,
        PlantZoneLinkInput,
        ScenarioData,
        SourceInput,
        SourcePlantLinkInput,
    )


class DataLoadError(RuntimeError):
    """Raised when scenario or database data cannot be loaded safely."""


_SUPPORTED_QUALITY_TRANSFORMS = {
    "identity",
    "ph_to_hydrogen_ion",
}

# Backwards-compatible database-column names for the three parameters currently
# supplied by the source view. New parameters should declare ``source_field`` in
# their quality-limit definition, so the loader itself does not need changing.
_DEFAULT_QUALITY_SOURCE_FIELDS = {
    "pH": "representative_ph",
    "alkalinity": "representative_alkalinity_mg_l_caco3",
    "turbidity": "representative_turbidity_ntu",
}

_DEFAULT_QUALITY_ESTIMATED_FIELDS = {
    "alkalinity": "alkalinity_is_estimated",
}

_DEFAULT_QUALITY_PROVENANCE_FIELDS = {
    "alkalinity": "alkalinity_provenance",
}

_RELATION_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")

_MILP_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_SCENARIO_PATH = _MILP_ROOT / "config" / "scenarios" / "base_scenarios_v1.json"


def _read_json(path: str | Path) -> dict[str, Any]:
    scenario_path = Path(path)

    if not scenario_path.exists():
        raise DataLoadError(f"Scenario file not found: {scenario_path}")
    if not scenario_path.is_file():
        raise DataLoadError(f"Scenario path is not a file: {scenario_path}")

    try:
        data = json.loads(scenario_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DataLoadError(
            f"Could not read scenario file {scenario_path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise DataLoadError(
            f"Invalid JSON in {scenario_path}: line {exc.lineno}, column {exc.colno}"
        ) from exc

    if not isinstance(data, dict):
        raise DataLoadError("The scenario file must contain one JSON object.")

    return data


def _database_url() -> str:
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise DataLoadError(
            'Missing dependency "python-dotenv". '
            "Install it with: pip install python-dotenv"
        ) from exc

    load_dotenv()
    value = os.getenv("DATABASE_URL")

    if not value:
        raise DataLoadError("DATABASE_URL is missing. Add it to your local .env file.")

    return value


def _quoted_relation(name: str) -> str:
    """Return a safely quoted schema/view name supplied by configuration."""
    if not _RELATION_PATTERN.fullmatch(name):
        raise DataLoadError(f"Unsafe database view name: {name}")

    return ".".join(f'"{part}"' for part in name.split("."))


def _fetch_sources(
    database_url: str,
    view_name: str,
    source_ids: list[str],
) -> list[dict[str, Any]]:
    """Fetch complete source-view rows for the requested active source IDs.

    ``SELECT *`` is deliberate here: quality parameter definitions identify the
    source-view column through ``quality_limits.parameters.<id>.source_field``.
    This keeps adding a quality parameter from requiring another loader edit.
    The relation name is strictly validated and quoted before interpolation;
    source IDs remain parameterised.
    """
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise DataLoadError(
            'Missing dependency "psycopg". '
            'Install it with: pip install "psycopg[binary]"'
        ) from exc

    relation = _quoted_relation(view_name)
    query = f"""
        SELECT *
        FROM {relation}
        WHERE source_id = ANY(%s)
          AND is_active = TRUE
        ORDER BY source_id;
    """

    try:
        with (
            psycopg.connect(
                database_url,
                row_factory=dict_row,
                connect_timeout=10,
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(query, (source_ids,))
            return list(cursor.fetchall())
    except Exception as exc:
        raise DataLoadError(
            "Could not fetch source data from Supabase. "
            f"Original error: {type(exc).__name__}: {exc}"
        ) from exc


def _require_mapping(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DataLoadError(f'"{field_name}" must be a JSON object.')
    return value


def _require_list(value: Any, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise DataLoadError(f'"{field_name}" must be a JSON array.')
    return value


def _require_object_list(value: Any, field_name: str) -> list[dict[str, Any]]:
    items = _require_list(value, field_name)
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise DataLoadError(f'"{field_name}[{index}]" must be a JSON object.')
    return items


def _required_text(value: Any, field_name: str) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        raise DataLoadError(f'"{field_name}" is required and must not be blank.')
    return text


def _optional_bool(value: Any, field_name: str, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise DataLoadError(f'"{field_name}" must be true or false.')


def _row_bool(
    row: dict[str, Any],
    field_name: str,
    source_id: str,
    *,
    default: bool = False,
) -> bool:
    """Read a source-row boolean without truthiness coercion."""
    return _optional_bool(
        row.get(field_name),
        f"source row {source_id}.{field_name}",
        default=default,
    )


def _to_float(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise DataLoadError(f'"{field_name}" must be numeric, not boolean.')

    try:
        converted = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DataLoadError(f'"{field_name}" must be a valid number.') from exc

    if not math.isfinite(converted):
        raise DataLoadError(f'"{field_name}" must be finite.')
    return converted


def _append_non_negative_issue(
    issues: list[str],
    value: float | None,
    label: str,
    *,
    required: bool,
) -> None:
    if value is None:
        if required:
            issues.append(f"{label} is missing.")
        return
    if value < 0:
        issues.append(f"{label} must be greater than or equal to zero.")


def _normalise_quality_limits(value: Any) -> dict[str, Any]:
    """Validate and copy the quality-limit configuration used by both stages."""
    quality_limits = dict(_require_mapping(value, "quality_limits"))
    parameters = _require_mapping(
        quality_limits.get("parameters"),
        "quality_limits.parameters",
    )
    if not parameters:
        raise DataLoadError('"quality_limits.parameters" must not be empty.')

    normalised_parameters: dict[str, dict[str, Any]] = {}
    model_names: set[str] = set()

    for raw_parameter_id, raw_specification in parameters.items():
        parameter_id = _required_text(
            raw_parameter_id,
            "quality_limits.parameters key",
        )
        specification = dict(
            _require_mapping(
                raw_specification,
                f"quality_limits.parameters.{parameter_id}",
            )
        )

        minimum = _to_float(
            specification.get("min"),
            f"quality_limits.parameters.{parameter_id}.min",
        )
        maximum = _to_float(
            specification.get("max"),
            f"quality_limits.parameters.{parameter_id}.max",
        )
        if minimum is None or maximum is None:
            raise DataLoadError(
                f'Quality parameter "{parameter_id}" requires both min and max.'
            )
        if minimum > maximum:
            raise DataLoadError(
                f'Quality parameter "{parameter_id}" has min greater than max.'
            )

        unit = _required_text(
            specification.get("unit"),
            f"quality_limits.parameters.{parameter_id}.unit",
        )
        transform = _required_text(
            specification.get("transform", "identity"),
            f"quality_limits.parameters.{parameter_id}.transform",
        )
        if transform not in _SUPPORTED_QUALITY_TRANSFORMS:
            supported = ", ".join(sorted(_SUPPORTED_QUALITY_TRANSFORMS))
            raise DataLoadError(
                f'Unsupported quality transform "{transform}" for '
                f'"{parameter_id}". Supported transforms: {supported}.'
            )

        source_field = specification.get("source_field")
        if source_field is None:
            source_field = _DEFAULT_QUALITY_SOURCE_FIELDS.get(parameter_id)
        source_field = _required_text(
            source_field,
            f"quality_limits.parameters.{parameter_id}.source_field",
        )

        default_model_name = (
            "hydrogen_ion_concentration_mol_l"
            if transform == "ph_to_hydrogen_ion"
            else parameter_id
        )
        model_name = _required_text(
            specification.get("model_name", default_model_name),
            f"quality_limits.parameters.{parameter_id}.model_name",
        )
        if model_name in model_names:
            raise DataLoadError(
                f'Quality model identifier "{model_name}" is used more than once.'
            )
        model_names.add(model_name)

        default_model_unit = "mol/L" if transform == "ph_to_hydrogen_ion" else unit
        model_unit = _required_text(
            specification.get("model_unit", default_model_unit),
            f"quality_limits.parameters.{parameter_id}.model_unit",
        )

        specification.update(
            {
                "min": minimum,
                "max": maximum,
                "unit": unit,
                "transform": transform,
                "source_field": source_field,
                "model_name": model_name,
                "model_unit": model_unit,
            }
        )
        normalised_parameters[parameter_id] = specification

    quality_limits["parameters"] = normalised_parameters
    return quality_limits


def _inline_source_rows(data_source: dict[str, Any]) -> list[dict[str, Any]]:
    """Return row-shaped source records embedded in the scenario JSON.

    ``source_rows`` is the canonical property. ``rows`` remains accepted as a
    compact alias so early toy scenarios can be migrated without code changes.
    """
    raw_rows = data_source.get("source_rows")
    field_name = "data_source.source_rows"

    if raw_rows is None and "rows" in data_source:
        raw_rows = data_source.get("rows")
        field_name = "data_source.rows"

    if raw_rows is None:
        raise DataLoadError(
            'data_source.type = "inline" requires "data_source.source_rows".'
        )

    rows = _require_object_list(raw_rows, field_name)
    active_rows: list[dict[str, Any]] = []

    for index, row in enumerate(rows):
        is_active = _optional_bool(
            row.get("is_active"),
            f"{field_name}[{index}].is_active",
            default=True,
        )
        if is_active:
            active_rows.append(row)

    return active_rows


def _database_rows_by_id(
    database_rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    rows_by_id: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(database_rows):
        if not isinstance(row, dict):
            raise DataLoadError(f"Database source row {index} is not an object.")
        source_id = _required_text(
            row.get("source_id"),
            f"database source row {index}.source_id",
        )
        if source_id in rows_by_id:
            raise DataLoadError(
                f"Database view returned duplicate rows for source {source_id}."
            )
        rows_by_id[source_id] = row
    return rows_by_id


def _build_sources(
    config_sources: list[dict[str, Any]],
    database_rows: list[dict[str, Any]],
    allow_estimated_values: bool,
    validation: dict[str, Any],
    quality_limits: dict[str, Any],
    source_data_origin: str,
) -> tuple[tuple[SourceInput, ...], list[str]]:
    issues: list[str] = []
    enabled_configs: list[dict[str, Any]] = []

    for index, item in enumerate(config_sources):
        enabled = _optional_bool(
            item.get("enabled"),
            f"sources[{index}].enabled",
            default=True,
        )
        if enabled:
            enabled_configs.append(item)

    source_ids = [
        _required_text(item.get("source_id"), f"sources[{index}].source_id")
        for index, item in enumerate(enabled_configs)
    ]
    if len(source_ids) != len(set(source_ids)):
        raise DataLoadError("The scenario contains duplicate enabled source IDs.")
    if not source_ids:
        issues.append("The scenario must contain at least one enabled source.")

    rows_by_id = _database_rows_by_id(database_rows)
    quality_parameters = _require_mapping(
        quality_limits["parameters"],
        "quality_limits.parameters",
    )
    required_quality_keys = set(quality_parameters)
    loaded_sources: list[SourceInput] = []

    paired_sources = zip(enabled_configs, source_ids, strict=True)
    for index, (item, source_id) in enumerate(paired_sources):
        row = rows_by_id.get(source_id)
        if row is None:
            if validation.get("fail_if_source_missing_from_database", True):
                issues.append(
                    f"Source {source_id} was not found in the configured source data."
                )
            continue

        source_name = _required_text(
            row.get("source_name"),
            f"database source {source_id}.source_name",
        )
        source_type = _required_text(
            row.get("source_type"),
            f"database source {source_id}.source_type",
        )

        minimum_override = item.get("minimum_withdrawal_ml_per_day_override")
        if minimum_override is None and "minimum_withdrawal_ml_per_day" in item:
            minimum_override = item.get("minimum_withdrawal_ml_per_day")

        maximum_override = item.get("maximum_withdrawal_ml_per_day_override")
        if maximum_override is None and "maximum_withdrawal_ml_per_day" in item:
            maximum_override = item.get("maximum_withdrawal_ml_per_day")
        if maximum_override is None:
            maximum_override = item.get("max_available_ml_per_day_override")

        database_minimum = _to_float(
            row.get("minimum_withdrawal_ml_per_day"),
            f"database source {source_id}.minimum_withdrawal_ml_per_day",
        )
        database_maximum = _to_float(
            row.get("max_available_ml_per_day"),
            f"database source {source_id}.max_available_ml_per_day",
        )

        if minimum_override is not None:
            minimum_withdrawal = _to_float(
                minimum_override,
                f"sources[{index}].minimum_withdrawal_ml_per_day_override",
            )
            minimum_origin = "scenario_override"
        else:
            minimum_withdrawal = database_minimum
            minimum_origin = source_data_origin

        if maximum_override is not None:
            maximum_withdrawal = _to_float(
                maximum_override,
                f"sources[{index}].maximum_withdrawal_ml_per_day_override",
            )
            maximum_origin = "scenario_override"
        else:
            maximum_withdrawal = database_maximum
            maximum_origin = source_data_origin

        withdrawal_bounds_origin = (
            minimum_origin if minimum_origin == maximum_origin else "mixed"
        )
        forced_inactive = _optional_bool(
            item.get("forced_inactive"),
            f"sources[{index}].forced_inactive",
            default=False,
        )

        if not forced_inactive:
            require_availability = bool(
                validation.get("fail_if_daily_availability_missing", True)
            )
            _append_non_negative_issue(
                issues,
                minimum_withdrawal,
                f"{source_name} minimum withdrawal in ML/day",
                required=require_availability,
            )
            _append_non_negative_issue(
                issues,
                maximum_withdrawal,
                f"{source_name} maximum withdrawal in ML/day",
                required=require_availability,
            )

        if (
            minimum_withdrawal is not None
            and maximum_withdrawal is not None
            and minimum_withdrawal > maximum_withdrawal
        ):
            issues.append(
                f"{source_name} has a minimum withdrawal greater than its maximum."
            )

        if "fixed_activation_cost" in item:
            fixed_activation_cost = _to_float(
                item.get("fixed_activation_cost"),
                f"sources[{index}].fixed_activation_cost",
            )
            if fixed_activation_cost is None:
                issues.append(f"{source_name} fixed activation cost is missing.")
                continue
        else:
            fixed_activation_cost = 0.0
        _append_non_negative_issue(
            issues,
            fixed_activation_cost,
            f"{source_name} fixed activation cost",
            required=True,
        )

        cost_per_ml = _to_float(
            row.get("cost_per_ml"),
            f"database source {source_id}.cost_per_ml",
        )
        if not forced_inactive:
            _append_non_negative_issue(
                issues,
                cost_per_ml,
                f"{source_name} cost per ML",
                required=True,
            )

        source_quality: dict[str, float] = {}
        quality_estimated_flags: list[bool] = []
        quality_provenance: dict[str, str | None] = {}

        for parameter_id, raw_specification in quality_parameters.items():
            specification = _require_mapping(
                raw_specification,
                f"quality_limits.parameters.{parameter_id}",
            )
            source_field = str(specification["source_field"])
            quality_value = _to_float(
                row.get(source_field),
                f"database source {source_id}.{source_field}",
            )
            if quality_value is None:
                if not forced_inactive and validation.get(
                    "fail_if_required_quality_value_missing", True
                ):
                    issues.append(
                        f"{source_name} is missing quality parameter "
                        f"'{parameter_id}' from database field '{source_field}'."
                    )
            else:
                source_quality[parameter_id] = quality_value

            estimated_field = specification.get("estimated_field")
            if estimated_field is None:
                estimated_field = _DEFAULT_QUALITY_ESTIMATED_FIELDS.get(parameter_id)
            if estimated_field:
                quality_estimated_flags.append(
                    _row_bool(row, str(estimated_field), source_id)
                )

            provenance_field = specification.get("provenance_field")
            if provenance_field is None:
                provenance_field = _DEFAULT_QUALITY_PROVENANCE_FIELDS.get(parameter_id)
            quality_provenance[parameter_id] = (
                None
                if not provenance_field or row.get(str(provenance_field)) is None
                else str(row.get(str(provenance_field)))
            )

        if not forced_inactive and set(source_quality) != required_quality_keys:
            missing_keys = sorted(required_quality_keys - set(source_quality))
            extra_keys = sorted(set(source_quality) - required_quality_keys)
            details: list[str] = []
            if missing_keys:
                details.append("missing=" + ", ".join(missing_keys))
            if extra_keys:
                details.append("unexpected=" + ", ".join(extra_keys))
            issues.append(
                f"{source_name} quality keys do not match "
                "quality_limits.parameters (" + "; ".join(details) + ")."
            )

        estimated_flags = [
            _row_bool(row, "cost_is_estimated", source_id),
            _row_bool(row, "minimum_withdrawal_is_estimated", source_id),
            _row_bool(row, "max_available_is_estimated", source_id),
            minimum_origin == "scenario_override",
            maximum_origin == "scenario_override",
            *quality_estimated_flags,
        ]
        has_estimated_values = any(estimated_flags)
        if has_estimated_values and not allow_estimated_values and not forced_inactive:
            issues.append(f"{source_name} contains estimated or overridden values.")

        provenance: dict[str, str | None] = {
            "storage_capacity": (
                None
                if row.get("storage_capacity_provenance") is None
                else str(row.get("storage_capacity_provenance"))
            ),
            "reference_flow": (
                None
                if row.get("reference_flow_provenance") is None
                else str(row.get("reference_flow_provenance"))
            ),
            "minimum_withdrawal": (
                None
                if row.get("minimum_withdrawal_provenance") is None
                else str(row.get("minimum_withdrawal_provenance"))
            ),
            "maximum_withdrawal": (
                None
                if row.get("max_available_provenance") is None
                else str(row.get("max_available_provenance"))
            ),
            "cost": (
                None
                if row.get("cost_provenance") is None
                else str(row.get("cost_provenance"))
            ),
        }
        provenance.update(
            {f"quality.{key}": value for key, value in quality_provenance.items()}
        )

        model_ready = _row_bool(row, "model_ready", source_id)

        loaded_sources.append(
            SourceInput(
                source_id=source_id,
                name=source_name,
                source_type=source_type,
                enabled=True,
                forced_inactive=forced_inactive,
                minimum_withdrawal_ml_per_day=minimum_withdrawal,
                maximum_withdrawal_ml_per_day=maximum_withdrawal,
                withdrawal_bounds_origin=withdrawal_bounds_origin,
                fixed_activation_cost=fixed_activation_cost,
                cost_per_ml=cost_per_ml,
                quality=source_quality,
                has_estimated_values=has_estimated_values,
                database_model_ready=model_ready,
                availability_status=str(row.get("availability_status", "unknown")),
                provenance=provenance,
            )
        )

    return tuple(loaded_sources), issues


def _build_network(
    network: dict[str, Any],
    validation: dict[str, Any],
) -> tuple[
    tuple[PlantInput, ...],
    tuple[DemandZoneInput, ...],
    tuple[SourcePlantLinkInput, ...],
    tuple[PlantZoneLinkInput, ...],
    list[str],
]:
    """Build and validate enabled plants, zones and network links."""
    issues: list[str] = []

    plant_items = _require_object_list(network.get("plants", []), "network.plants")
    plants: list[PlantInput] = []
    plant_ids_seen: set[str] = set()

    for index, item in enumerate(plant_items):
        if not _optional_bool(
            item.get("enabled"),
            f"network.plants[{index}].enabled",
            default=True,
        ):
            continue

        plant_id = _required_text(
            item.get("plant_id"),
            f"network.plants[{index}].plant_id",
        )
        if plant_id in plant_ids_seen:
            issues.append(f"Duplicate enabled plant ID '{plant_id}'.")
            continue
        plant_ids_seen.add(plant_id)

        name = _required_text(
            item.get("name"),
            f"network.plants[{index}].name",
        )
        minimum_value = item.get("minimum_processing_capacity_ml_per_day")
        if minimum_value is None and "minimum_operating_flow_ml_per_day" in item:
            minimum_value = item.get("minimum_operating_flow_ml_per_day")

        minimum_capacity = _to_float(
            minimum_value,
            f"network.plants[{index}].minimum_processing_capacity_ml_per_day",
        )
        maximum_capacity = _to_float(
            item.get("maximum_processing_capacity_ml_per_day"),
            f"network.plants[{index}].maximum_processing_capacity_ml_per_day",
        )
        fixed_cost = _to_float(
            item.get("fixed_activation_cost"),
            f"network.plants[{index}].fixed_activation_cost",
        )
        treatment_cost = _to_float(
            item.get("treatment_cost_per_ml"),
            f"network.plants[{index}].treatment_cost_per_ml",
        )

        _append_non_negative_issue(
            issues,
            minimum_capacity,
            f"Plant '{plant_id}' minimum processing capacity",
            required=True,
        )
        _append_non_negative_issue(
            issues,
            maximum_capacity,
            f"Plant '{plant_id}' maximum processing capacity",
            required=True,
        )
        _append_non_negative_issue(
            issues,
            fixed_cost,
            f"Plant '{plant_id}' fixed activation cost",
            required=True,
        )
        _append_non_negative_issue(
            issues,
            treatment_cost,
            f"Plant '{plant_id}' treatment cost per ML",
            required=True,
        )

        if (
            minimum_capacity is not None
            and maximum_capacity is not None
            and minimum_capacity > maximum_capacity
        ):
            issues.append(
                f"Plant '{plant_id}' minimum processing capacity exceeds its maximum."
            )

        # A PlantInput cannot truthfully represent missing required scalar values.
        # Keep the validation issue and omit the invalid plant instead of replacing
        # missing values with 0.0.
        if minimum_capacity is None or fixed_cost is None or treatment_cost is None:
            continue

        plants.append(
            PlantInput(
                plant_id=plant_id,
                name=name,
                enabled=True,
                minimum_processing_capacity_ml_per_day=minimum_capacity,
                maximum_processing_capacity_ml_per_day=maximum_capacity,
                fixed_activation_cost=fixed_cost,
                treatment_cost_per_ml=treatment_cost,
            )
        )

    zone_items = _require_object_list(
        network.get("demand_zones", []),
        "network.demand_zones",
    )
    demand_zones: list[DemandZoneInput] = []
    zone_ids_seen: set[str] = set()

    for index, item in enumerate(zone_items):
        zone_id = _required_text(
            item.get("zone_id"),
            f"network.demand_zones[{index}].zone_id",
        )
        if zone_id in zone_ids_seen:
            issues.append(f"Duplicate demand-zone ID '{zone_id}'.")
            continue
        zone_ids_seen.add(zone_id)

        name = _required_text(
            item.get("name"),
            f"network.demand_zones[{index}].name",
        )
        demand = _to_float(
            item.get("demand_ml_per_day"),
            f"network.demand_zones[{index}].demand_ml_per_day",
        )
        _append_non_negative_issue(
            issues,
            demand,
            f"Demand zone '{zone_id}' demand in ML/day",
            required=bool(validation.get("fail_if_demand_missing", True)),
        )

        if "demand_must_be_met" in item:
            demand_must_be_met = _optional_bool(
                item.get("demand_must_be_met"),
                f"network.demand_zones[{index}].demand_must_be_met",
                default=True,
            )
            if not demand_must_be_met:
                issues.append(
                    f"Demand zone '{zone_id}' allows unmet demand, but the current "
                    "formulation requires all demand to be satisfied."
                )

        demand_zones.append(
            DemandZoneInput(
                zone_id=zone_id,
                name=name,
                demand_ml_per_day=demand,
            )
        )

    source_link_items = _require_object_list(
        network.get("source_to_plant_links", []),
        "network.source_to_plant_links",
    )
    source_links: list[SourcePlantLinkInput] = []
    source_link_keys: set[tuple[str, str]] = set()

    for index, item in enumerate(source_link_items):
        if not _optional_bool(
            item.get("enabled"),
            f"network.source_to_plant_links[{index}].enabled",
            default=True,
        ):
            continue

        source_id = _required_text(
            item.get("source_id"),
            f"network.source_to_plant_links[{index}].source_id",
        )
        plant_id = _required_text(
            item.get("plant_id"),
            f"network.source_to_plant_links[{index}].plant_id",
        )
        key = (source_id, plant_id)
        if key in source_link_keys:
            issues.append(f"Duplicate enabled source-to-plant link {key!r}.")
            continue
        source_link_keys.add(key)

        capacity = _to_float(
            item.get("maximum_flow_ml_per_day"),
            f"network.source_to_plant_links[{index}].maximum_flow_ml_per_day",
        )
        _append_non_negative_issue(
            issues,
            capacity,
            f"Source-to-plant link {source_id} -> {plant_id} maximum flow",
            required=True,
        )
        source_links.append(
            SourcePlantLinkInput(
                source_id=source_id,
                plant_id=plant_id,
                enabled=True,
                maximum_flow_ml_per_day=capacity,
            )
        )

    zone_link_items = _require_object_list(
        network.get("plant_to_zone_links", []),
        "network.plant_to_zone_links",
    )
    zone_links: list[PlantZoneLinkInput] = []
    zone_link_keys: set[tuple[str, str]] = set()

    for index, item in enumerate(zone_link_items):
        if not _optional_bool(
            item.get("enabled"),
            f"network.plant_to_zone_links[{index}].enabled",
            default=True,
        ):
            continue

        plant_id = _required_text(
            item.get("plant_id"),
            f"network.plant_to_zone_links[{index}].plant_id",
        )
        zone_id = _required_text(
            item.get("zone_id"),
            f"network.plant_to_zone_links[{index}].zone_id",
        )
        key = (plant_id, zone_id)
        if key in zone_link_keys:
            issues.append(f"Duplicate enabled plant-to-zone link {key!r}.")
            continue
        zone_link_keys.add(key)

        capacity = _to_float(
            item.get("maximum_flow_ml_per_day"),
            f"network.plant_to_zone_links[{index}].maximum_flow_ml_per_day",
        )
        _append_non_negative_issue(
            issues,
            capacity,
            f"Plant-to-zone link {plant_id} -> {zone_id} maximum flow",
            required=True,
        )
        zone_links.append(
            PlantZoneLinkInput(
                plant_id=plant_id,
                zone_id=zone_id,
                enabled=True,
                maximum_flow_ml_per_day=capacity,
            )
        )

    plant_ids = {plant.plant_id for plant in plants}
    zone_ids = {zone.zone_id for zone in demand_zones}

    if not plants:
        issues.append("The scenario must contain at least one valid enabled plant.")
    if not demand_zones:
        issues.append("The scenario must contain at least one demand zone.")
    if not source_links:
        issues.append(
            "The scenario must contain at least one enabled source-to-plant link."
        )
    if not zone_links:
        issues.append(
            "The scenario must contain at least one enabled plant-to-zone link."
        )

    for link in source_links:
        if link.plant_id not in plant_ids:
            issues.append(
                f"Source-to-plant link references unknown or invalid plant "
                f"'{link.plant_id}'."
            )

    for link in zone_links:
        if link.plant_id not in plant_ids:
            issues.append(
                f"Plant-to-zone link references unknown or invalid plant "
                f"'{link.plant_id}'."
            )
        if link.zone_id not in zone_ids:
            issues.append(
                f"Plant-to-zone link references unknown zone '{link.zone_id}'."
            )

    return (
        tuple(plants),
        tuple(demand_zones),
        tuple(source_links),
        tuple(zone_links),
        issues,
    )


def load_scenario(
    scenario_path: str | Path,
    *,
    strict: bool = True,
) -> ScenarioData:
    """Load one scenario from Supabase or embedded source rows."""
    config = _read_json(scenario_path)

    scenario_id = _required_text(config.get("scenario_id"), "scenario_id")
    scenario_name = _required_text(config.get("scenario_name"), "scenario_name")
    status = _required_text(config.get("status", "draft"), "status")

    data_source = _require_mapping(config.get("data_source"), "data_source")
    validation = _require_mapping(config.get("validation", {}), "validation")

    data_source_type = _required_text(
        data_source.get("type"),
        "data_source.type",
    ).lower()
    allow_estimated_values = _optional_bool(
        data_source.get("allow_estimated_values"),
        "data_source.allow_estimated_values",
        default=False,
    )

    quality_limits = _normalise_quality_limits(config.get("quality_limits"))
    source_config = _require_object_list(config.get("sources"), "sources")

    enabled_source_ids: list[str] = []
    for index, item in enumerate(source_config):
        if _optional_bool(
            item.get("enabled"),
            f"sources[{index}].enabled",
            default=True,
        ):
            enabled_source_ids.append(
                _required_text(item.get("source_id"), f"sources[{index}].source_id")
            )

    if data_source_type == "supabase":
        view_name = _required_text(data_source.get("view"), "data_source.view")
        rows = (
            _fetch_sources(_database_url(), view_name, enabled_source_ids)
            if enabled_source_ids
            else []
        )
    elif data_source_type == "inline":
        rows = _inline_source_rows(data_source)
    else:
        raise DataLoadError('"data_source.type" must be either "supabase" or "inline".')

    sources, source_issues = _build_sources(
        source_config,
        rows,
        allow_estimated_values,
        validation,
        quality_limits,
        source_data_origin=("database" if data_source_type == "supabase" else "inline"),
    )

    network = _require_mapping(config.get("network"), "network")
    (
        plants,
        demand_zones,
        source_links,
        zone_links,
        network_issues,
    ) = _build_network(network, validation)

    known_source_ids = {source.source_id for source in sources}
    for link in source_links:
        if link.source_id not in known_source_ids:
            network_issues.append(
                f"Source-to-plant link references unavailable source "
                f"'{link.source_id}'."
            )

    # Preserve first occurrence order while removing duplicated diagnostics.
    issues = tuple(dict.fromkeys(source_issues + network_issues))

    scenario = ScenarioData(
        scenario_id=scenario_id,
        scenario_name=scenario_name,
        status=status,
        description=str(config.get("description", "")),
        sources=sources,
        plants=plants,
        demand_zones=demand_zones,
        source_to_plant_links=source_links,
        plant_to_zone_links=zone_links,
        quality_limits=quality_limits,
        validation_issues=issues,
    )

    if strict and issues:
        message = "\n".join(f"- {issue}" for issue in issues)
        raise DataLoadError("Scenario validation failed:\n" + message)

    return scenario


def print_summary(scenario: ScenarioData) -> None:
    """Print a compact scenario summary for local verification."""
    print(f"\nScenario: {scenario.scenario_name}")
    print(f"ID: {scenario.scenario_id}")
    print(f"Ready: {scenario.is_ready}")

    print("\nSources:")
    for source in scenario.sources:
        minimum = (
            f"{source.minimum_withdrawal_ml_per_day:g}"
            if source.minimum_withdrawal_ml_per_day is not None
            else "missing"
        )
        maximum = (
            f"{source.maximum_withdrawal_ml_per_day:g}"
            if source.maximum_withdrawal_ml_per_day is not None
            else "missing"
        )
        quality_keys = ", ".join(source.quality) or "none"
        print(
            f"- {source.name}: minimum={minimum} ML/day, "
            f"maximum={maximum} ML/day ({source.withdrawal_bounds_origin}); "
            f"quality={quality_keys}"
        )

    print("\nDemand zones:")
    for zone in scenario.demand_zones:
        demand = (
            f"{zone.demand_ml_per_day:g} ML/day"
            if zone.demand_ml_per_day is not None
            else "missing"
        )
        print(f"- {zone.name}: {demand}")

    if scenario.validation_issues:
        print("\nValidation issues:")
        for issue in scenario.validation_issues:
            print(f"- {issue}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Load an AquaBlend scenario from JSON using Supabase or inline source rows."
        )
    )
    parser.add_argument(
        "scenario",
        nargs="?",
        default=_DEFAULT_SCENARIO_PATH,
        help=(f"Path to the scenario JSON file. Defaults to {_DEFAULT_SCENARIO_PATH}."),
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Show incomplete data instead of stopping on validation issues.",
    )
    args = parser.parse_args()

    try:
        scenario = load_scenario(args.scenario, strict=not args.preview)
        print_summary(scenario)
    except DataLoadError as exc:
        raise SystemExit(f"\nData loader error:\n{exc}") from exc


if __name__ == "__main__":
    main()
