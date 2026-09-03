"""AquaBlend automatic MILP worker wired to the live Supabase schema.

Live database contract
----------------------
public."Runs"
    "Id"              integer PK
    "ExternalId"      text          -> public MILP run ID (e.g. RUN-1001)
    "ScenarioId"      integer FK    -> public."Scenarios"."Id"
    "WorkflowStatus"  text
    "SolverOutcome"   text nullable
    "ProgressMessage" text nullable
    "ErrorCode"       text nullable
    "ErrorMessage"    text nullable
    "SolveTimeMs"     float nullable
    "ObjectiveValue"  numeric nullable
    "CreatedAt"       timestamptz
    "UpdatedAt"       timestamptz nullable

public."Scenarios"
    "Id"              integer PK
    "ExternalId"      text
    "FormStateJson"   jsonb

public."OptimisationResults"
    "Id"              integer PK
    "ScenarioId"      integer FK
    "Status"          text
    "SolvedAt"        timestamptz
    "ReceivedAt"      timestamptz
    "ContractVersion" text
    "ResultJson"      jsonb
    "TotalCost"       numeric nullable
    "Currency"        text nullable
    "CreatedAt"       timestamptz
    "UpdatedAt"       timestamptz nullable
    "RunId"           integer FK -> public."Runs"."Id"

Execution
---------
1. Find one run with WorkflowStatus='queued'.
2. Atomically claim it using FOR UPDATE SKIP LOCKED.
3. Set run -> running.
4. Resolve Runs.ScenarioId -> Scenarios.ExternalId.
5. Call pipeline.run_pipeline(scenario_external_id, run_id=Runs.ExternalId).
6. Store the complete SolvedScenario JSON in OptimisationResults.ResultJson.
7. Update Runs with outcome, objective and solve time.
8. Mark workflow completed.

A mathematically infeasible model is still a successfully executed run.  The
workflow becomes 'completed'; SolverOutcome/OptimisationResults.Status carry the
actual solver outcome (e.g. infeasible).

Python/database/runtime failures become WorkflowStatus='failed'.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

try:
    from .pipeline import (
        PipelineError,
        PipelineOptions,
        PipelineStageError,
        _database_url,
        run_pipeline,
        solved_scenario_to_json,
    )
except ImportError:  # pragma: no cover
    from pipeline import (
        PipelineError,
        PipelineOptions,
        PipelineStageError,
        _database_url,
        run_pipeline,
        solved_scenario_to_json,
    )


LOGGER = logging.getLogger("aquablend.milp.worker")

RUN_STATUS_QUEUED = "queued"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"

RESULT_CURRENCY = "AUD"
DEFAULT_POLL_SECONDS = 2.0
MAX_ERROR_MESSAGE_LENGTH = 4000


class RunWorkerError(RuntimeError):
    """Run-queue orchestration failure."""


@dataclass(frozen=True, slots=True)
class ClaimedRun:
    database_id: int
    external_id: str
    scenario_database_id: int
    scenario_external_id: str


def _connect() -> Any:
    try:
        import psycopg
    except ImportError as exc:
        raise RunWorkerError(
            'Missing dependency "psycopg". Install MILP requirements first.'
        ) from exc

    return psycopg.connect(_database_url(), connect_timeout=10)


def _clean_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RunWorkerError(f"{label} must be a non-empty string.")
    return value.strip()


def claim_next_run() -> ClaimedRun | None:
    """Atomically claim the oldest queued run.

    FOR UPDATE OF r SKIP LOCKED means multiple worker processes may safely run
    concurrently without solving the same run twice.
    """

    sql = """
        WITH next_run AS (
            SELECT
                r."Id" AS run_id,
                r."ExternalId" AS run_external_id,
                r."ScenarioId" AS scenario_id,
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
            "SolverOutcome" = NULL,
            "ProgressMessage" = %s,
            "ErrorCode" = NULL,
            "ErrorMessage" = NULL,
            "SolveTimeMs" = NULL,
            "ObjectiveValue" = NULL,
            "UpdatedAt" = now()
        FROM next_run AS n
        WHERE r."Id" = n.run_id
        RETURNING
            r."Id",
            r."ExternalId",
            r."ScenarioId",
            n.scenario_external_id;
    """

    try:
        with _connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql,
                    (
                        RUN_STATUS_QUEUED,
                        RUN_STATUS_RUNNING,
                        "MILP optimisation started.",
                    ),
                )
                row = cursor.fetchone()
    except Exception as exc:
        raise RunWorkerError("Could not claim the next queued run.") from exc

    if row is None:
        return None

    run_db_id = int(row[0])
    run_external_id = _clean_text(row[1], "Runs.ExternalId")
    scenario_db_id = int(row[2])
    scenario_external_id = _clean_text(
        row[3],
        "Scenarios.ExternalId",
    )

    return ClaimedRun(
        database_id=run_db_id,
        external_id=run_external_id,
        scenario_database_id=scenario_db_id,
        scenario_external_id=scenario_external_id,
    )


def _solver_status(solved: Any) -> str:
    value = getattr(getattr(solved, "solver", None), "status", None)
    if value is None:
        raise RunWorkerError("SolvedScenario is missing solver.status.")
    status = str(value).strip()
    if not status:
        raise RunWorkerError("SolvedScenario solver.status is blank.")
    return status


def _objective_value(solved: Any) -> Decimal | None:
    value = getattr(
        getattr(solved, "solver", None),
        "objective_value",
        None,
    )
    if value is None:
        return None

    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RunWorkerError(
            "SolvedScenario solver.objective_value is not numeric."
        ) from exc


def _contract_version(solved: Any) -> str:
    value = getattr(solved, "schema_version", None)
    if value is None:
        return "1.0"
    text = str(value).strip()
    return text or "1.0"


def _preferred_solve_time_ms(solved: Any, elapsed_ms: float) -> float:
    """Prefer solver-native timing if exposed; otherwise use worker wall time."""

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


def _upsert_optimisation_result(
    cursor: Any,
    *,
    run: ClaimedRun,
    solver_status: str,
    contract_version: str,
    result_json: str,
    total_cost: Decimal | None,
) -> None:
    """Update the result for this RunId, or insert it if none exists.

    OptimisationResults.RunId currently has no unique constraint, so ON CONFLICT
    cannot safely be used.  The run row is already exclusively claimed by one
    worker; locking any existing result row is sufficient here.
    """

    cursor.execute(
        """
        SELECT "Id"
        FROM public."OptimisationResults"
        WHERE "RunId" = %s
        ORDER BY "Id" ASC
        FOR UPDATE;
        """,
        (run.database_id,),
    )
    rows = cursor.fetchall()

    if len(rows) > 1:
        raise RunWorkerError(
            f"Run {run.external_id!r} has multiple OptimisationResults rows. "
            "Refusing to choose one silently."
        )

    now = datetime.now(timezone.utc)
    db_result_status = solver_status.upper()

    if rows:
        cursor.execute(
            """
            UPDATE public."OptimisationResults"
            SET
                "ScenarioId" = %s,
                "Status" = %s,
                "SolvedAt" = %s,
                "ReceivedAt" = %s,
                "ContractVersion" = %s,
                "ResultJson" = %s::jsonb,
                "TotalCost" = %s,
                "Currency" = %s,
                "UpdatedAt" = %s
            WHERE "Id" = %s;
            """,
            (
                run.scenario_database_id,
                db_result_status,
                now,
                now,
                contract_version,
                result_json,
                total_cost,
                RESULT_CURRENCY,
                now,
                int(rows[0][0]),
            ),
        )
        return

    cursor.execute(
        """
        INSERT INTO public."OptimisationResults" (
            "ScenarioId",
            "Status",
            "SolvedAt",
            "ReceivedAt",
            "ContractVersion",
            "ResultJson",
            "TotalCost",
            "Currency",
            "CreatedAt",
            "UpdatedAt",
            "RunId"
        )
        VALUES (
            %s, %s, %s, %s, %s,
            %s::jsonb, %s, %s, %s, %s, %s
        );
        """,
        (
            run.scenario_database_id,
            db_result_status,
            now,
            now,
            contract_version,
            result_json,
            total_cost,
            RESULT_CURRENCY,
            now,
            now,
            run.database_id,
        ),
    )


def complete_run(
    run: ClaimedRun,
    *,
    solved: Any,
    result_json: str,
    elapsed_ms: float,
) -> None:
    """Atomically persist OptimisationResults and complete the Runs row."""

    solver_status = _solver_status(solved)
    objective_value = _objective_value(solved)
    solve_time_ms = _preferred_solve_time_ms(solved, elapsed_ms)
    contract_version = _contract_version(solved)

    try:
        with _connect() as connection:
            with connection.cursor() as cursor:
                _upsert_optimisation_result(
                    cursor,
                    run=run,
                    solver_status=solver_status,
                    contract_version=contract_version,
                    result_json=result_json,
                    total_cost=objective_value,
                )

                cursor.execute(
                    """
                    UPDATE public."Runs"
                    SET
                        "WorkflowStatus" = %s,
                        "SolverOutcome" = %s,
                        "ProgressMessage" = %s,
                        "ErrorCode" = NULL,
                        "ErrorMessage" = NULL,
                        "SolveTimeMs" = %s,
                        "ObjectiveValue" = %s,
                        "UpdatedAt" = now()
                    WHERE "Id" = %s
                      AND lower("WorkflowStatus") = %s;
                    """,
                    (
                        RUN_STATUS_COMPLETED,
                        solver_status.lower(),
                        "MILP optimisation completed.",
                        solve_time_ms,
                        objective_value,
                        run.database_id,
                        RUN_STATUS_RUNNING,
                    ),
                )

                if cursor.rowcount != 1:
                    raise RunWorkerError(
                        f"Run {run.external_id!r} was no longer in the "
                        f"{RUN_STATUS_RUNNING!r} state during completion."
                    )
    except RunWorkerError:
        raise
    except Exception as exc:
        raise RunWorkerError(
            f"Could not persist completed run {run.external_id!r}."
        ) from exc


def fail_run(
    run: ClaimedRun,
    *,
    error_code: str,
    error_message: str,
    elapsed_ms: float,
) -> None:
    """Persist a worker/pipeline failure on the Runs row."""

    safe_code = (error_code or "MILP_EXECUTION_ERROR")[:200]
    safe_message = (
        error_message or "MILP execution failed."
    )[:MAX_ERROR_MESSAGE_LENGTH]

    try:
        with _connect() as connection:
            with connection.cursor() as cursor:
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
                        "UpdatedAt" = now()
                    WHERE "Id" = %s
                      AND lower("WorkflowStatus") = %s;
                    """,
                    (
                        RUN_STATUS_FAILED,
                        "MILP optimisation failed.",
                        safe_code,
                        safe_message,
                        max(0.0, elapsed_ms),
                        run.database_id,
                        RUN_STATUS_RUNNING,
                    ),
                )

                if cursor.rowcount != 1:
                    LOGGER.error(
                        "Could not transition run %s from running to failed.",
                        run.external_id,
                    )
    except Exception:
        LOGGER.exception(
            "Could not persist failed state for run %s.",
            run.external_id,
        )


