from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any


class DataLoadError(RuntimeError):
    """Raised when scenario or database data cannot be loaded safely."""


@dataclass(frozen=True)
class SourceInput:
    source_id: str
    name: str
    source_type: str
    enabled: bool
    forced_inactive: bool
    max_available_ml_per_day: float | None
    availability_origin: str
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
    minimum_operating_flow_ml_per_day: float
    maximum_processing_capacity_ml_per_day: float | None
    fixed_activation_cost: float
    treatment_cost_per_ml: float


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


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


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


def _build_sources(
    config_sources: list[dict[str, Any]],
    database_rows: list[dict[str, Any]],
    allow_estimated_values: bool,
    validation: dict[str, Any],
) -> tuple[tuple[SourceInput, ...], list[str]]:
    issues: list[str] = []

    enabled_configs = [
        item for item in config_sources if bool(item.get("enabled", True))
    ]

    source_ids = [str(item.get("source_id", "")).strip() for item in enabled_configs]

    if any(not source_id for source_id in source_ids):
        raise DataLoadError("Every enabled source must have a source_id.")

    if len(source_ids) != len(set(source_ids)):
        raise DataLoadError("The scenario contains duplicate source IDs.")

    rows_by_id = {str(row["source_id"]): row for row in database_rows}
    loaded_sources: list[SourceInput] = []

    for item in enabled_configs:
        source_id = str(item["source_id"])
        row = rows_by_id.get(source_id)

        if row is None:
            message = f"Source {source_id} was not found in the database."
            if validation.get("fail_if_source_missing_from_database", True):
                issues.append(message)
            continue

        override = item.get("max_available_ml_per_day_override")
        database_availability = _to_float(row["max_available_ml_per_day"])

        if override is not None:
            availability = _to_float(override)
            availability_origin = "scenario_override"
        else:
            availability = database_availability
            availability_origin = "database"

        forced_inactive = bool(item.get("forced_inactive", False))

        if (
            availability is None
            and not forced_inactive
            and validation.get("fail_if_daily_availability_missing", True)
        ):
            issues.append(
                f"{row['source_name']} is missing max availability in ML/day."
            )

        if availability is not None and availability < 0:
            issues.append(
                f"{row['source_name']} has a negative daily availability."
            )

        required_quality = {
            "pH": row["representative_ph"],
            "alkalinity": row["representative_alkalinity_mg_l_caco3"],
            "turbidity": row["representative_turbidity_ntu"],
            "cost": row["cost_per_ml"],
        }

        if validation.get("fail_if_required_quality_value_missing", True):
            for label, value in required_quality.items():
                if value is None:
                    issues.append(f"{row['source_name']} is missing {label}.")

        estimated_flags = [
            bool(row["cost_is_estimated"]),
            bool(row["alkalinity_is_estimated"]),
            bool(row["max_available_is_estimated"]),
            availability_origin == "scenario_override",
        ]
        has_estimated_values = any(estimated_flags)

        if has_estimated_values and not allow_estimated_values:
            issues.append(
                f"{row['source_name']} contains estimated or overridden values."
            )

        loaded_sources.append(
            SourceInput(
                source_id=source_id,
                name=str(row["source_name"]),
                source_type=str(row["source_type"]),
                enabled=True,
                forced_inactive=forced_inactive,
                max_available_ml_per_day=availability,
                availability_origin=availability_origin,
                cost_per_ml=_to_float(row["cost_per_ml"]),
                ph=_to_float(row["representative_ph"]),
                alkalinity_mg_l_caco3=_to_float(
                    row["representative_alkalinity_mg_l_caco3"]
                ),
                turbidity_ntu=_to_float(row["representative_turbidity_ntu"]),
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
        issues.append(
            "Skipped database sources: " + ", ".join(sorted(missing_ids))
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
    issues: list[str] = []

    plants = tuple(
        PlantInput(
            plant_id=str(item["plant_id"]),
            name=str(item["name"]),
            enabled=bool(item.get("enabled", True)),
            minimum_operating_flow_ml_per_day=_to_float(
                item.get("minimum_operating_flow_ml_per_day", 0)
            )
            or 0.0,
            maximum_processing_capacity_ml_per_day=_to_float(
                item.get("maximum_processing_capacity_ml_per_day")
            ),
            fixed_activation_cost=_to_float(
                item.get("fixed_activation_cost", 0)
            )
            or 0.0,
            treatment_cost_per_ml=_to_float(
                item.get("treatment_cost_per_ml", 0)
            )
            or 0.0,
        )
        for item in _require_list(network.get("plants", []), "network.plants")
    )

    demand_zones = tuple(
        DemandZoneInput(
            zone_id=str(item["zone_id"]),
            name=str(item["name"]),
            demand_ml_per_day=_to_float(item.get("demand_ml_per_day")),
            demand_must_be_met=bool(item.get("demand_must_be_met", True)),
        )
        for item in _require_list(
            network.get("demand_zones", []),
            "network.demand_zones",
        )
    )

    if validation.get("fail_if_demand_missing", True):
        for zone in demand_zones:
            if zone.demand_must_be_met and zone.demand_ml_per_day is None:
                issues.append(
                    f"{zone.name} is missing demand in ML/day."
                )

    source_links = tuple(
        SourcePlantLinkInput(
            source_id=str(item["source_id"]),
            plant_id=str(item["plant_id"]),
            enabled=bool(item.get("enabled", True)),
            maximum_flow_ml_per_day=_to_float(
                item.get("maximum_flow_ml_per_day")
            ),
        )
        for item in _require_list(
            network.get("source_to_plant_links", []),
            "network.source_to_plant_links",
        )
    )

    zone_links = tuple(
        PlantZoneLinkInput(
            plant_id=str(item["plant_id"]),
            zone_id=str(item["zone_id"]),
            enabled=bool(item.get("enabled", True)),
            maximum_flow_ml_per_day=_to_float(
                item.get("maximum_flow_ml_per_day")
            ),
        )
        for item in _require_list(
            network.get("plant_to_zone_links", []),
            "network.plant_to_zone_links",
        )
    )

    plant_ids = {plant.plant_id for plant in plants}
    zone_ids = {zone.zone_id for zone in demand_zones}

    for link in source_links:
        if link.plant_id not in plant_ids:
            issues.append(
                f"Source link references unknown plant {link.plant_id}."
            )

    for link in zone_links:
        if link.plant_id not in plant_ids:
            issues.append(
                f"Zone link references unknown plant {link.plant_id}."
            )
        if link.zone_id not in zone_ids:
            issues.append(
                f"Zone link references unknown zone {link.zone_id}."
            )

    return plants, demand_zones, source_links, zone_links, issues


def load_scenario(
    scenario_path: str | Path,
    *,
    strict: bool = True,
) -> ScenarioData:
    """Load one scenario and merge it with Supabase source data."""
    config = _read_json(scenario_path)

    data_source = _require_mapping(
        config.get("data_source"),
        "data_source",
    )
    validation = _require_mapping(
        config.get("validation", {}),
        "validation",
    )

    if data_source.get("type") != "supabase":
        raise DataLoadError('Only data_source.type = "supabase" is supported.')

    view_name = str(data_source.get("view", "")).strip()
    if not view_name:
        raise DataLoadError("data_source.view is required.")

    source_config = _require_list(config.get("sources"), "sources")
    enabled_source_ids = [
        str(item["source_id"])
        for item in source_config
        if bool(item.get("enabled", True))
    ]

    rows = _fetch_sources(
        _database_url(),
        view_name,
        enabled_source_ids,
    )

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

    known_source_ids = {source.source_id for source in sources}
    for link in source_links:
        if link.source_id not in known_source_ids:
            network_issues.append(
                f"Source link references unavailable source {link.source_id}."
            )

    issues = tuple(source_issues + network_issues)

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
        quality_limits=_require_mapping(
            config.get("quality_limits"),
            "quality_limits",
        ),
        treatment=_require_mapping(
            config.get("treatment", {}),
            "treatment",
        ),
        validation_issues=issues,
    )

    if strict and issues:
        message = "\n".join(f"- {issue}" for issue in issues)
        raise DataLoadError(
            "Scenario validation failed:\n" + message
        )

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
        print(
            f"- {source.name}: {availability} "
            f"({source.availability_origin})"
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
        scenario = load_scenario(
            args.scenario,
            strict=not args.preview,
        )
        print_summary(scenario)
    except DataLoadError as exc:
        raise SystemExit(f"\nData loader error:\n{exc}") from exc


if __name__ == "__main__":
    main()