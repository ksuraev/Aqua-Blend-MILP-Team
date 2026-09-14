"""Final AquaBlend MILP pipeline for run-scoped, cacheable execution.

Production boundary
-------------------
public.milp_model_input.canonical_input_json
    -> data_loader.load_scenario()
    -> ScenarioData
    -> persist scenario_data_json + scenario_data_hash
    -> preprocessing.preprocess_scenario()
    -> ModelParameters
    -> persist model_parameters_json + model_parameters_hash
    -> derive solver_config_hash + execution_key
    -> (worker performs cache lookup)
    -> model.solve()
    -> postprocessing.postprocess_solution()
    -> SolvedScenario

The pipeline intentionally does NOT decide whether a cached output should be
reused.  Cache orchestration belongs to run_worker.py.  This module owns only
real MILP stage preparation/serialization and the actual solve/postprocess path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
from dotenv import load_dotenv

load_dotenv()

from pyomo.opt import TerminationCondition

try:
    from .contracts import (
        DataSource,
        InputValidationPolicy,
        LoaderValidation,
        PreprocessingValidation,
        ScenarioData,
        SolvedScenario,
        ValidationCheck,
    )
    from .data_loader import DataLoadError, load_scenario
    from .model import solve
    from .postprocessing import PostprocessingError, postprocess_solution
    from .preprocessing import ModelParameters, PreprocessingError, preprocess_scenario
except ImportError:  # pragma: no cover - direct script execution
    from postprocessing import PostprocessingError, postprocess_solution

    from contracts import (
        DataSource,
        InputValidationPolicy,
        LoaderValidation,
        PreprocessingValidation,
        ScenarioData,
        SolvedScenario,
        ValidationCheck,
    )
    from data_loader import DataLoadError, load_scenario
    from model import solve
    from preprocessing import ModelParameters, PreprocessingError, preprocess_scenario


LOGGER = logging.getLogger("aquablend.milp.pipeline")


class PipelineError(RuntimeError):
    """Base exception for AquaBlend pipeline execution failures."""


class PipelineStageError(PipelineError):
    """Failure raised at a named stage of one optimisation run."""

    def __init__(
        self,
        stage: str,
        message: str,
        *,
        run_id: str | None = None,
        scenario_id: str | None = None,
        input_id: str | None = None,
    ) -> None:
        super().__init__(f"{stage}: {message}")
        self.stage = stage
        self.run_id = run_id
        self.scenario_id = scenario_id
        self.input_id = input_id


@dataclass(frozen=True, slots=True)
class PipelineOptions:
    validate_capacity_feasibility: bool = True
    validate_quality_feasibility: bool = True
    tee: bool = False


@dataclass(frozen=True, slots=True)
class MilpInputSnapshot:
    id: str
    run_database_id: int
    scenario_database_id: int
    run_external_id: str
    scenario_external_id: str
    scenario_name: str
    scenario_status: str
    input_contract_version: str
    scenario_data_schema_version: str
    model_parameters_schema_version: str
    canonical_input_json: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PreparedMilpInput:
    snapshot: MilpInputSnapshot
    scenario: ScenarioData
    parameters: ModelParameters
    scenario_data_json: Any
    model_parameters_json: Any
    source_data_snapshot_json: Any
    canonical_input_hash: str
    scenario_data_hash: str
    model_parameters_hash: str
    input_hash: str
    solver_config_json: dict[str, Any]
    solver_config_hash: str
    milp_model_version: str
    execution_key: str


class _ScenarioPostprocessingView:
    __slots__ = ("_scenario", "input_policy_validation", "loader_validation")

    def __init__(
        self,
        scenario: ScenarioData,
        *,
        input_policy_validation: InputValidationPolicy,
        loader_validation: LoaderValidation,
    ) -> None:
        self._scenario = scenario
        self.input_policy_validation = input_policy_validation
        self.loader_validation = loader_validation

    def __getattr__(self, name: str) -> Any:
        return getattr(self._scenario, name)


class _ParametersPostprocessingView:
    __slots__ = ("_parameters", "preprocessing_validation", "run_id")

    def __init__(
        self,
        parameters: ModelParameters,
        *,
        run_id: str,
        preprocessing_validation: PreprocessingValidation,
    ) -> None:
        self._parameters = parameters
        self.run_id = run_id
        self.preprocessing_validation = preprocessing_validation

    def __getattr__(self, name: str) -> Any:
        return getattr(self._parameters, name)


def _load_environment() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise PipelineError(
            'Missing dependency "python-dotenv". Install MILP requirements first.'
        ) from exc
    load_dotenv()


def _database_url() -> str:
    _load_environment()
    value = os.getenv("DATABASE_URL")
    if not value:
        raise PipelineError(
            "DATABASE_URL is missing. Add the Supabase PostgreSQL connection "
            "string to the MILP environment."
        )
    return value


def _connect():
    return psycopg.connect(
        host=os.environ["PGHOST"],
        port=int(os.getenv("PGPORT", "5432")),
        dbname=os.getenv("PGDATABASE", "postgres"),
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
        sslmode=os.getenv("PGSSLMODE", "require"),
        connect_timeout=15,
    )


def _clean_nonblank(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PipelineError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _normalise_json_value(value: Any) -> Any:
    """Convert dataclass/model values into deterministic JSON-compatible data.

    Non-string mappings (common for mathematical parameter dictionaries keyed by
    tuples) are represented as sorted ``[{"key": ..., "value": ...}]`` entries.
    That representation remains readable and hashes deterministically.
    """

    if value is None or isinstance(value, (str, bool, int)):
        return value

    if isinstance(value, float):
        if not math.isfinite(value):
            raise PipelineError("NaN/Infinity cannot be persisted or hashed.")
        return value

    if isinstance(value, Decimal):
        # Preserve Decimal precision and type rather than silently coercing to float.
        return {"__decimal__": format(value, "f")}

    if isinstance(value, Enum):
        return _normalise_json_value(value.value)

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if isinstance(value, Path):
        return str(value)

    if isinstance(value, UUID):
        return str(value)

    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _normalise_json_value(getattr(value, field.name))
            for field in fields(value)
        }

    if isinstance(value, Mapping):
        if all(isinstance(key, str) for key in value):
            return {
                str(key): _normalise_json_value(value[key]) for key in sorted(value)
            }

        entries: list[dict[str, Any]] = []
        for key, item in value.items():
            normalised_key = _normalise_json_value(key)
            normalised_value = _normalise_json_value(item)
            entries.append({"key": normalised_key, "value": normalised_value})
        entries.sort(key=lambda entry: stable_json_dumps(entry["key"]))
        return entries

    if isinstance(value, (list, tuple)):
        return [_normalise_json_value(item) for item in value]

    if isinstance(value, (set, frozenset)):
        items = [_normalise_json_value(item) for item in value]
        items.sort(key=stable_json_dumps)
        return items

    # Numpy scalar support without introducing numpy as a dependency.
    item_method = getattr(value, "item", None)
    if callable(item_method) and value.__class__.__module__.startswith("numpy"):
        return _normalise_json_value(item_method())

    raise PipelineError(
        "Unsupported value while serialising MILP contract: "
        f"{type(value).__module__}.{type(value).__qualname__}"
    )


def stable_json_dumps(value: Any) -> str:
    normalised = _normalise_json_value(value)
    try:
        return json.dumps(
            normalised,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise PipelineError("Value cannot be deterministically serialised.") from exc


def stable_hash(value: Any) -> str:
    return hashlib.sha256(stable_json_dumps(value).encode("utf-8")).hexdigest()


def json_for_database(value: Any) -> str:
    """Strict compact JSON suitable for a PostgreSQL jsonb parameter."""

    normalised = _normalise_json_value(value)
    try:
        return json.dumps(
            normalised,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise PipelineError(
            "Value is not valid JSON for database persistence."
        ) from exc


def _ensure_json_object(value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        document = dict(value)
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise PipelineError(f"{label} is not valid JSON.") from exc
        if not isinstance(parsed, dict):
            raise PipelineError(f"{label} must contain a JSON object.")
        document = parsed
    else:
        raise PipelineError(f"{label} must be a JSON object.")

    # Strict JSON proof.
    json.dumps(document, allow_nan=False)
    return document


def _validate_canonical_document(
    document: Mapping[str, Any],
    *,
    expected_scenario_id: str,
) -> dict[str, Any]:
    result = dict(document)
    required = {
        "scenario_id",
        "scenario_name",
        "status",
        "data_source",
        "validation",
        "sources",
        "network",
        "quality_limits",
    }
    missing = sorted(required.difference(result))
    if missing:
        raise PipelineError(
            "canonical_input_json is missing required field(s): " + ", ".join(missing)
        )

    scenario_id = _clean_nonblank(result.get("scenario_id"), "scenario_id")
    if scenario_id != expected_scenario_id:
        raise PipelineError(
            "Canonical scenario ID mismatch: input snapshot expects "
            f"{expected_scenario_id!r}, JSON contains {scenario_id!r}."
        )

    network = result.get("network")
    if not isinstance(network, Mapping):
        raise PipelineError("canonical_input_json.network must be an object.")
    required_network = {
        "plants",
        "demand_zones",
        "source_to_plant_links",
        "plant_to_zone_links",
    }
    missing_network = sorted(required_network.difference(network))
    if missing_network:
        raise PipelineError(
            "canonical_input_json.network is missing field(s): "
            + ", ".join(missing_network)
        )
    return result


def fetch_milp_input_snapshot(input_id: str | UUID) -> MilpInputSnapshot:
    try:
        parsed_id = UUID(str(input_id))
    except (TypeError, ValueError) as exc:
        raise PipelineError(f"Invalid milp_model_input id: {input_id!r}") from exc

    query = """
        SELECT
            i.id,
            i.run_id,
            i.scenario_id,
            i.run_external_id,
            i.scenario_external_id,
            i.scenario_name,
            i.scenario_status,
            i.input_contract_version,
            i.scenario_data_schema_version,
            i.model_parameters_schema_version,
            i.canonical_input_json
        FROM public.milp_model_input AS i
        WHERE i.id = %s
        LIMIT 2;
    """

    try:
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute(query, (parsed_id,))
            rows = cursor.fetchall()
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError("Could not fetch milp_model_input from Supabase.") from exc

    if not rows:
        raise PipelineError(f"milp_model_input {parsed_id} was not found.")
    if len(rows) > 1:
        raise PipelineError(f"Duplicate milp_model_input id detected: {parsed_id}.")

    row = rows[0]
    scenario_external_id = _clean_nonblank(row[4], "scenario_external_id")
    canonical = _ensure_json_object(row[10], "canonical_input_json")
    canonical = _validate_canonical_document(
        canonical,
        expected_scenario_id=scenario_external_id,
    )

    return MilpInputSnapshot(
        id=str(row[0]),
        run_database_id=int(row[1]),
        scenario_database_id=int(row[2]),
        run_external_id=_clean_nonblank(row[3], "run_external_id"),
        scenario_external_id=scenario_external_id,
        scenario_name=_clean_nonblank(row[5], "scenario_name"),
        scenario_status=_clean_nonblank(row[6], "scenario_status"),
        input_contract_version=_clean_nonblank(
            row[7] or "1.0", "input_contract_version"
        ),
        scenario_data_schema_version=_clean_nonblank(
            row[8] or "1.0", "scenario_data_schema_version"
        ),
        model_parameters_schema_version=_clean_nonblank(
            row[9] or "1.0", "model_parameters_schema_version"
        ),
        canonical_input_json=canonical,
    )


def _load_scenario_from_mapping(scenario_json: Mapping[str, Any]) -> ScenarioData:
    encoded = json.dumps(
        dict(scenario_json),
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
    )
    with tempfile.TemporaryDirectory(prefix="aquablend_scenario_") as temp_dir:
        path = Path(temp_dir) / "scenario.json"
        path.write_text(encoded, encoding="utf-8")
        return load_scenario(path, strict=True)


def _extract_source_snapshot(scenario_json: Any) -> Any:
    if isinstance(scenario_json, Mapping):
        return scenario_json.get("sources", [])
    return []


def _mark_input_status(input_id: str, status: str) -> None:
    try:
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                    UPDATE public.milp_model_input
                    SET input_status = %s
                    WHERE id = %s;
                    """,
                (status, UUID(input_id)),
            )
    except Exception:
        # Never mask the original pipeline failure over a status-marking error.
        LOGGER.warning(
            "Could not mark milp_model_input %s as %r.",
            input_id,
            status,
            exc_info=True,
        )