def _failure_metadata(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, PipelineStageError):
        stage = getattr(exc, "stage", None)
        return (
            f"MILP_{str(stage or 'PIPELINE').upper()}_FAILED",
            str(exc),
        )

    if isinstance(exc, PipelineError):
        return ("MILP_PIPELINE_FAILED", str(exc))

    if isinstance(exc, RunWorkerError):
        return ("MILP_WORKER_FAILED", str(exc))

    return (
        "MILP_EXECUTION_ERROR",
        f"{type(exc).__name__}: MILP execution failed. See worker logs.",
    )


def process_claimed_run(
    run: ClaimedRun,
    *,
    options: PipelineOptions | None = None,
) -> bool:
    LOGGER.info(
        "Running %s for scenario %s.",
        run.external_id,
        run.scenario_external_id,
    )

    started = time.perf_counter()

    try:
        solved = run_pipeline(
            run.scenario_external_id,
            run_id=run.external_id,
            options=options or PipelineOptions(),
        )

        result_json = solved_scenario_to_json(solved, indent=None)
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        complete_run(
            run,
            solved=solved,
            result_json=result_json,
            elapsed_ms=elapsed_ms,
        )

        LOGGER.info(
            "Completed %s: solver=%s, feasible=%s, optimal=%s.",
            run.external_id,
            solved.solver.status,
            solved.solver.is_feasible,
            solved.solver.is_optimal,
        )
        return True

    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
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
    """Process at most one run. False means no queued run was present."""

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
        LOGGER.info("Shutdown requested; finishing safely.")

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    LOGGER.info(
        "AquaBlend MILP worker watching public.Runs for WorkflowStatus=%r.",
        RUN_STATUS_QUEUED,
    )

    while not stop_requested:
        try:
            run = claim_next_run()
        except RunWorkerError:
            LOGGER.exception("Could not poll the run queue.")
            time.sleep(poll_seconds)
            continue

        if run is None:
            time.sleep(poll_seconds)
            continue

        process_claimed_run(run, options=options)

    LOGGER.info("AquaBlend MILP worker stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Automatically execute queued AquaBlend MILP runs."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process at most one queued run and exit.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=float(
            os.getenv("AQUABLEND_RUN_POLL_SECONDS", str(DEFAULT_POLL_SECONDS))
        ),
        help="Polling interval while watching public.Runs.",
    )
    parser.add_argument(
        "--tee",
        action="store_true",
        help="Print HiGHS solver output.",
    )
    parser.add_argument(
        "--skip-capacity-check",
        action="store_true",
        help="Development only.",
    )
    parser.add_argument(
        "--skip-quality-check",
        action="store_true",
        help="Development only.",
    )
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
                LOGGER.info("No queued runs found.")
            return

        watch_run_queue(
            options=options,
            poll_seconds=args.poll_seconds,
        )
    except (RunWorkerError, PipelineError) as exc:
        print(f"AquaBlend worker failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
