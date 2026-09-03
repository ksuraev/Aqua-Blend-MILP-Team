"""Automatic Supabase run-queue worker for the AquaBlend MILP.

This module watches a database run table for new queued optimisation runs.
Each queued row is claimed atomically, exactly one worker executes it, and the
final SolvedScenario JSON is written back to that same row.

Database flow
-------------
Frontend / Backend
    -> INSERT optimisation run row with status QUEUED
    -> run_worker.py claims row atomically
    -> reads run_id + scenario_id
    -> pipeline.run_pipeline(scenario_id, run_id=run_id)
    -> pipeline fetches Scenarios.FormStateJson
    -> loader -> preprocessing -> HiGHS -> postprocessing
    -> worker writes SolvedScenario JSON to run row
    -> status COMPLETED

If the MILP itself concludes that the scenario is infeasible, that is still a
successfully executed run: the queue status is COMPLETED and the solver status
inside the stored SolvedScenario explains infeasibility.

The run-table schema is intentionally configurable because its exact column
names were not available when this worker was written.
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
from typing import Any

try:
    from .pipeline import (
        PipelineError,
        PipelineOptions,
        _database_url,
        _quote_identifier,
        _quote_relation,
        run_pipeline,
        solved_scenario_to_json,
    )
except ImportError:  # pragma: no cover
    from pipeline import (
        PipelineError,
        PipelineOptions,
        _database_url,
        _quote_identifier,
        _quote_relation,
        run_pipeline,
        solved_scenario_to_json,
    )


LOGGER = logging.getLogger("aquablend.milp.worker")

_DEFAULT_PENDING_STATUS = "QUEUED"
_DEFAULT_RUNNING_STATUS = "RUNNING"
_DEFAULT_COMPLETED_STATUS = "COMPLETED"
_DEFAULT_FAILED_STATUS = "FAILED"

_MAX_ERROR_LENGTH = 4000


class RunWorkerError(RuntimeError):
    """Raised when the run queue cannot be configured or updated safely."""


@dataclass(frozen=True, slots=True)
class RunQueueConfig:
    """Supabase/PostgreSQL queue schema.

    Required:
        table
        run_id_column
        scenario_id_column
        status_column
        result_json_column

    Optional:
        error_column
        created_at_column
        started_at_column
        completed_at_column
    """

    table: str
    run_id_column: str
    scenario_id_column: str
    status_column: str
    result_json_column: str

    error_column: str | None = None
    created_at_column: str | None = None
    started_at_column: str | None = None
    completed_at_column: str | None = None

    pending_status: str = _DEFAULT_PENDING_STATUS
    running_status: str = _DEFAULT_RUNNING_STATUS
    completed_status: str = _DEFAULT_COMPLETED_STATUS
    failed_status: str = _DEFAULT_FAILED_STATUS

    @classmethod
    def resolve(cls) -> "RunQueueConfig":
        """Resolve the actual run-table contract from environment variables."""

        required = {
            "table": os.getenv("AQUABLEND_RUN_TABLE"),
            "run_id_column": os.getenv("AQUABLEND_RUN_ID_COLUMN"),
            "scenario_id_column": os.getenv("AQUABLEND_RUN_SCENARIO_ID_COLUMN"),
            "status_column": os.getenv("AQUABLEND_RUN_STATUS_COLUMN"),
            "result_json_column": os.getenv("AQUABLEND_RUN_RESULT_JSON_COLUMN"),
        }

        missing = [name for name, value in required.items() if not value]
        if missing:
            env_names = {
                "table": "AQUABLEND_RUN_TABLE",
                "run_id_column": "AQUABLEND_RUN_ID_COLUMN",
                "scenario_id_column": "AQUABLEND_RUN_SCENARIO_ID_COLUMN",
                "status_column": "AQUABLEND_RUN_STATUS_COLUMN",
                "result_json_column": "AQUABLEND_RUN_RESULT_JSON_COLUMN",
            }
            missing_env = ", ".join(env_names[name] for name in missing)
            raise RunWorkerError(
                "Run queue is not fully configured. Missing: " + missing_env
            )

        config = cls(
            table=required["table"],  # type: ignore[arg-type]
            run_id_column=required["run_id_column"],  # type: ignore[arg-type]
            scenario_id_column=required["scenario_id_column"],  # type: ignore[arg-type]
            status_column=required["status_column"],  # type: ignore[arg-type]
            result_json_column=required["result_json_column"],  # type: ignore[arg-type]
            error_column=os.getenv("AQUABLEND_RUN_ERROR_COLUMN") or None,
            created_at_column=os.getenv("AQUABLEND_RUN_CREATED_AT_COLUMN") or None,
            started_at_column=os.getenv("AQUABLEND_RUN_STARTED_AT_COLUMN") or None,
            completed_at_column=os.getenv("AQUABLEND_RUN_COMPLETED_AT_COLUMN") or None,
            pending_status=os.getenv(
                "AQUABLEND_RUN_PENDING_STATUS", _DEFAULT_PENDING_STATUS
            ),
            running_status=os.getenv(
                "AQUABLEND_RUN_RUNNING_STATUS", _DEFAULT_RUNNING_STATUS
            ),
            completed_status=os.getenv(
                "AQUABLEND_RUN_COMPLETED_STATUS", _DEFAULT_COMPLETED_STATUS
            ),
            failed_status=os.getenv(
                "AQUABLEND_RUN_FAILED_STATUS", _DEFAULT_FAILED_STATUS
            ),
        )
        config._validate_identifiers()
        return config

    def _validate_identifiers(self) -> None:
        _quote_relation(self.table)

        columns = {
            "run ID column": self.run_id_column,
            "scenario ID column": self.scenario_id_column,
            "status column": self.status_column,
            "result JSON column": self.result_json_column,
            "error column": self.error_column,
            "created-at column": self.created_at_column,
            "started-at column": self.started_at_column,
            "completed-at column": self.completed_at_column,
        }

        for label, value in columns.items():
            if value:
                _quote_identifier(value, label)


@dataclass(frozen=True, slots=True)
class ClaimedRun:
    run_id: str
    scenario_id: str


def _connect() -> Any:
    try:
        import psycopg
    except ImportError as exc:
        raise RunWorkerError(
            'Missing dependency "psycopg". Install MILP requirements first.'
        ) from exc

    return psycopg.connect(_database_url(), connect_timeout=10)


def _qcol(value: str, label: str) -> str:
    return _quote_identifier(value, label)


def claim_next_run(config: RunQueueConfig) -> ClaimedRun | None:
    """Atomically claim one QUEUED row.

    PostgreSQL FOR UPDATE SKIP LOCKED makes this safe when multiple MILP workers
    are running: a queued run can be claimed by only one worker.
    """

    table = _quote_relation(config.table)
    run_id = _qcol(config.run_id_column, "run ID column")
    scenario_id = _qcol(config.scenario_id_column, "scenario ID column")
    status = _qcol(config.status_column, "status column")

    order_column = (
        _qcol(config.created_at_column, "created-at column")
        if config.created_at_column
        else run_id
    )

    set_parts = [f"{status} = %s"]
    params: list[Any] = [
        config.pending_status,
        config.running_status,
    ]

    if config.started_at_column:
        started_at = _qcol(config.started_at_column, "started-at column")
        set_parts.append(f"{started_at} = %s")
        params.append(datetime.now(timezone.utc))

    # Parameters are intentionally arranged as:
    # WHERE pending_status, then SET running_status / optional timestamp.
    # Reorder for the SQL below.
    pending = params.pop(0)
    set_params = params

    sql = f"""
        WITH next_run AS (
            SELECT {run_id}, {scenario_id}
            FROM {table}
            WHERE {status} = %s
            ORDER BY {order_column} ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        UPDATE {table} AS r
        SET {", ".join(set_parts)}
        FROM next_run AS n
        WHERE r.{run_id} = n.{run_id}
        RETURNING r.{run_id}, r.{scenario_id};
    """

    query_params = [pending, *set_params]

    try:
        with _connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, tuple(query_params))
                row = cursor.fetchone()
    except Exception as exc:
        raise RunWorkerError("Could not claim the next queued MILP run.") from exc

    if row is None:
        return None

    claimed_run_id = str(row[0]).strip()
    claimed_scenario_id = str(row[1]).strip()

    if not claimed_run_id or not claimed_scenario_id:
        raise RunWorkerError(
            "Claimed run is missing a valid run ID or scenario ID."
        )

    return ClaimedRun(
        run_id=claimed_run_id,
        scenario_id=claimed_scenario_id,
    )


def _complete_run(
    config: RunQueueConfig,
    *,
    run_id_value: str,
    result_json: str,
) -> None:
    """Persist a successful MILP result and mark the queue row COMPLETED."""

    table = _quote_relation(config.table)
    run_id = _qcol(config.run_id_column, "run ID column")
    status = _qcol(config.status_column, "status column")
    result = _qcol(config.result_json_column, "result JSON column")

    set_parts = [
        f"{status} = %s",
        f"{result} = %s::jsonb",
    ]
    params: list[Any] = [
        config.completed_status,
        result_json,
    ]

    if config.error_column:
        error = _qcol(config.error_column, "error column")
        set_parts.append(f"{error} = NULL")

    if config.completed_at_column:
        completed_at = _qcol(config.completed_at_column, "completed-at column")
        set_parts.append(f"{completed_at} = %s")
        params.append(datetime.now(timezone.utc))

    params.extend([run_id_value, config.running_status])

    sql = f"""
        UPDATE {table}
        SET {", ".join(set_parts)}
        WHERE {run_id} = %s
          AND {status} = %s;
    """

    try:
        with _connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, tuple(params))
                if cursor.rowcount != 1:
                    raise RunWorkerError(
                        f"Run {run_id_value!r} was not in the expected "
                        f"{config.running_status!r} state when completing it."
                    )
    except RunWorkerError:
        raise
    except Exception as exc:
        raise RunWorkerError(
            f"Could not persist result for run {run_id_value!r}."
        ) from exc


def _fail_run(
    config: RunQueueConfig,
    *,
    run_id_value: str,
    error_message: str,
) -> None:
    """Mark the run FAILED without exposing credentials or giant tracebacks."""

    table = _quote_relation(config.table)
    run_id = _qcol(config.run_id_column, "run ID column")
    status = _qcol(config.status_column, "status column")

    set_parts = [f"{status} = %s"]
    params: list[Any] = [config.failed_status]

    if config.error_column:
        error = _qcol(config.error_column, "error column")
        set_parts.append(f"{error} = %s")
        params.append(error_message[:_MAX_ERROR_LENGTH])

    if config.completed_at_column:
        completed_at = _qcol(config.completed_at_column, "completed-at column")
        set_parts.append(f"{completed_at} = %s")
        params.append(datetime.now(timezone.utc))

    params.extend([run_id_value, config.running_status])

    sql = f"""
        UPDATE {table}
        SET {", ".join(set_parts)}
        WHERE {run_id} = %s
          AND {status} = %s;
    """

    try:
        with _connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, tuple(params))
                if cursor.rowcount != 1:
                    LOGGER.error(
                        "Could not transition run %s from %s to %s.",
                        run_id_value,
                        config.running_status,
                        config.failed_status,
                    )
    except Exception:
        LOGGER.exception(
            "Could not persist FAILED state for run %s.", run_id_value
        )


def process_claimed_run(
    run: ClaimedRun,
    *,
    config: RunQueueConfig,
    pipeline_options: PipelineOptions | None = None,
) -> bool:
    """Execute one already-claimed run.

    Returns True if the worker completed the run, False if it failed.
    """

    LOGGER.info(
        "Starting MILP run %s for scenario %s.",
        run.run_id,
        run.scenario_id,
    )

    try:
        solved = run_pipeline(
            run.scenario_id,
            run_id=run.run_id,
            options=pipeline_options or PipelineOptions(),
        )

        result_json = solved_scenario_to_json(solved, indent=None)

        _complete_run(
            config,
            run_id_value=run.run_id,
            result_json=result_json,
        )

        LOGGER.info(
            "Completed MILP run %s (solver=%s, optimal=%s, feasible=%s).",
            run.run_id,
            solved.solver.status,
            solved.solver.is_optimal,
            solved.solver.is_feasible,
        )
        return True

    except Exception as exc:
        # Keep the database-facing error concise. Full traceback stays in worker
        # logs for developers.
        LOGGER.exception("MILP run %s failed.", run.run_id)

        if isinstance(exc, PipelineError):
            user_safe_message = str(exc)
        else:
            user_safe_message = (
                f"{type(exc).__name__}: MILP execution failed. "
                "See worker logs for details."
            )

        _fail_run(
            config,
            run_id_value=run.run_id,
            error_message=user_safe_message,
        )
        return False


def process_next_run(
    *,
    config: RunQueueConfig | None = None,
    pipeline_options: PipelineOptions | None = None,
) -> bool:
    """Claim and process at most one queued run.

    Returns False only when no queued run currently exists.
    """

    resolved_config = config or RunQueueConfig.resolve()
    run = claim_next_run(resolved_config)
    if run is None:
        return False

    process_claimed_run(
        run,
        config=resolved_config,
        pipeline_options=pipeline_options,
    )
    return True


def watch_run_queue(
    *,
    config: RunQueueConfig | None = None,
    pipeline_options: PipelineOptions | None = None,
    poll_seconds: float = 2.0,
) -> None:
    """Continuously process new queued rows until the worker is stopped."""

    if poll_seconds < 0.25:
        raise RunWorkerError("poll_seconds must be at least 0.25 seconds.")

    resolved_config = config or RunQueueConfig.resolve()
    stop_requested = False

    def request_stop(signum: int, frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True
        LOGGER.info("Shutdown requested; worker will stop safely.")

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    LOGGER.info(
        "AquaBlend MILP worker started. Watching %s for status=%s.",
        resolved_config.table,
        resolved_config.pending_status,
    )

    while not stop_requested:
        try:
            claimed = claim_next_run(resolved_config)
        except RunWorkerError:
            LOGGER.exception("Run-queue polling failed.")
            time.sleep(poll_seconds)
            continue

        if claimed is None:
            time.sleep(poll_seconds)
            continue

        process_claimed_run(
            claimed,
            config=resolved_config,
            pipeline_options=pipeline_options,
        )

    LOGGER.info("AquaBlend MILP worker stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Automatically process queued AquaBlend MILP runs."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process at most one queued run and exit.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=float(os.getenv("AQUABLEND_RUN_POLL_SECONDS", "2")),
        help="Queue polling interval for worker mode (default: 2 seconds).",
    )
    parser.add_argument(
        "--tee",
        action="store_true",
        help="Print HiGHS solver output.",
    )
    parser.add_argument(
        "--skip-capacity-check",
        action="store_true",
        help="Development only: skip preprocessing capacity pre-check.",
    )
    parser.add_argument(
        "--skip-quality-check",
        action="store_true",
        help="Development only: skip preprocessing quality pre-check.",
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
        config = RunQueueConfig.resolve()

        if args.once:
            found = process_next_run(
                config=config,
                pipeline_options=options,
            )
            if not found:
                LOGGER.info("No queued MILP runs found.")
            return

        watch_run_queue(
            config=config,
            pipeline_options=options,
            poll_seconds=args.poll_seconds,
        )

    except (RunWorkerError, PipelineError) as exc:
        print(f"AquaBlend worker failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
