"""Final AquaBlend MILP queue worker with deterministic cache reuse.

The worker owns orchestration; pipeline.py owns MILP stages.

Flow
----
queued Run
    -> claim with FOR UPDATE SKIP LOCKED
    -> preparing_input
    -> pipeline.prepare_milp_input()
    -> ModelParameters + execution_key
    -> cache lookup in public.milp_model_output
       -> HIT: attach existing output, no solver call
       -> MISS: milp_running -> solve -> persist new milp_model_output
    -> milp_completed

ForceRerun skips cache reuse but still records the same execution_key so future
normal runs may reuse the new output.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import signal
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import psycopg
from dotenv import load_dotenv

load_dotenv()

try:
    from .pipeline import (
        PipelineError,
        PipelineOptions,
        PipelineStageError,
        PreparedMilpInput,
        json_for_database,
        prepare_milp_input,
        semantic_solved_output_hash,
        solve_prepared_input,
        solved_scenario_to_dict,
    )
except ImportError:  # pragma: no cover
    from pipeline import (
        PipelineError,
        PipelineOptions,
        PipelineStageError,
        PreparedMilpInput,
        json_for_database,
        prepare_milp_input,
        semantic_solved_output_hash,
        solve_prepared_input,
        solved_scenario_to_dict,
    )


LOGGER = logging.getLogger("aquablend.milp.worker")

RUN_STATUS_QUEUED = "queued"
RUN_STATUS_PREPARING_INPUT = "preparing_input"
RUN_STATUS_MILP_RUNNING = "milp_running"
RUN_STATUS_MILP_COMPLETED = "milp_completed"
RUN_STATUS_FAILED = "failed"

DEFAULT_POLL_SECONDS = 2.0
MAX_ERROR_MESSAGE_LENGTH = 4000


class RunWorkerError(RuntimeError):
    """Run-queue/cache/result persistence failure."""


@dataclass(frozen=True, slots=True)
class ClaimedRun:
    database_id: int
    external_id: str
    scenario_database_id: int
    scenario_external_id: str
    milp_input_id: str | None
    force_rerun: bool


@dataclass(frozen=True, slots=True)
class CachedMilpOutput:
    id: str
    origin_run_id: int | None
    solver_status: str
    solver_is_feasible: bool | None
    solver_is_optimal: bool | None
    objective_value: Decimal | None
    solve_time_ms: float | None
    output_hash: str


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


def _clean_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RunWorkerError(f"{label} must be a non-empty string.")
    return value.strip()


def _as_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Mapping) and "__decimal__" in value:
        value = value["__decimal__"]
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise RunWorkerError(f"Expected numeric value, got {value!r}.") from exc


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise RunWorkerError(
            f"Expected float-compatible value, got {value!r}."
        ) from exc


def _path(value: Any, path: Sequence[str], default: Any = None) -> Any:
    current = value
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return default
        current = current[key]
    return current


def _first_path(value: Any, *paths: Sequence[str], default: Any = None) -> Any:
    sentinel = object()
    for path in paths:
        found = _path(value, path, sentinel)
        if found is not sentinel:
            return found
    return default


def _json_or_default(value: Any, default: Any) -> Any:
    return default if value is None else value


def claim_next_run() -> ClaimedRun | None:
    """Atomically claim the oldest queued run.

    The backend is expected to create Runs + milp_model_input in one transaction.
    A missing MilpInputId is therefore a contract failure, not something the
    worker reconstructs from Scenarios.FormStateJson.
    """

    sql = """
        WITH next_run AS (
            SELECT
                r."Id" AS run_id,
                r."ExternalId" AS run_external_id,
                r."ScenarioId" AS scenario_id,
                r."MilpInputId" AS milp_input_id,
                r."ForceRerun" AS force_rerun,
                s."ExternalId" AS scenario_external_id
            FROM public."Runs" AS r
            JOIN public."Scenarios" AS s
              ON s."Id" = r."ScenarioId"
            WHERE lower(r."WorkflowStatus") = %s
            ORDER BY r."CreatedAt" ASC, r."Id" ASC
            FOR UPDATE OF r SKIP LOCKED
            LIMIT 1
        )
        UPDATE public."Runs" AS r
        SET
            "WorkflowStatus" = %s,
            "ProgressMessage" = %s,
            "SolverOutcome" = NULL,
            "ErrorCode" = NULL,
            "ErrorMessage" = NULL,
            "SolveTimeMs" = NULL,
            "ObjectiveValue" = NULL,
            "MilpCacheHit" = false,
            "MilpOutputId" = NULL,
            "ReusedMilpFromRunId" = NULL,
            "MilpStartedAt" = NULL,
            "MilpCompletedAt" = NULL,
            "UpdatedAt" = now()
        FROM next_run AS n
        WHERE r."Id" = n.run_id
        RETURNING
            r."Id",
            r."ExternalId",
            r."ScenarioId",
            n.scenario_external_id,
            r."MilpInputId",
            r."ForceRerun";
    """

    try:
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                sql,
                (
                    RUN_STATUS_QUEUED,
                    RUN_STATUS_PREPARING_INPUT,
                    "Resolving immutable MILP input snapshot.",
                ),
            )
            row = cursor.fetchone()
    except Exception as exc:
        raise RunWorkerError(
            f"Could not claim the next queued Run: {type(exc).__name__}: {exc}"
        ) from exc

    if row is None:
        return None

    return ClaimedRun(
        database_id=int(row[0]),
        external_id=_clean_text(row[1], "Runs.ExternalId"),
        scenario_database_id=int(row[2]),
        scenario_external_id=_clean_text(row[3], "Scenarios.ExternalId"),
        milp_input_id=str(row[4]) if row[4] is not None else None,
        force_rerun=bool(row[5]),
    )


def _execution_lock_key(execution_key: str) -> int:
    digest = hashlib.sha256(execution_key.encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)


@contextmanager
def execution_key_lock(execution_key: str) -> Iterator[None]:
    """Prevent a cache stampede for the same effective MILP execution.

    A session-level PostgreSQL advisory lock is held only for normal cacheable
    execution. A second worker waiting on the same key re-checks the cache after
    the first worker finishes and therefore avoids a duplicate solver call.
    """

    key = _execution_lock_key(execution_key)
    connection = _connect()
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s);", (key,))
        yield
    finally:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s);", (key,))
        finally:
            connection.close()


def find_cached_output(execution_key: str) -> CachedMilpOutput | None:
    query = """
        SELECT
            id,
            origin_run_id,
            solver_status,
            solver_is_feasible,
            solver_is_optimal,
            solver_objective_value,
            solve_time_ms,
            output_hash
        FROM public.milp_model_output
        WHERE execution_key = %s
          AND cache_reusable = true
          AND output_hash IS NOT NULL
          AND solver_status <> 'NOT_SOLVED'
          AND raw_output_json <> '{}'::jsonb
        ORDER BY completed_at DESC NULLS LAST, created_at DESC
        LIMIT 1;
    """

    try:
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute(query, (execution_key,))
            row = cursor.fetchone()
    except Exception as exc:
        raise RunWorkerError("MILP cache lookup failed.") from exc

    if row is None:
        return None

    return CachedMilpOutput(
        id=str(row[0]),
        origin_run_id=int(row[1]) if row[1] is not None else None,
        solver_status=_clean_text(row[2], "milp_model_output.solver_status"),
        solver_is_feasible=bool(row[3]) if row[3] is not None else None,
        solver_is_optimal=bool(row[4]) if row[4] is not None else None,
        objective_value=_as_decimal(row[5]),
        solve_time_ms=_as_float(row[6]),
        output_hash=_clean_text(row[7], "milp_model_output.output_hash"),
    )


def attach_cached_output(run: ClaimedRun, cached: CachedMilpOutput) -> None:
    query = """
        UPDATE public."Runs"
        SET
            "WorkflowStatus" = %s,
            "SolverOutcome" = %s,
            "ProgressMessage" = %s,
            "ErrorCode" = NULL,
            "ErrorMessage" = NULL,
            "SolveTimeMs" = 0,
            "ObjectiveValue" = %s,
            "MilpCacheHit" = true,
            "MilpOutputId" = %s,
            "ReusedMilpFromRunId" = %s,
            "MilpStartedAt" = COALESCE("MilpStartedAt", now()),
            "MilpCompletedAt" = now(),
            "UpdatedAt" = now()
        WHERE "Id" = %s
          AND lower("WorkflowStatus") = %s;
    """

    try:
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                query,
                (
                    RUN_STATUS_MILP_COMPLETED,
                    cached.solver_status.lower(),
                    "MILP result reused from cache.",
                    cached.objective_value,
                    UUID(cached.id),
                    cached.origin_run_id,
                    run.database_id,
                    RUN_STATUS_PREPARING_INPUT,
                ),
            )
            if cursor.rowcount != 1:
                raise RunWorkerError(
                    f"Run {run.external_id!r} was not in preparing_input "
                    "while attaching a cached result."
                )
    except RunWorkerError:
        raise
    except Exception as exc:
        raise RunWorkerError("Could not attach cached MILP output to Run.") from exc


def mark_milp_running(run: ClaimedRun) -> None:
    query = """
        UPDATE public."Runs"
        SET
            "WorkflowStatus" = %s,
            "ProgressMessage" = %s,
            "MilpCacheHit" = false,
            "MilpStartedAt" = now(),
            "UpdatedAt" = now()
        WHERE "Id" = %s
          AND lower("WorkflowStatus") = %s;
    """
    try:
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                query,
                (
                    RUN_STATUS_MILP_RUNNING,
                    "Executing Pyomo/HiGHS optimisation.",
                    run.database_id,
                    RUN_STATUS_PREPARING_INPUT,
                ),
            )
            if cursor.rowcount != 1:
                raise RunWorkerError(
                    f"Run {run.external_id!r} was not in preparing_input."
                )
    except RunWorkerError:
        raise
    except Exception as exc:
        raise RunWorkerError("Could not mark Run as milp_running.") from exc


def _solver_status(solved: Any) -> str:
    value = getattr(getattr(solved, "solver", None), "status", None)
    return _clean_text(value, "SolvedScenario.solver.status")


def _solver_bool(solved: Any, name: str) -> bool | None:
    value = getattr(getattr(solved, "solver", None), name, None)
    return bool(value) if value is not None else None


def _objective_value(solved: Any) -> Decimal | None:
    value = getattr(getattr(solved, "solver", None), "objective_value", None)
    return _as_decimal(value)


def _preferred_solve_time_ms(solved: Any, elapsed_ms: float) -> float:
    solver = getattr(solved, "solver", None)
    if solver is not None:
        for attr in ("solve_time_ms", "solver_time_ms"):
            value = getattr(solver, attr, None)
            if value is not None:
                try:
                    return max(0.0, float(value))
                except (TypeError, ValueError):
                    pass
        for attr in ("solve_time_seconds", "solver_time_seconds"):
            value = getattr(solver, attr, None)
            if value is not None:
                try:
                    return max(0.0, float(value) * 1000.0)
                except (TypeError, ValueError):
                    pass
    return max(0.0, elapsed_ms)


def _numeric_from_paths(
    raw: Mapping[str, Any], *paths: Sequence[str]
) -> Decimal | None:
    value = _first_path(raw, *paths)
    return _as_decimal(value)


def _build_flat_output(
    raw: Mapping[str, Any],
    prepared: PreparedMilpInput,
    *,
    solve_time_ms: float,
    solved: Any,
) -> dict[str, Any]:
    canonical = prepared.snapshot.canonical_input_json
    data_source = canonical.get("data_source")
    if not isinstance(data_source, Mapping):
        data_source = {}
    validation_policy = canonical.get("validation")
    if not isinstance(validation_policy, Mapping):
        validation_policy = {}

    loader = _first_path(raw, ("validation", "loader"), default={})
    if not isinstance(loader, Mapping):
        loader = {}
    preprocessing = _first_path(raw, ("validation", "preprocessing"), default={})
    if not isinstance(preprocessing, Mapping):
        preprocessing = {}
    consistency = _first_path(raw, ("validation", "output_consistency"), default={})
    if not isinstance(consistency, Mapping):
        consistency = {}

    summary = raw.get("summary")
    if not isinstance(summary, Mapping):
        summary = {}

    solver = raw.get("solver")
    if not isinstance(solver, Mapping):
        solver = {}

    flows = raw.get("flows")
    if not isinstance(flows, Mapping):
        flows = {}

    return {
        "schema_version": str(raw.get("schema_version") or "1.0"),
        "scenario_id": prepared.snapshot.scenario_external_id,
        "scenario_status": prepared.snapshot.scenario_status,
        "data_source_type": data_source.get("type"),
        "data_source_view": data_source.get("view"),
        "allow_estimated_values": bool(
            data_source.get("allow_estimated_values", False)
        ),
        "fail_if_source_missing_from_database": bool(
            validation_policy.get("fail_if_source_missing_from_database", True)
        ),
        "fail_if_daily_availability_missing": bool(
            validation_policy.get("fail_if_daily_availability_missing", True)
        ),
        "fail_if_required_quality_value_missing": bool(
            validation_policy.get("fail_if_required_quality_value_missing", True)
        ),
        "fail_if_demand_missing": bool(
            validation_policy.get("fail_if_demand_missing", True)
        ),
        "loader_status": str(loader.get("status") or "PASSED"),
        "loader_scenario_ready": loader.get(
            "scenario_ready", getattr(prepared.scenario, "is_ready", True)
        ),
        "loader_validation_issues": _json_or_default(
            loader.get("validation_issues"), []
        ),
        "loader_checks": _json_or_default(loader.get("checks"), []),
        "preprocessing_status": str(preprocessing.get("status") or "PASSED"),
        "preprocessing_warnings": _json_or_default(
            preprocessing.get("warnings"),
            getattr(prepared.parameters, "warnings", []),
        ),
        "preprocessing_checks": _json_or_default(preprocessing.get("checks"), []),
        "output_consistency_status": str(consistency.get("status") or "NOT_RUN"),
        "output_consistency_tolerance": _as_decimal(consistency.get("tolerance")),
        "output_consistency_checks": _json_or_default(consistency.get("checks"), []),
        "solver_status": _solver_status(solved),
        "solver_is_feasible": _solver_bool(solved, "is_feasible"),
        "solver_is_optimal": _solver_bool(solved, "is_optimal"),
        "solver_objective_value": _objective_value(solved),
        "solver_version": (
            solver.get("version")
            or prepared.solver_config_json.get("solver_package_version")
        ),
        "solver_termination_condition": solver.get("termination_condition"),
        "solver_message": solver.get("message"),
        "solve_time_ms": solve_time_ms,
        "total_demand_ml_per_day": _numeric_from_paths(
            raw,
            ("summary", "total_demand_ml_per_day"),
            ("summary", "total_demand"),
        ),
        "total_withdrawal_ml_per_day": _numeric_from_paths(
            raw,
            ("summary", "total_withdrawal_ml_per_day"),
            ("summary", "total_withdrawal"),
        ),
        "total_treated_ml_per_day": _numeric_from_paths(
            raw,
            ("summary", "total_treated_ml_per_day"),
            ("summary", "total_treated"),
        ),
        "total_delivered_ml_per_day": _numeric_from_paths(
            raw,
            ("summary", "total_delivered_ml_per_day"),
            ("summary", "total_delivered"),
        ),
        "selected_source_count": _first_path(
            raw,
            ("summary", "selected_source_count"),
            default=None,
        ),
        "active_plant_count": _first_path(
            raw,
            ("summary", "active_plant_count"),
            default=None,
        ),
        "total_source_fixed_cost": _numeric_from_paths(
            raw,
            ("summary", "total_source_fixed_cost"),
            ("summary", "costs", "total_source_fixed_cost"),
            ("summary", "cost_breakdown", "total_source_fixed_cost"),
        ),
        "total_source_variable_cost": _numeric_from_paths(
            raw,
            ("summary", "total_source_variable_cost"),
            ("summary", "costs", "total_source_variable_cost"),
            ("summary", "cost_breakdown", "total_source_variable_cost"),
            ("summary", "cost_breakdown", "total_source_withdrawal_cost"),
        ),
        "total_plant_fixed_cost": _numeric_from_paths(
            raw,
            ("summary", "total_plant_fixed_cost"),
            ("summary", "costs", "total_plant_fixed_cost"),
            ("summary", "cost_breakdown", "total_plant_fixed_cost"),
        ),
        "total_plant_variable_cost": _numeric_from_paths(
            raw,
            ("summary", "total_plant_variable_cost"),
            ("summary", "costs", "total_plant_variable_cost"),
            ("summary", "cost_breakdown", "total_plant_variable_cost"),
            ("summary", "cost_breakdown", "total_plant_treatment_cost"),
        ),
        "reconstructed_total_cost": _numeric_from_paths(
            raw,
            ("summary", "reconstructed_total_cost"),
            ("summary", "costs", "reconstructed_total_cost"),
            ("summary", "cost_breakdown", "reconstructed_total_cost"),
        ),
        "total_cost": _numeric_from_paths(
            raw,
            ("summary", "total_cost"),
            ("summary", "costs", "total_cost"),
            ("solver", "objective_value"),
        ),
        "cost_reconciles": _first_path(
            raw,
            ("summary", "cost_reconciles"),
            ("summary", "costs", "cost_reconciles"),
            ("summary", "cost_breakdown", "cost_reconciles"),
            default=None,
        ),
        "sources": _json_or_default(raw.get("sources"), []),
        "plants": _json_or_default(raw.get("plants"), []),
        "demand_zones": _json_or_default(raw.get("demand_zones"), []),
        "flows_source_to_plant": _json_or_default(
            _first_path(
                raw,
                ("flows", "source_to_plant"),
                ("flows", "source_to_plant_flows"),
                ("flows_source_to_plant",),
                default=None,
            ),
            [],
        ),
        "flows_plant_to_zone": _json_or_default(
            _first_path(
                raw,
                ("flows", "plant_to_zone"),
                ("flows", "plant_to_zone_flows"),
                ("flows_plant_to_zone",),
                default=None,
            ),
            [],
        ),
        "quality": _json_or_default(raw.get("quality"), {}),
        "binding_constraints_summary": _json_or_default(
            raw.get("binding_constraints_summary"), []
        ),
        "warnings": _json_or_default(raw.get("warnings"), []),
    }


def persist_solved_output(
    run: ClaimedRun,
    prepared: PreparedMilpInput,
    solved: Any,
    *,
    elapsed_ms: float,
) -> str:
    """Insert authoritative milp_model_output and attach it to the current Run."""

    raw = solved_scenario_to_dict(solved)
    output_hash = semantic_solved_output_hash(raw)
    solve_time_ms = _preferred_solve_time_ms(solved, elapsed_ms)
    flat = _build_flat_output(
        raw,
        prepared,
        solve_time_ms=solve_time_ms,
        solved=solved,
    )

    now = datetime.now(timezone.utc)

    insert_sql = """
        INSERT INTO public.milp_model_output (
            schema_version,
            scenario_id,
            scenario_status,
            data_source_type,
            data_source_view,
            allow_estimated_values,
            fail_if_source_missing_from_database,
            fail_if_daily_availability_missing,
            fail_if_required_quality_value_missing,
            fail_if_demand_missing,
            loader_status,
            loader_scenario_ready,
            loader_validation_issues,
            loader_checks,
            preprocessing_status,
            preprocessing_warnings,
            preprocessing_checks,
            output_consistency_status,
            output_consistency_tolerance,
            output_consistency_checks,
            solver_status,
            solver_is_feasible,
            solver_is_optimal,
            solver_objective_value,
            solver_version,
            total_demand_ml_per_day,
            total_withdrawal_ml_per_day,
            total_treated_ml_per_day,
            total_delivered_ml_per_day,
            selected_source_count,
            active_plant_count,
            total_source_fixed_cost,
            total_source_variable_cost,
            total_plant_fixed_cost,
            total_plant_variable_cost,
            reconstructed_total_cost,
            total_cost,
            cost_reconciles,
            sources,
            plants,
            demand_zones,
            flows_source_to_plant,
            flows_plant_to_zone,
            quality,
            binding_constraints_summary,
            warnings,
            origin_run_id,
            input_id,
            scenario_db_id,
            execution_key,
            input_hash,
            model_parameters_hash,
            milp_model_version,
            solver_name,
            solver_config_hash,
            solver_termination_condition,
            solver_message,
            solve_time_ms,
            raw_output_json,
            output_hash,
            cache_reusable,
            forced_recompute,
            started_at,
            completed_at,
            updated_at
        ) VALUES (
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
            %s,%s,%s::jsonb,%s::jsonb,%s,%s::jsonb,%s::jsonb,%s,%s,%s::jsonb,
            %s,%s,%s,%s,%s,
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
            %s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s
        )
        RETURNING id;
    """

    values = (
        flat["schema_version"],
        flat["scenario_id"],
        flat["scenario_status"],
        flat["data_source_type"],
        flat["data_source_view"],
        flat["allow_estimated_values"],
        flat["fail_if_source_missing_from_database"],
        flat["fail_if_daily_availability_missing"],
        flat["fail_if_required_quality_value_missing"],
        flat["fail_if_demand_missing"],
        flat["loader_status"],
        flat["loader_scenario_ready"],
        json_for_database(flat["loader_validation_issues"]),
        json_for_database(flat["loader_checks"]),
        flat["preprocessing_status"],
        json_for_database(flat["preprocessing_warnings"]),
        json_for_database(flat["preprocessing_checks"]),
        flat["output_consistency_status"],
        flat["output_consistency_tolerance"],
        json_for_database(flat["output_consistency_checks"]),
        flat["solver_status"],
        flat["solver_is_feasible"],
        flat["solver_is_optimal"],
        flat["solver_objective_value"],
        flat["solver_version"],
        flat["total_demand_ml_per_day"],
        flat["total_withdrawal_ml_per_day"],
        flat["total_treated_ml_per_day"],
        flat["total_delivered_ml_per_day"],
        flat["selected_source_count"],
        flat["active_plant_count"],
        flat["total_source_fixed_cost"],
        flat["total_source_variable_cost"],
        flat["total_plant_fixed_cost"],
        flat["total_plant_variable_cost"],
        flat["reconstructed_total_cost"],
        flat["total_cost"],
        flat["cost_reconciles"],
        json_for_database(flat["sources"]),
        json_for_database(flat["plants"]),
        json_for_database(flat["demand_zones"]),
        json_for_database(flat["flows_source_to_plant"]),
        json_for_database(flat["flows_plant_to_zone"]),
        json_for_database(flat["quality"]),
        json_for_database(flat["binding_constraints_summary"]),
        json_for_database(flat["warnings"]),
        run.database_id,
        UUID(prepared.snapshot.id),
        run.scenario_database_id,
        prepared.execution_key,
        prepared.input_hash,
        prepared.model_parameters_hash,
        prepared.milp_model_version,
        prepared.solver_config_json.get("solver_name", "appsi_highs"),
        prepared.solver_config_hash,
        flat["solver_termination_condition"],
        flat["solver_message"],
        flat["solve_time_ms"],
        json_for_database(raw),
        output_hash,
        True,
        run.force_rerun,
        now,
        now,
        now,
    )

    update_sql = """
        UPDATE public."Runs"
        SET
            "WorkflowStatus" = %s,
            "SolverOutcome" = %s,
            "ProgressMessage" = %s,
            "ErrorCode" = NULL,
            "ErrorMessage" = NULL,
            "SolveTimeMs" = %s,
            "ObjectiveValue" = %s,
            "MilpCacheHit" = false,
            "MilpOutputId" = %s,
            "ReusedMilpFromRunId" = NULL,
            "MilpCompletedAt" = now(),
            "UpdatedAt" = now()
        WHERE "Id" = %s
          AND lower("WorkflowStatus") = %s;
    """

    try:
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute(insert_sql, values)
            output_row = cursor.fetchone()
            if output_row is None:
                raise RunWorkerError("milp_model_output INSERT returned no id.")
            output_id = str(output_row[0])

            cursor.execute(
                update_sql,
                (
                    RUN_STATUS_MILP_COMPLETED,
                    flat["solver_status"].lower(),
                    "MILP optimisation completed; result ready for AI analysis.",
                    flat["solve_time_ms"],
                    flat["solver_objective_value"],
                    UUID(output_id),
                    run.database_id,
                    RUN_STATUS_MILP_RUNNING,
                ),
            )
            if cursor.rowcount != 1:
                raise RunWorkerError(
                    f"Run {run.external_id!r} was not in milp_running "
                    "during completion."
                )
        return output_id
    except RunWorkerError:
        raise
    except Exception as exc:
        raise RunWorkerError("Could not persist MILP output atomically.") from exc


def fail_run(
    run: ClaimedRun,
    *,
    error_code: str,
    error_message: str,
    elapsed_ms: float,
) -> None:
    safe_code = (error_code or "MILP_EXECUTION_ERROR")[:200]
    safe_message = (error_message or "MILP execution failed.")[
        :MAX_ERROR_MESSAGE_LENGTH
    ]

    try:
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                    UPDATE public."Runs"
                    SET
                        "WorkflowStatus" = %s,
                        "SolverOutcome" = NULL,
                        "ProgressMessage" = %s,
                        "ErrorCode" = %s,
                        "ErrorMessage" = %s,
                        "SolveTimeMs" = %s,
                        "ObjectiveValue" = NULL,
                        "MilpCacheHit" = false,
                        "MilpCompletedAt" = now(),
                        "UpdatedAt" = now()
                    WHERE "Id" = %s
                      AND lower("WorkflowStatus") IN (%s, %s);
                    """,
                (
                    RUN_STATUS_FAILED,
                    "MILP execution failed.",
                    safe_code,
                    safe_message,
                    max(0.0, elapsed_ms),
                    run.database_id,
                    RUN_STATUS_PREPARING_INPUT,
                    RUN_STATUS_MILP_RUNNING,
                ),
            )
    except Exception:
        LOGGER.exception("Could not persist failed state for %s.", run.external_id)