def _persist_loaded_scenario(
    snapshot: MilpInputSnapshot,
    *,
    canonical_input_hash: str,
    scenario_data_json: Any,
    scenario_data_hash: str,
    source_data_snapshot_json: Any,
) -> None:
    query = """
        UPDATE public.milp_model_input
        SET
            input_status = 'loaded',
            canonical_input_hash = %s,
            scenario_data_json = %s::jsonb,
            scenario_data_hash = %s,
            source_data_snapshot_json = %s::jsonb
        WHERE id = %s;
    """
    try:
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                query,
                (
                    canonical_input_hash,
                    json_for_database(scenario_data_json),
                    scenario_data_hash,
                    json_for_database(source_data_snapshot_json),
                    UUID(snapshot.id),
                ),
            )
            if cursor.rowcount != 1:
                raise PipelineError(
                    f"milp_model_input {snapshot.id} disappeared during loading."
                )
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError("Could not persist ScenarioData snapshot.") from exc


def _persist_preprocessed_parameters(
    snapshot: MilpInputSnapshot,
    *,
    model_parameters_json: Any,
    model_parameters_hash: str,
    input_hash: str,
    solver_config_json: Mapping[str, Any],
    solver_config_hash: str,
    milp_model_version: str,
    execution_key: str,
) -> None:
    query = """
        UPDATE public.milp_model_input
        SET
            input_status = 'resolved',
            model_parameters_json = %s::jsonb,
            model_parameters_hash = %s,
            input_hash = %s,
            solver_config_json = %s::jsonb,
            solver_config_hash = %s,
            milp_model_version = %s,
            execution_key = %s,
            resolved_at = now()
        WHERE id = %s;
    """
    try:
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                query,
                (
                    json_for_database(model_parameters_json),
                    model_parameters_hash,
                    input_hash,
                    json_for_database(dict(solver_config_json)),
                    solver_config_hash,
                    milp_model_version,
                    execution_key,
                    UUID(snapshot.id),
                ),
            )
            if cursor.rowcount != 1:
                raise PipelineError(
                    f"milp_model_input {snapshot.id} disappeared during preprocessing."
                )
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError("Could not persist ModelParameters snapshot.") from exc


