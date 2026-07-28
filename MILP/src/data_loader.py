=from __future__ import annotations

import argparse
import json
import math
import os
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any


class DataLoadError(RuntimeError):
    """Raised when scenario or database data cannot be loaded safely."""


# Raw input objects may retain None in preview mode, but strict mode blocks them.
@dataclass(frozen=True)
class SourceInput:
    source_id: str
    name: str
    source_type: str
    enabled: bool
    forced_inactive: bool
    max_available_ml_per_day: float | None
    availability_origin: str
    fixed_activation_cost: float | None
    cost_per_ml: float | None
    ph: float | None
    alkalinity_mg_l_caco3: float | None
    turbidity_ntu: float | None
    has_estimated_values: bool
    database_model_ready: bool
    availability_status: str
    provenance: dict[str, str | None]


@dataclass(frozen=True)
class PlantInput:
    plant_id: str
    name: str
    enabled: bool
    minimum_operating_flow_ml_per_day: float | None
    maximum_processing_capacity_ml_per_day: float | None
    fixed_activation_cost: float | None
    treatment_cost_per_ml: float | None


@dataclass(frozen=True)
class DemandZoneInput:
    zone_id: str
    name: str
    demand_ml_per_day: float | None
    demand_must_be_met: bool


@dataclass(frozen=True)
class SourcePlantLinkInput:
    source_id: str
    plant_id: str
    enabled: bool
    maximum_flow_ml_per_day: float | None


@dataclass(frozen=True)
class PlantZoneLinkInput:
    plant_id: str
    zone_id: str
    enabled: bool
    maximum_flow_ml_per_day: float | None


@dataclass(frozen=True)
class ScenarioData:
    scenario_id: str
    scenario_name: str
    status: str
    description: str
    sources: tuple[SourceInput, ...]
    plants: tuple[PlantInput, ...]
    demand_zones: tuple[DemandZoneInput, ...]
    source_to_plant_links: tuple[SourcePlantLinkInput, ...]
    plant_to_zone_links: tuple[PlantZoneLinkInput, ...]
    quality_limits: dict[str, Any]
    treatment: dict[str, Any]
    validation_issues: tuple[str, ...]

    @property
    def is_ready(self) -> bool:
        return not self.validation_issues


def _to_float(value: Any, field_name: str) -> float | None:
    """Convert a numeric input while rejecting invalid and non-finite values."""
    if value is None:
        return None

    try:
        converted = float(value) if not isinstance(value, Decimal) else float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DataLoadError(f'"{field_name}" must be a valid number.') from exc

    if not math.isfinite(converted):
        raise DataLoadError(f'"{field_name}" must be finite.')

    return converted


def _append_required_non_negative_issue(
    issues: list[str],
    value: float | None,
    field_label: str,
    *,
    required: bool = True,
) -> None:
    """Record missing or negative model inputs without hiding the original value."""
    if value is None:
        if required:
            issues.append(f"{field_label} is missing.")
        return

    if value < 0:
        issues.append(f"{field_label} must be greater than or equal to zero.")


def _required_text(item: dict[str, Any], key: str, field_name: str) -> str:
    value = str(item.get(key, "")).strip()
    if not value:
        raise DataLoadError(f'"{field_name}" is required.')
    return value


def _read_json(path: str | Path) -> dict[str, Any]:
    scenario_path = Path(path)

    if not scenario_path.exists():
        raise DataLoadError(f"Scenario file not found: {scenario_path}")

    try:
        data = json.loads(scenario_path.read_text(encoding="utf-8"))
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
            'Install it with: pip install python-dotenv'
        ) from exc

    # Credentials remain local and are never stored in the scenario file.
    load_dotenv()
    value = os.getenv("DATABASE_URL")

    if not value:
        raise DataLoadError(
            "DATABASE_URL is missing. Add it to your local .env file."
        )

    return value


def _quoted_relation(name: str) -> str:
    """Allow safe schema.view names from the scenario file."""
    pattern = r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$"

    if not re.fullmatch(pattern, name):
        raise DataLoadError(f"Unsafe database view name: {name}")

    return ".".join(f'"{part}"' for part in name.split("."))