def _failure_metadata(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, PipelineStageError):
        stage = getattr(exc, "stage", None)
        return (f"MILP_{str(stage or 'PIPELINE').upper()}_FAILED", str(exc))
    if isinstance(exc, PipelineError):
        return ("MILP_PIPELINE_FAILED", str(exc))
    if isinstance(exc, RunWorkerError):
        return ("MILP_WORKER_FAILED", str(exc))
    return (
        "MILP_EXECUTION_ERROR",
        f"{type(exc).__name__}: MILP execution failed. See worker logs.",
    )


def _solve_and_store(
    run: ClaimedRun,
    prepared: PreparedMilpInput,
    *,
    options: PipelineOptions,
) -> str:
    mark_milp_running(run)
    started = time.perf_counter()
    solved = solve_prepared_input(
        prepared,
        options=options,
        run_id=run.external_id,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return persist_solved_output(
        run,
        prepared,
        solved,
        elapsed_ms=elapsed_ms,
    )


def process_claimed_run(
    run: ClaimedRun,
    *,
    options: PipelineOptions | None = None,
) -> bool:
    options = options or PipelineOptions()
    overall_started = time.perf_counter()

    LOGGER.info(
        "Preparing run %s (scenario=%s, input=%s, force=%s).",
        run.external_id,
        run.scenario_external_id,
        run.milp_input_id,
        run.force_rerun,
    )

    try:
        if run.milp_input_id is None:
            raise RunWorkerError(
                f"Run {run.external_id!r} has no MilpInputId. Backend run "
                "creation must commit Runs and milp_model_input atomically."
            )

        prepared = prepare_milp_input(run.milp_input_id, options=options)

        if prepared.snapshot.run_database_id != run.database_id:
            raise RunWorkerError(
                "milp_model_input.run_id does not match the claimed Run."
            )
        if prepared.snapshot.scenario_database_id != run.scenario_database_id:
            raise RunWorkerError(
                "milp_model_input.scenario_id does not match the claimed Run."
            )
        if prepared.snapshot.run_external_id != run.external_id:
            raise RunWorkerError(
                "milp_model_input.run_external_id does not match the claimed Run."
            )

        if run.force_rerun:
            output_id = _solve_and_store(run, prepared, options=options)
            LOGGER.info("Force-rerun %s solved; output=%s.", run.external_id, output_id)
            return True

        # Fast path before obtaining the advisory lock.
        cached = find_cached_output(prepared.execution_key)
        if cached is not None:
            attach_cached_output(run, cached)
            LOGGER.info(
                "Cache hit for %s; reused output=%s from run=%s.",
                run.external_id,
                cached.id,
                cached.origin_run_id,
            )
            return True

        # Prevent two workers from simultaneously solving the same cache miss.
        with execution_key_lock(prepared.execution_key):
            cached = find_cached_output(prepared.execution_key)
            if cached is not None:
                attach_cached_output(run, cached)
                LOGGER.info(
                    "Cache hit after lock for %s; reused output=%s.",
                    run.external_id,
                    cached.id,
                )
                return True

            output_id = _solve_and_store(run, prepared, options=options)
            LOGGER.info("Solved %s; output=%s.", run.external_id, output_id)
            return True

    except Exception as exc:
        elapsed_ms = (time.perf_counter() - overall_started) * 1000.0
        LOGGER.exception("Run %s failed.", run.external_id)
        code, message = _failure_metadata(exc)
        fail_run(
            run,
            error_code=code,
            error_message=message,
            elapsed_ms=elapsed_ms,
        )
        return False


def process_next_run(
    *,
    options: PipelineOptions | None = None,
) -> bool:
    """Process at most one queued run; False means queue was empty."""

    run = claim_next_run()
    if run is None:
        return False
    process_claimed_run(run, options=options)
    return True


def watch_run_queue(
    *,
    options: PipelineOptions | None = None,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> None:
    if poll_seconds < 0.25:
        raise RunWorkerError("poll_seconds must be at least 0.25 seconds.")

    stop_requested = False

    def request_stop(signum: int, frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True
        LOGGER.info("Shutdown requested; current work will finish safely.")

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    LOGGER.info("Watching public.Runs for WorkflowStatus=%r.", RUN_STATUS_QUEUED)

    while not stop_requested:
        try:
            found = process_next_run(options=options)
        except RunWorkerError:
            LOGGER.exception("Queue polling/claim failed.")
            time.sleep(poll_seconds)
            continue

        if not found:
            time.sleep(poll_seconds)

    LOGGER.info("AquaBlend MILP worker stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Process queued AquaBlend MILP runs with cache reuse."
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=float(
            os.getenv("AQUABLEND_RUN_POLL_SECONDS", str(DEFAULT_POLL_SECONDS))
        ),
    )
    parser.add_argument("--tee", action="store_true")
    parser.add_argument("--skip-capacity-check", action="store_true")
    parser.add_argument("--skip-quality-check", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=os.getenv("AQUABLEND_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    options = PipelineOptions(
        validate_capacity_feasibility=not args.skip_capacity_check,
        validate_quality_feasibility=not args.skip_quality_check,
        tee=args.tee,
    )

    try:
        if args.once:
            found = process_next_run(options=options)
            if not found:
                LOGGER.info("No queued Runs found.")
            return

        watch_run_queue(options=options, poll_seconds=args.poll_seconds)
    except (RunWorkerError, PipelineError) as exc:
        print(f"AquaBlend worker failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