def _repo_root() -> Path:
    # pipeline.py normally lives in MILP/src/.
    return Path(__file__).resolve().parent.parent


def _git_output(args: Sequence[str]) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(_repo_root()), *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip()


def _dirty_source_fingerprint() -> str:
    src_root = Path(__file__).resolve().parent
    hasher = hashlib.sha256()
    found = False
    for path in sorted(src_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        found = True
        hasher.update(str(path.relative_to(src_root)).encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    if not found:
        raise PipelineError("Could not fingerprint MILP source files.")
    return hasher.hexdigest()[:16]


def resolve_milp_model_version() -> str:
    """Return a cache-safe model version.

    Priority:
      1. AQUABLEND_MILP_MODEL_VERSION
      2. git commit, with a source fingerprint suffix for a dirty worktree

    Failing closed prevents unsafe cache reuse under an unknown code version.
    """

    _load_environment()
    explicit = os.getenv("AQUABLEND_MILP_MODEL_VERSION")
    if explicit and explicit.strip():
        return explicit.strip()

    commit = _git_output(["rev-parse", "HEAD"])
    if commit:
        dirty = _git_output(["status", "--porcelain", "--", "src"])
        if dirty:
            return f"git:{commit}:dirty:{_dirty_source_fingerprint()}"
        return f"git:{commit}"

    raise PipelineError(
        "A cache-safe MILP model version could not be determined. Set "
        "AQUABLEND_MILP_MODEL_VERSION (for example to a release tag or commit SHA)."
    )


def _solver_package_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("highspy")
        except PackageNotFoundError:
            return "unknown"
    except Exception:
        # Version probing must never fail a run; it only annotates solver config.
        LOGGER.warning("Could not resolve the highspy package version.", exc_info=True)
        return "unknown"


def build_solver_config() -> dict[str, Any]:
    """Return only configuration that can affect mathematical solver results."""

    return {
        "solver_name": "appsi_highs",
        "solver_package": "highspy",
        "solver_package_version": _solver_package_version(),
    }


def build_execution_key(
    *,
    model_parameters_hash: str,
    milp_model_version: str,
    solver_config_hash: str,
    input_contract_version: str,
    model_parameters_schema_version: str,
) -> str:
    identity = {
        "model_parameters_hash": model_parameters_hash,
        "milp_model_version": milp_model_version,
        "solver_config_hash": solver_config_hash,
        "input_contract_version": input_contract_version,
        "model_parameters_schema_version": model_parameters_schema_version,
    }
    return stable_hash(identity)


def prepare_milp_input(
    input_id: str | UUID,
    *,
    options: PipelineOptions | None = None,
) -> PreparedMilpInput:
    """Resolve one backend-created input snapshot into real ModelParameters.

    This function persists ScenarioData and ModelParameters snapshots/hashes but
    never performs a cache lookup and never calls the solver.
    """

    options = options or PipelineOptions()
    snapshot = fetch_milp_input_snapshot(input_id)
    _mark_input_status(snapshot.id, "resolving")

    canonical_input_hash = stable_hash(snapshot.canonical_input_json)

    try:
        scenario = _load_scenario_from_mapping(snapshot.canonical_input_json)
    except DataLoadError as exc:
        _mark_input_status(snapshot.id, "failed")
        raise PipelineStageError(
            "DATA_LOADING",
            str(exc),
            run_id=snapshot.run_external_id,
            scenario_id=snapshot.scenario_external_id,
            input_id=snapshot.id,
        ) from exc
    except Exception as exc:
        _mark_input_status(snapshot.id, "failed")
        raise PipelineStageError(
            "DATA_LOADING",
            f"{type(exc).__name__}: {exc}",
            run_id=snapshot.run_external_id,
            scenario_id=snapshot.scenario_external_id,
            input_id=snapshot.id,
        ) from exc

    if scenario.scenario_id != snapshot.scenario_external_id:
        _mark_input_status(snapshot.id, "failed")
        raise PipelineStageError(
            "DATA_LOADING",
            "ScenarioData.scenario_id does not match the immutable run snapshot.",
            run_id=snapshot.run_external_id,
            scenario_id=snapshot.scenario_external_id,
            input_id=snapshot.id,
        )

    scenario_data_json = _normalise_json_value(scenario)
    scenario_data_hash = stable_hash(scenario_data_json)
    source_snapshot = _extract_source_snapshot(scenario_data_json)

    try:
        _persist_loaded_scenario(
            snapshot,
            canonical_input_hash=canonical_input_hash,
            scenario_data_json=scenario_data_json,
            scenario_data_hash=scenario_data_hash,
            source_data_snapshot_json=source_snapshot,
        )
    except Exception as exc:
        _mark_input_status(snapshot.id, "failed")
        raise PipelineStageError(
            "INPUT_PERSISTENCE",
            str(exc),
            run_id=snapshot.run_external_id,
            scenario_id=snapshot.scenario_external_id,
            input_id=snapshot.id,
        ) from exc

    try:
        parameters = preprocess_scenario(
            scenario,
            validate_capacity_feasibility=options.validate_capacity_feasibility,
            validate_quality_feasibility=options.validate_quality_feasibility,
        )
    except PreprocessingError as exc:
        _mark_input_status(snapshot.id, "failed")
        raise PipelineStageError(
            "PREPROCESSING",
            str(exc),
            run_id=snapshot.run_external_id,
            scenario_id=snapshot.scenario_external_id,
            input_id=snapshot.id,
        ) from exc
    except Exception as exc:
        _mark_input_status(snapshot.id, "failed")
        raise PipelineStageError(
            "PREPROCESSING",
            f"{type(exc).__name__}: {exc}",
            run_id=snapshot.run_external_id,
            scenario_id=snapshot.scenario_external_id,
            input_id=snapshot.id,
        ) from exc

    model_parameters_json = _normalise_json_value(parameters)
    model_parameters_hash = stable_hash(model_parameters_json)
    input_hash = model_parameters_hash
    solver_config_json = build_solver_config()
    solver_config_hash = stable_hash(solver_config_json)

    try:
        milp_model_version = resolve_milp_model_version()
    except Exception as exc:
        _mark_input_status(snapshot.id, "failed")
        raise PipelineStageError(
            "VERSIONING",
            str(exc),
            run_id=snapshot.run_external_id,
            scenario_id=snapshot.scenario_external_id,
            input_id=snapshot.id,
        ) from exc

    execution_key = build_execution_key(
        model_parameters_hash=model_parameters_hash,
        milp_model_version=milp_model_version,
        solver_config_hash=solver_config_hash,
        input_contract_version=snapshot.input_contract_version,
        model_parameters_schema_version=snapshot.model_parameters_schema_version,
    )

    try:
        _persist_preprocessed_parameters(
            snapshot,
            model_parameters_json=model_parameters_json,
            model_parameters_hash=model_parameters_hash,
            input_hash=input_hash,
            solver_config_json=solver_config_json,
            solver_config_hash=solver_config_hash,
            milp_model_version=milp_model_version,
            execution_key=execution_key,
        )
    except Exception as exc:
        _mark_input_status(snapshot.id, "failed")
        raise PipelineStageError(
            "INPUT_PERSISTENCE",
            str(exc),
            run_id=snapshot.run_external_id,
            scenario_id=snapshot.scenario_external_id,
            input_id=snapshot.id,
        ) from exc

    return PreparedMilpInput(
        snapshot=snapshot,
        scenario=scenario,
        parameters=parameters,
        scenario_data_json=scenario_data_json,
        model_parameters_json=model_parameters_json,
        source_data_snapshot_json=source_snapshot,
        canonical_input_hash=canonical_input_hash,
        scenario_data_hash=scenario_data_hash,
        model_parameters_hash=model_parameters_hash,
        input_hash=input_hash,
        solver_config_json=solver_config_json,
        solver_config_hash=solver_config_hash,
        milp_model_version=milp_model_version,
        execution_key=execution_key,
    )


def _input_policy_fallback(scenario_json: Mapping[str, Any]) -> InputValidationPolicy:
    raw = scenario_json.get("validation")
    validation = raw if isinstance(raw, Mapping) else {}
    return InputValidationPolicy(
        fail_if_source_missing_from_database=bool(
            validation.get("fail_if_source_missing_from_database", True)
        ),
        fail_if_daily_availability_missing=bool(
            validation.get("fail_if_daily_availability_missing", True)
        ),
        fail_if_required_quality_value_missing=bool(
            validation.get("fail_if_required_quality_value_missing", True)
        ),
        fail_if_demand_missing=bool(validation.get("fail_if_demand_missing", True)),
    )


def _loader_validation_fallback(scenario: ScenarioData) -> LoaderValidation:
    ready = scenario.is_ready
    return LoaderValidation(
        status="PASSED" if ready else "FAILED",
        scenario_ready=ready,
        validation_issues=tuple(scenario.validation_issues),
        checks=(
            ValidationCheck(
                check="loader_validation_issues_empty",
                enabled=True,
                passed=ready,
            ),
        ),
    )


def _preprocessing_validation_fallback(
    parameters: ModelParameters,
    options: PipelineOptions,
) -> PreprocessingValidation:
    return PreprocessingValidation(
        status="PASSED",
        warnings=tuple(parameters.warnings),
        checks=(
            ValidationCheck(
                check="model_parameters_constructed",
                enabled=True,
                passed=True,
            ),
            ValidationCheck(
                check="capacity_feasibility_check",
                enabled=options.validate_capacity_feasibility,
                passed=True,
            ),
            ValidationCheck(
                check="quality_feasibility_check",
                enabled=options.validate_quality_feasibility,
                passed=True,
            ),
        ),
    )


def _postprocessing_scenario_view(
    scenario: ScenarioData,
    scenario_json: Mapping[str, Any],
) -> _ScenarioPostprocessingView:
    input_policy = getattr(scenario, "input_policy_validation", None)
    if not isinstance(input_policy, InputValidationPolicy):
        input_policy = _input_policy_fallback(scenario_json)

    loader_validation = getattr(scenario, "loader_validation", None)
    if not isinstance(loader_validation, LoaderValidation):
        loader_validation = _loader_validation_fallback(scenario)

    return _ScenarioPostprocessingView(
        scenario,
        input_policy_validation=input_policy,
        loader_validation=loader_validation,
    )


def _postprocessing_parameters_view(
    parameters: ModelParameters,
    *,
    run_id: str,
    options: PipelineOptions,
) -> _ParametersPostprocessingView:
    preprocessing_validation = getattr(parameters, "preprocessing_validation", None)
    if not isinstance(preprocessing_validation, PreprocessingValidation):
        preprocessing_validation = _preprocessing_validation_fallback(
            parameters, options
        )
    return _ParametersPostprocessingView(
        parameters,
        run_id=run_id,
        preprocessing_validation=preprocessing_validation,
    )


def _model_has_loaded_values(model: Any) -> bool:
    for component_name in ("alpha", "beta", "a", "b", "c"):
        component = getattr(model, component_name, None)
        if component is None:
            continue
        try:
            for index in component:
                if getattr(component[index], "value", None) is not None:
                    return True
        except (KeyError, TypeError):
            continue
    return False


def _ensure_feasible_solution_loaded(model: Any, solver_results: Any) -> None:
    solver_info = getattr(solver_results, "solver", None)
    termination = getattr(solver_info, "termination_condition", None)
    if termination not in (
        TerminationCondition.optimal,
        TerminationCondition.feasible,
    ):
        return
    if _model_has_loaded_values(model):
        return
    try:
        model.solutions.load_from(solver_results)
    except Exception as exc:
        raise RuntimeError(
            "The solver reported a feasible solution, but variable values "
            "could not be loaded into the Pyomo model."
        ) from exc


def _data_source_from_json(scenario_json: Mapping[str, Any]) -> DataSource:
    raw = scenario_json.get("data_source")
    data_source = raw if isinstance(raw, Mapping) else {}
    return DataSource(
        type=str(data_source.get("type", "unknown")),
        view=str(data_source.get("view", "")),
        allow_estimated_values=bool(data_source.get("allow_estimated_values", False)),
    )


def _finalise_postprocessed_output(
    solved: SolvedScenario,
    *,
    run_id: str,
    scenario_json: Mapping[str, Any],
) -> SolvedScenario:
    return replace(
        solved,
        run_id=run_id,
        scenario=replace(
            solved.scenario,
            data_source=_data_source_from_json(scenario_json),
        ),
    )


def solve_prepared_input(
    prepared: PreparedMilpInput,
    *,
    options: PipelineOptions | None = None,
    run_id: str | None = None,
) -> SolvedScenario:
    """Solve one prepared input without performing any cache lookup."""

    options = options or PipelineOptions()
    resolved_run_id = (
        _clean_nonblank(run_id, "run_id")
        if run_id is not None
        else prepared.snapshot.run_external_id
    )

    try:
        model, solver_results = solve(prepared.parameters, tee=options.tee)
        _ensure_feasible_solution_loaded(model, solver_results)
    except Exception as exc:
        raise PipelineStageError(
            "SOLVING",
            f"{type(exc).__name__}: {exc}",
            run_id=resolved_run_id,
            scenario_id=prepared.snapshot.scenario_external_id,
            input_id=prepared.snapshot.id,
        ) from exc

    scenario_view = _postprocessing_scenario_view(
        prepared.scenario,
        prepared.snapshot.canonical_input_json,
    )
    parameters_view = _postprocessing_parameters_view(
        prepared.parameters,
        run_id=resolved_run_id,
        options=options,
    )

    try:
        solved = postprocess_solution(
            scenario_view,  # type: ignore[arg-type]
            parameters_view,  # type: ignore[arg-type]
            model,
            solver_results,
        )
    except PostprocessingError as exc:
        raise PipelineStageError(
            "POSTPROCESSING",
            str(exc),
            run_id=resolved_run_id,
            scenario_id=prepared.snapshot.scenario_external_id,
            input_id=prepared.snapshot.id,
        ) from exc
    except Exception as exc:
        raise PipelineStageError(
            "POSTPROCESSING",
            f"{type(exc).__name__}: {exc}",
            run_id=resolved_run_id,
            scenario_id=prepared.snapshot.scenario_external_id,
            input_id=prepared.snapshot.id,
        ) from exc

    return _finalise_postprocessed_output(
        solved,
        run_id=resolved_run_id,
        scenario_json=prepared.snapshot.canonical_input_json,
    )


def run_pipeline_for_input(
    input_id: str | UUID,
    *,
    options: PipelineOptions | None = None,
    run_id: str | None = None,
) -> SolvedScenario:
    """Convenience entry point that deliberately bypasses cache orchestration.

    Production workers should call prepare_milp_input(), perform cache lookup,
    then call solve_prepared_input() only on a miss or forced recomputation.
    """

    options = options or PipelineOptions()
    prepared = prepare_milp_input(input_id, options=options)
    return solve_prepared_input(prepared, options=options, run_id=run_id)


def run_pipeline(
    input_id: str | UUID,
    *,
    options: PipelineOptions | None = None,
    run_id: str | None = None,
) -> SolvedScenario:
    """Production convenience alias: milp_model_input UUID -> SolvedScenario.

    Unlike the historical version of this function, the identifier is now the
    immutable ``milp_model_input.id`` rather than ``Scenarios.ExternalId``.
    Cache-aware production workers should still use the two-phase
    prepare_milp_input()/solve_prepared_input() API.
    """

    return run_pipeline_for_input(input_id, options=options, run_id=run_id)


def solved_scenario_to_dict(solved: SolvedScenario) -> dict[str, Any]:
    normalised = _normalise_json_value(solved)
    if not isinstance(normalised, dict):
        raise PipelineError("SolvedScenario did not serialise to a JSON object.")
    return normalised


def solved_scenario_to_json(
    solved: SolvedScenario,
    *,
    indent: int | None = 2,
) -> str:
    return json.dumps(
        solved_scenario_to_dict(solved),
        indent=indent,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=False,
    )


def semantic_solved_output_hash(solved: SolvedScenario | Mapping[str, Any]) -> str:
    """Hash substantive output while excluding per-run/telemetry-only metadata.

    This lets a forced recomputation that produces the same mathematical result
    reuse the same downstream AI analysis even though run ID or solve timing differ.
    """

    raw = (
        solved_scenario_to_dict(solved)
        if not isinstance(solved, Mapping)
        else _normalise_json_value(dict(solved))
    )
    if not isinstance(raw, dict):
        raise PipelineError("Solved output must serialise to an object.")

    semantic = dict(raw)
    semantic.pop("run_id", None)

    solver = semantic.get("solver")
    if isinstance(solver, dict):
        solver = dict(solver)
        for key in (
            "solve_time_ms",
            "solver_time_ms",
            "solve_time_seconds",
            "solver_time_seconds",
        ):
            solver.pop(key, None)
        semantic["solver"] = solver

    for key in ("created_at", "started_at", "completed_at", "updated_at"):
        semantic.pop(key, None)

    return stable_hash(semantic)


def write_solved_scenario_json(
    solved: SolvedScenario,
    output_path: str | Path,
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = solved_scenario_to_json(solved, indent=2)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def _print_run_summary(solved: SolvedScenario) -> None:
    print(f"Run ID: {solved.run_id}")
    print(f"Scenario ID: {solved.scenario.scenario_id}")
    print(f"Solver status: {solved.solver.status}")
    print(f"Feasible: {solved.solver.is_feasible}")
    print(f"Optimal: {solved.solver.is_optimal}")
    print(f"Objective: {solved.solver.objective_value}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the AquaBlend MILP from an immutable milp_model_input snapshot."
    )
    parser.add_argument(
        "--input-id",
        required=True,
        help="UUID of public.milp_model_input.",
    )
    parser.add_argument("--run-id", help="Optional external Run ID override.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--tee", action="store_true")
    parser.add_argument("--skip-capacity-check", action="store_true")
    parser.add_argument("--skip-quality-check", action="store_true")
    args = parser.parse_args()

    options = PipelineOptions(
        tee=args.tee,
        validate_capacity_feasibility=not args.skip_capacity_check,
        validate_quality_feasibility=not args.skip_quality_check,
    )

    try:
        solved = run_pipeline_for_input(
            args.input_id,
            options=options,
            run_id=args.run_id,
        )
        _print_run_summary(solved)
        if args.output:
            write_solved_scenario_json(solved, args.output)
    except PipelineError as exc:
        print(f"AquaBlend pipeline failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