def _fetch_sources(
    database_url: str,
    view_name: str,
    source_ids: list[str],
) -> list[dict[str, Any]]:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise DataLoadError(
            'Missing dependency "psycopg". '
            'Install it with: pip install "psycopg[binary]"'
        ) from exc

    relation = _quoted_relation(view_name)

    # Source activation cost is scenario-specific and is loaded from the JSON.
    query = f"""
        SELECT
            source_id,
            source_name,
            source_type,
            is_active,
            storage_capacity_ml,
            reference_flow_ml_per_day,
            max_available_ml_per_day,
            cost_per_ml,
            representative_ph,
            representative_alkalinity_mg_l_caco3,
            representative_turbidity_ntu,
            storage_capacity_is_estimated,
            reference_flow_is_estimated,
            max_available_is_estimated,
            cost_is_estimated,
            alkalinity_is_estimated,
            storage_capacity_provenance,
            reference_flow_provenance,
            max_available_provenance,
            cost_provenance,
            alkalinity_provenance,
            availability_status,
            model_ready
        FROM {relation}
        WHERE source_id = ANY(%s)
          AND is_active = TRUE
        ORDER BY source_id;
    """

    try:
        with psycopg.connect(
            database_url,
            row_factory=dict_row,
            connect_timeout=10,
        ) as connection:
            with connection.cursor() as cursor:
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


def _enabled_mappings(value: Any, field_name: str) -> list[dict[str, Any]]:
    """Return enabled configuration objects and reject malformed list entries."""
    result: list[dict[str, Any]] = []

    for index, item in enumerate(_require_list(value, field_name)):
        if not isinstance(item, dict):
            raise DataLoadError(f'"{field_name}[{index}]" must be a JSON object.')
        if bool(item.get("enabled", True)):
            result.append(item)

    return result


def _build_sources(
    config_sources: list[dict[str, Any]],
    database_rows: list[dict[str, Any]],
    allow_estimated_values: bool,
    validation: dict[str, Any],
) -> tuple[tuple[SourceInput, ...], list[str]]:
    issues: list[str] = []
    enabled_configs = _enabled_mappings(config_sources, "sources")

    source_ids = [
        _required_text(item, "source_id", f"sources[{index}].source_id")
        for index, item in enumerate(enabled_configs)
    ]

    if len(source_ids) != len(set(source_ids)):
        raise DataLoadError("The scenario contains duplicate source IDs.")

    rows_by_id = {str(row["source_id"]).strip(): row for row in database_rows}
    loaded_sources: list[SourceInput] = []

    for index, item in enumerate(enabled_configs):
        source_id = source_ids[index]
        row = rows_by_id.get(source_id)

        if row is None:
            message = f"Source {source_id} was not found in the database."
            if validation.get("fail_if_source_missing_from_database", True):
                issues.append(message)
            continue

        override = item.get("max_available_ml_per_day_override")
        database_availability = _to_float(
            row["max_available_ml_per_day"],
            f"database source {source_id}.max_available_ml_per_day",
        )

        if override is not None:
            availability = _to_float(
                override,
                f"sources[{index}].max_available_ml_per_day_override",
            )
            availability_origin = "scenario_override"
        else:
            availability = database_availability
            availability_origin = "database"

        forced_inactive = bool(item.get("forced_inactive", False))
        source_name = str(row["source_name"])

        _append_required_non_negative_issue(
            issues,
            availability,
            f"{source_name} max availability in ML/day",
            required=(
                not forced_inactive
                and validation.get("fail_if_daily_availability_missing", True)
            ),
        )

        fixed_activation_cost = _to_float(
            item.get("fixed_activation_cost"),
            f"sources[{index}].fixed_activation_cost",
        )
        variable_cost = _to_float(
            row["cost_per_ml"],
            f"database source {source_id}.cost_per_ml",
        )

        # Costs belong to the objective and are validated separately from quality.
        _append_required_non_negative_issue(
            issues,
            fixed_activation_cost,
            f"{source_name} fixed activation cost",
        )
        _append_required_non_negative_issue(
            issues,
            variable_cost,
            f"{source_name} cost per ML",
        )

        ph = _to_float(
            row["representative_ph"],
            f"database source {source_id}.representative_ph",
        )
        alkalinity = _to_float(
            row["representative_alkalinity_mg_l_caco3"],
            f"database source {source_id}.representative_alkalinity_mg_l_caco3",
        )
        turbidity = _to_float(
            row["representative_turbidity_ntu"],
            f"database source {source_id}.representative_turbidity_ntu",
        )

        if validation.get("fail_if_required_quality_value_missing", True):
            if ph is None:
                issues.append(f"{source_name} is missing pH.")
            if alkalinity is None:
                issues.append(f"{source_name} is missing alkalinity.")
            if turbidity is None:
                issues.append(f"{source_name} is missing turbidity.")

        if ph is not None and not 0 <= ph <= 10:
            issues.append(f"{source_name} pH must be between 0 and 10.")
        if alkalinity is not None and alkalinity < 0:
            issues.append(f"{source_name} alkalinity must be non-negative.")
        if turbidity is not None and turbidity < 0:
            issues.append(f"{source_name} turbidity must be non-negative.")

        estimated_flags = [
            bool(row["cost_is_estimated"]),
            bool(row["alkalinity_is_estimated"]),
            bool(row["max_available_is_estimated"]),
            availability_origin == "scenario_override",
        ]
        has_estimated_values = any(estimated_flags)

        if has_estimated_values and not allow_estimated_values:
            issues.append(
                f"{source_name} contains estimated or overridden values."
            )

        loaded_sources.append(
            SourceInput(
                source_id=source_id,
                name=source_name,
                source_type=str(row["source_type"]),
                enabled=True,
                forced_inactive=forced_inactive,
                max_available_ml_per_day=availability,
                availability_origin=availability_origin,
                fixed_activation_cost=fixed_activation_cost,
                cost_per_ml=variable_cost,
                ph=ph,
                alkalinity_mg_l_caco3=alkalinity,
                turbidity_ntu=turbidity,
                has_estimated_values=has_estimated_values,
                database_model_ready=bool(row["model_ready"]),
                availability_status=str(row["availability_status"]),
                provenance={
                    "storage_capacity": row["storage_capacity_provenance"],
                    "reference_flow": row["reference_flow_provenance"],
                    "max_available": row["max_available_provenance"],
                    "cost": row["cost_provenance"],
                    "alkalinity": row["alkalinity_provenance"],
                },
            )
        )

    returned_ids = {source.source_id for source in loaded_sources}
    missing_ids = set(source_ids) - returned_ids

    if missing_ids and not validation.get(
        "fail_if_source_missing_from_database", True
    ):
        issues.append("Skipped database sources: " + ", ".join(sorted(missing_ids)))

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
    issues: list[str] = []

    plant_configs = _enabled_mappings(network.get("plants", []), "network.plants")
    zone_configs = _enabled_mappings(
        network.get("demand_zones", []),
        "network.demand_zones",
    )
    source_link_configs = _enabled_mappings(
        network.get("source_to_plant_links", []),
        "network.source_to_plant_links",
    )
    zone_link_configs = _enabled_mappings(
        network.get("plant_to_zone_links", []),
        "network.plant_to_zone_links",
    )

    plants: list[PlantInput] = []
    for index, item in enumerate(plant_configs):
        plant_id = _required_text(
            item,
            "plant_id",
            f"network.plants[{index}].plant_id",
        )
        name = _required_text(item, "name", f"network.plants[{index}].name")

        # Minimum flow is an optional business rule and defaults explicitly to zero.
        minimum_flow = _to_float(
            item.get("minimum_operating_flow_ml_per_day", 0),
            f"network.plants[{index}].minimum_operating_flow_ml_per_day",
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

        _append_required_non_negative_issue(
            issues,
            minimum_flow,
            f"{name} minimum operating flow",
        )
        _append_required_non_negative_issue(
            issues,
            maximum_capacity,
            f"{name} maximum processing capacity",
        )
        _append_required_non_negative_issue(
            issues,
            fixed_cost,
            f"{name} fixed activation cost",
        )
        _append_required_non_negative_issue(
            issues,
            treatment_cost,
            f"{name} treatment cost per ML",
        )

        plants.append(
            PlantInput(
                plant_id=plant_id,
                name=name,
                enabled=True,
                minimum_operating_flow_ml_per_day=minimum_flow,
                maximum_processing_capacity_ml_per_day=maximum_capacity,
                fixed_activation_cost=fixed_cost,
                treatment_cost_per_ml=treatment_cost,
            )
        )

    plant_ids_in_order = [plant.plant_id for plant in plants]
    if len(plant_ids_in_order) != len(set(plant_ids_in_order)):
        raise DataLoadError("The scenario contains duplicate plant IDs.")

    demand_zones: list[DemandZoneInput] = []
    for index, item in enumerate(zone_configs):
        zone_id = _required_text(
            item,
            "zone_id",
            f"network.demand_zones[{index}].zone_id",
        )
        name = _required_text(
            item,
            "name",
            f"network.demand_zones[{index}].name",
        )
        demand_must_be_met = bool(item.get("demand_must_be_met", True))
        demand = _to_float(
            item.get("demand_ml_per_day"),
            f"network.demand_zones[{index}].demand_ml_per_day",
        )

        _append_required_non_negative_issue(
            issues,
            demand,
            f"{name} demand in ML/day",
            required=(
                demand_must_be_met
                and validation.get("fail_if_demand_missing", True)
            ),
        )

        demand_zones.append(
            DemandZoneInput(
                zone_id=zone_id,
                name=name,
                demand_ml_per_day=demand,
                demand_must_be_met=demand_must_be_met,
            )
        )

    zone_ids_in_order = [zone.zone_id for zone in demand_zones]
    if len(zone_ids_in_order) != len(set(zone_ids_in_order)):
        raise DataLoadError("The scenario contains duplicate demand zone IDs.")

    source_links: list[SourcePlantLinkInput] = []
    source_link_keys: list[tuple[str, str]] = []
    for index, item in enumerate(source_link_configs):
        source_id = _required_text(
            item,
            "source_id",
            f"network.source_to_plant_links[{index}].source_id",
        )
        plant_id = _required_text(
            item,
            "plant_id",
            f"network.source_to_plant_links[{index}].plant_id",
        )
        maximum_flow = _to_float(
            item.get("maximum_flow_ml_per_day"),
            f"network.source_to_plant_links[{index}].maximum_flow_ml_per_day",
        )

        _append_required_non_negative_issue(
            issues,
            maximum_flow,
            f"Source-to-plant link {source_id} -> {plant_id} maximum flow",
        )

        source_link_keys.append((source_id, plant_id))
        source_links.append(
            SourcePlantLinkInput(
                source_id=source_id,
                plant_id=plant_id,
                enabled=True,
                maximum_flow_ml_per_day=maximum_flow,
            )
        )

    if len(source_link_keys) != len(set(source_link_keys)):
        raise DataLoadError(
            "The scenario contains duplicate source-to-plant links."
        )

    zone_links: list[PlantZoneLinkInput] = []
    zone_link_keys: list[tuple[str, str]] = []
    for index, item in enumerate(zone_link_configs):
        plant_id = _required_text(
            item,
            "plant_id",
            f"network.plant_to_zone_links[{index}].plant_id",
        )
        zone_id = _required_text(
            item,
            "zone_id",
            f"network.plant_to_zone_links[{index}].zone_id",
        )
        maximum_flow = _to_float(
            item.get("maximum_flow_ml_per_day"),
            f"network.plant_to_zone_links[{index}].maximum_flow_ml_per_day",
        )

        _append_required_non_negative_issue(
            issues,
            maximum_flow,
            f"Plant-to-zone link {plant_id} -> {zone_id} maximum flow",
        )

        zone_link_keys.append((plant_id, zone_id))
        zone_links.append(
            PlantZoneLinkInput(
                plant_id=plant_id,
                zone_id=zone_id,
                enabled=True,
                maximum_flow_ml_per_day=maximum_flow,
            )
        )

    if len(zone_link_keys) != len(set(zone_link_keys)):
        raise DataLoadError(
            "The scenario contains duplicate plant-to-zone links."
        )

    plant_ids = set(plant_ids_in_order)
    zone_ids = set(zone_ids_in_order)

    for link in source_links:
        if link.plant_id not in plant_ids:
            issues.append(f"Source link references unknown plant {link.plant_id}.")

    for link in zone_links:
        if link.plant_id not in plant_ids:
            issues.append(f"Zone link references unknown plant {link.plant_id}.")
        if link.zone_id not in zone_ids:
            issues.append(f"Zone link references unknown zone {link.zone_id}.")

    return (
        tuple(plants),
        tuple(demand_zones),
        tuple(source_links),
        tuple(zone_links),
        issues,
    )


def _validate_quality_limits(
    quality_limits: dict[str, Any],
) -> list[str]:
    """Validate the raw bounds; pH conversion remains a preprocessing task."""
    issues: list[str] = []
    required_parameters = {
        "ph": "pH",
        "alkalinity_mg_l_caco3": "alkalinity",
        "turbidity_ntu": "turbidity",
    }

    for key, label in required_parameters.items():
        value = quality_limits.get(key)
        if not isinstance(value, dict):
            issues.append(f"Quality limits for {label} are missing.")
            continue

        minimum = _to_float(value.get("minimum"), f"quality_limits.{key}.minimum")
        maximum = _to_float(value.get("maximum"), f"quality_limits.{key}.maximum")

        if minimum is None:
            issues.append(f"{label} minimum quality limit is missing.")
        if maximum is None:
            issues.append(f"{label} maximum quality limit is missing.")
        if minimum is None or maximum is None:
            continue

        if minimum > maximum:
            issues.append(f"{label} minimum quality limit exceeds its maximum.")

        if key == "ph":
            if not 0 <= minimum <= 10 or not 0 <= maximum <= 10:
                issues.append("pH quality limits must be between 0 and 10.")
        elif minimum < 0 or maximum < 0:
            issues.append(f"{label} quality limits must be non-negative.")

    return issues


def load_scenario(
    scenario_path: str | Path,
    *,
    strict: bool = True,
) -> ScenarioData:
    """Load one scenario and merge it with Supabase source data."""
    config = _read_json(scenario_path)

    data_source = _require_mapping(config.get("data_source"), "data_source")
    validation = _require_mapping(config.get("validation", {}), "validation")

    if data_source.get("type") != "supabase":
        raise DataLoadError('Only data_source.type = "supabase" is supported.')

    view_name = str(data_source.get("view", "")).strip()
    if not view_name:
        raise DataLoadError("data_source.view is required.")

    source_config = _require_list(config.get("sources"), "sources")
    enabled_sources = _enabled_mappings(source_config, "sources")
    enabled_source_ids = [
        _required_text(item, "source_id", f"sources[{index}].source_id")
        for index, item in enumerate(enabled_sources)
    ]

    rows = _fetch_sources(_database_url(), view_name, enabled_source_ids)

    sources, source_issues = _build_sources(
        source_config,
        rows,
        bool(data_source.get("allow_estimated_values", False)),
        validation,
    )

    network = _require_mapping(config.get("network"), "network")
    (
        plants,
        demand_zones,
        source_links,
        zone_links,
        network_issues,
    ) = _build_network(network, validation)

    # References are checked after source and network filtering are complete.
    known_source_ids = {source.source_id for source in sources}
    for link in source_links:
        if link.source_id not in known_source_ids:
            network_issues.append(
                f"Source link references unavailable source {link.source_id}."
            )

    quality_limits = _require_mapping(
        config.get("quality_limits"),
        "quality_limits",
    )
    quality_issues = _validate_quality_limits(quality_limits)
    issues = tuple(source_issues + network_issues + quality_issues)

    scenario = ScenarioData(
        scenario_id=str(config.get("scenario_id", "")).strip(),
        scenario_name=str(config.get("scenario_name", "")).strip(),
        status=str(config.get("status", "draft")),
        description=str(config.get("description", "")),
        sources=sources,
        plants=plants,
        demand_zones=demand_zones,
        source_to_plant_links=source_links,
        plant_to_zone_links=zone_links,
        quality_limits=quality_limits,
        treatment=_require_mapping(config.get("treatment", {}), "treatment"),
        validation_issues=issues,
    )

    # Strict mode is the final gate before preprocessing and model construction.
    if strict and issues:
        message = "\n".join(f"- {issue}" for issue in issues)
        raise DataLoadError("Scenario validation failed:\n" + message)

    return scenario


def print_summary(scenario: ScenarioData) -> None:
    print(f"\nScenario: {scenario.scenario_name}")
    print(f"ID: {scenario.scenario_id}")
    print(f"Ready: {scenario.is_ready}")

    print("\nSources:")
    for source in scenario.sources:
        availability = (
            f"{source.max_available_ml_per_day:g} ML/day"
            if source.max_available_ml_per_day is not None
            else "missing"
        )
        print(f"- {source.name}: {availability} ({source.availability_origin})")

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
        description="Load an Aqua Blend scenario from JSON and Supabase."
    )
    parser.add_argument(
        "scenario",
        nargs="?",
        default="config/scenarios/base_scenarios_v1.json",
        help="Path to the scenario JSON file.",
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