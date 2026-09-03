"""Canonical production pipeline for the AquaBlend MILP.

The pipeline contains orchestration only.  It does not implement optimisation
mathematics.

Production flow
---------------
scenario_id
    -> fetch raw scenario JSON/JSONB from Supabase
    -> data_loader.load_scenario()
    -> preprocessing.preprocess_scenario()
    -> model.solve()
    -> postprocessing.postprocess_solution()
    -> SolvedScenario

Notes
-----
1. The existing data loader currently accepts a JSON *path*, not a mapping.
   Supabase scenario JSON is therefore materialised to a temporary file and then
   passed through the existing loader.  This preserves all current loader logic
   instead of duplicating it here.

2. The draft postprocessing implementation currently expects validation/run
   metadata that ScenarioData and ModelParameters do not yet carry.  Small
   compatibility views below provide those fields only when they are missing.
   If the merged upstream contracts later expose the metadata directly, the
   real metadata is preferred automatically.

3. The current HiGHS solver PR loads variable values only for an optimal
   termination.  Postprocessing also supports a feasible non-optimal result, so
   this module defensively loads a returned feasible incumbent when values are
   not already present.  This compatibility guard can be removed once solve()
   handles all feasible incumbents itself.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

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
except ImportError:  # pragma: no cover - supports direct script execution.
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
    from postprocessing import PostprocessingError, postprocess_solution
    from preprocessing import ModelParameters, PreprocessingError, preprocess_scenario


_DEFAULT_SCENARIO_ID_COLUMN = "scenario_id"

_RELATION_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$"
)
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PipelineError(RuntimeError):
    """Base exception for AquaBlend pipeline execution failures."""


class ScenarioFetchError(PipelineError):
    """Raised when raw scenario JSON cannot be retrieved safely."""


class PipelineStageError(PipelineError):
    """Failure raised at a named stage of one optimisation run."""

    def __init__(
        self,
        stage: str,
        message: str,
        *,
        scenario_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        super().__init__(f"{stage}: {message}")
        self.stage = stage
        self.scenario_id = scenario_id
        self.run_id = run_id


@dataclass(frozen=True, slots=True)
class PipelineOptions:
    """MILP execution controls.

    Pre-solver feasibility checks remain enabled by default.  Disabling them is
    intended for development/diagnostics only; the mathematical model still
    enforces its own constraints.
    """

    validate_capacity_feasibility: bool = True
    validate_quality_feasibility: bool = True
    tee: bool = False


@dataclass(frozen=True, slots=True)
class ScenarioStoreConfig:
    """Location of the raw scenario JSON/JSONB in Supabase/PostgreSQL."""

    table: str
    id_column: str
    json_column: str

    @classmethod
    def resolve(
        cls,
        *,
        table: str | None = None,
        id_column: str | None = None,
        json_column: str | None = None,
    ) -> "ScenarioStoreConfig":
        _load_environment()

        resolved_table = table or os.getenv("AQUABLEND_SCENARIO_TABLE")
        resolved_id_column = (
            id_column
            or os.getenv("AQUABLEND_SCENARIO_ID_COLUMN")
            or _DEFAULT_SCENARIO_ID_COLUMN
        )
        resolved_json_column = (
            json_column or os.getenv("AQUABLEND_SCENARIO_JSON_COLUMN")
        )

        if not resolved_table:
            raise ScenarioFetchError(
                "The Supabase scenario table is not configured. Set "
                "AQUABLEND_SCENARIO_TABLE or pass table= explicitly."
            )
        if not resolved_json_column:
            raise ScenarioFetchError(
                "The Supabase scenario JSON/JSONB column is not configured. Set "
                "AQUABLEND_SCENARIO_JSON_COLUMN or pass json_column= explicitly."
            )

        # Validate identifiers now, before any SQL is constructed.
        _quote_relation(resolved_table)
        _quote_identifier(resolved_id_column, "scenario ID column")
        _quote_identifier(resolved_json_column, "scenario JSON column")

        return cls(
            table=resolved_table,
            id_column=resolved_id_column,
            json_column=resolved_json_column,
        )


class _ScenarioPostprocessingView:
    """Compatibility view for the current draft postprocessor."""

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
    """Compatibility view that keeps orchestration state out of ModelParameters."""

    __slots__ = ("_parameters", "run_id", "preprocessing_validation")

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
    """Load the MILP .env before reading any environment-backed configuration."""

    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise ScenarioFetchError(
            'Missing dependency "python-dotenv". Install MILP requirements first.'
        ) from exc

    load_dotenv()


def _database_url() -> str:
    """Return the single database connection string used by the whole MILP run."""

    _load_environment()
    value = os.getenv("DATABASE_URL")
    if not value:
        raise ScenarioFetchError(
            "DATABASE_URL is missing. Add the Supabase PostgreSQL connection "
            "string to the local .env file."
        )
    return value


def _clean_nonblank(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise PipelineError(f"{field_name} must be a string.")
    cleaned = value.strip()
    if not cleaned:
        raise PipelineError(f"{field_name} must not be blank.")
    return cleaned


def _quote_relation(name: str) -> str:
    if not _RELATION_PATTERN.fullmatch(name):
        raise ScenarioFetchError(f"Unsafe scenario table name: {name!r}")
    return ".".join(f'"{part}"' for part in name.split("."))


def _quote_identifier(name: str, label: str) -> str:
    if not _IDENTIFIER_PATTERN.fullmatch(name):
        raise ScenarioFetchError(f"Unsafe {label}: {name!r}")
    return f'"{name}"'


def _new_run_id(scenario_id: str) -> str:
    """Create a collision-resistant run identifier without database coupling."""

    return f"{scenario_id}-{uuid4().hex}"


def _normalise_scenario_document(
    value: Any,
    requested_scenario_id: str,
) -> dict[str, Any]:
    """Normalise one JSON/JSONB database value into a scenario dictionary."""

    if isinstance(value, Mapping):
        document = dict(value)
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ScenarioFetchError(
                f"Stored scenario {requested_scenario_id!r} is not valid JSON."
            ) from exc
        if not isinstance(parsed, dict):
            raise ScenarioFetchError(
                f"Stored scenario {requested_scenario_id!r} must be a JSON object."
            )
        document = parsed
    else:
        raise ScenarioFetchError(
            f"Stored scenario {requested_scenario_id!r} must be JSON/JSONB, "
            f"got {type(value).__name__}."
        )

    stored_id_raw = document.get("scenario_id")
    if stored_id_raw is None:
        document["scenario_id"] = requested_scenario_id
    else:
        stored_id = _clean_nonblank(stored_id_raw, "scenario_id")
        if stored_id != requested_scenario_id:
            raise ScenarioFetchError(
                "Scenario ID mismatch: requested "
                f"{requested_scenario_id!r}, but stored JSON contains "
                f"{stored_id!r}."
            )

    # Prove that the retrieved document is standards-compliant JSON now rather
    # than allowing NaN/Infinity or an unserialisable object to fail later.
    try:
        json.dumps(document, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ScenarioFetchError(
            f"Stored scenario {requested_scenario_id!r} is not JSON-safe."
        ) from exc

    return document


def fetch_scenario_json(
    scenario_id: str,
    *,
    table: str | None = None,
    id_column: str | None = None,
    json_column: str | None = None,
) -> dict[str, Any]:
    """Fetch one raw scenario JSON/JSONB document from Supabase/PostgreSQL.

    The same DATABASE_URL environment variable used by the existing data loader
    is used here.  This prevents a run from accidentally fetching its scenario
    from one database and source rows from another.
    """

    scenario_id = _clean_nonblank(scenario_id, "scenario_id")
    store = ScenarioStoreConfig.resolve(
        table=table,
        id_column=id_column,
        json_column=json_column,
    )

    relation = _quote_relation(store.table)
    id_identifier = _quote_identifier(store.id_column, "scenario ID column")
    json_identifier = _quote_identifier(store.json_column, "scenario JSON column")

    query = f"""
        SELECT {json_identifier}
        FROM {relation}
        WHERE {id_identifier} = %s
        LIMIT 2;
    """

    try:
        import psycopg
    except ImportError as exc:
        raise ScenarioFetchError(
            'Missing dependency "psycopg". Install MILP requirements first.'
        ) from exc

    try:
        with psycopg.connect(
            _database_url(),
            connect_timeout=10,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, (scenario_id,))
                rows = cursor.fetchall()
    except ScenarioFetchError:
        raise
    except Exception as exc:
        # Keep database credentials/connection details out of a message that may
        # later be surfaced by an API.  The chained exception is still available
        # to internal logs/debuggers.
        raise ScenarioFetchError(
            "Could not fetch the scenario JSON from Supabase."
        ) from exc

    if not rows:
        raise ScenarioFetchError(
            f"No scenario row was found for scenario_id={scenario_id!r}."
        )
    if len(rows) > 1:
        raise ScenarioFetchError(
            f"More than one row matched scenario_id={scenario_id!r}; "
            "scenario IDs must be unique."
        )

    return _normalise_scenario_document(rows[0][0], scenario_id)


def _load_scenario_from_mapping(scenario_json: Mapping[str, Any]) -> ScenarioData:
    """Run an in-memory/Supabase document through the existing path-based loader."""

    document = dict(scenario_json)

    try:
        encoded = json.dumps(
            document,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise DataLoadError("Scenario document is not JSON-safe.") from exc

    with tempfile.TemporaryDirectory(prefix="aquablend_scenario_") as temp_dir:
        path = Path(temp_dir) / "scenario.json"
        path.write_text(encoded, encoding="utf-8")
        # Strict loading is mandatory in the canonical pipeline.  Preprocessing
        # itself refuses ScenarioData that still contains validation issues.
        return load_scenario(path, strict=True)


def _input_policy_fallback(
    scenario_json: Mapping[str, Any],
) -> InputValidationPolicy:
    """Mirror the loader's four policy defaults when upstream does not pass them."""

    raw_validation = scenario_json.get("validation")
    validation = (
        raw_validation if isinstance(raw_validation, Mapping) else {}
    )

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
        fail_if_demand_missing=bool(
            validation.get("fail_if_demand_missing", True)
        ),
    )


def _loader_validation_fallback(scenario: ScenarioData) -> LoaderValidation:
    """Create the minimum truthful loader summary for the draft output contract."""

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
    """Summarise checks that must have succeeded for preprocess_scenario to return."""

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
    """Prefer real upstream validation metadata; fall back only when absent."""

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
    """Prefer real upstream preprocessing metadata; supply run_id separately."""

    preprocessing_validation = getattr(
        parameters,
        "preprocessing_validation",
        None,
    )
    if not isinstance(preprocessing_validation, PreprocessingValidation):
        preprocessing_validation = _preprocessing_validation_fallback(
            parameters,
            options,
        )

    return _ParametersPostprocessingView(
        parameters,
        run_id=run_id,
        preprocessing_validation=preprocessing_validation,
    )


def _model_has_loaded_values(model: Any) -> bool:
    """Return True when at least one canonical Pyomo decision variable has a value."""

    for component_name in ("alpha", "beta", "a", "b", "c"):
        component = getattr(model, component_name, None)
        if component is None:
            continue

        try:
            for index in component:
                variable = component[index]
                if getattr(variable, "value", None) is not None:
                    return True
        except (KeyError, TypeError):
            continue

    return False


def _ensure_feasible_solution_loaded(model: Any, solver_results: Any) -> None:
    """Load a feasible incumbent if solve() returned it without loading values.

    Current solver PR behaviour:
      - optimal -> values loaded
      - feasible but non-optimal -> values not loaded

    Current postprocessing behaviour:
      - optimal -> supported
      - feasible -> also considered a usable solution

    Therefore the pipeline bridges the mismatch until solve() is updated.
    """

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
            "The solver reported a feasible solution, but its variable values "
            "could not be loaded into the Pyomo model."
        ) from exc


def _data_source_from_json(scenario_json: Mapping[str, Any]) -> DataSource:
    raw_data_source = scenario_json.get("data_source")
    data_source = (
        raw_data_source if isinstance(raw_data_source, Mapping) else {}
    )

    return DataSource(
        type=str(data_source.get("type", "unknown")),
        view=str(data_source.get("view", "")),
        allow_estimated_values=bool(
            data_source.get("allow_estimated_values", False)
        ),
    )


def _finalise_postprocessed_output(
    solved: SolvedScenario,
    *,
    run_id: str,
    scenario_json: Mapping[str, Any],
) -> SolvedScenario:
    """Correct temporary draft-postprocessor placeholders deterministically."""

    return replace(
        solved,
        run_id=run_id,
        scenario=replace(
            solved.scenario,
            data_source=_data_source_from_json(scenario_json),
        ),
    )


def _run_loaded_document(
    scenario_json: Mapping[str, Any],
    *,
    scenario_loader: Any,
    options: PipelineOptions,
    run_id: str | None,
) -> SolvedScenario:
    """Execute loader -> preprocessing -> solve -> postprocessing."""

    scenario_id_hint = scenario_json.get("scenario_id")
    scenario_id = (
        scenario_id_hint.strip()
        if isinstance(scenario_id_hint, str) and scenario_id_hint.strip()
        else None
    )

    # Stage 1: existing data loader
    try:
        scenario = scenario_loader()
    except DataLoadError as exc:
        raise PipelineStageError(
            "DATA_LOADING",
            str(exc),
            scenario_id=scenario_id,
            run_id=run_id,
        ) from exc
    except Exception as exc:
        raise PipelineStageError(
            "DATA_LOADING",
            f"{type(exc).__name__}: {exc}",
            scenario_id=scenario_id,
            run_id=run_id,
        ) from exc

    scenario_id = scenario.scenario_id
    resolved_run_id = (
        _clean_nonblank(run_id, "run_id")
        if run_id is not None
        else _new_run_id(scenario_id)
    )

    # Stage 2: existing preprocessing
    try:
        parameters = preprocess_scenario(
            scenario,
            validate_capacity_feasibility=options.validate_capacity_feasibility,
            validate_quality_feasibility=options.validate_quality_feasibility,
        )
    except PreprocessingError as exc:
        raise PipelineStageError(
            "PREPROCESSING",
            str(exc),
            scenario_id=scenario_id,
            run_id=resolved_run_id,
        ) from exc
    except Exception as exc:
        raise PipelineStageError(
            "PREPROCESSING",
            f"{type(exc).__name__}: {exc}",
            scenario_id=scenario_id,
            run_id=resolved_run_id,
        ) from exc

    # Stage 3: existing model + HiGHS solve
    try:
        model, solver_results = solve(parameters, tee=options.tee)
        _ensure_feasible_solution_loaded(model, solver_results)
    except Exception as exc:
        raise PipelineStageError(
            "SOLVING",
            f"{type(exc).__name__}: {exc}",
            scenario_id=scenario_id,
            run_id=resolved_run_id,
        ) from exc

    # Stage 4: existing postprocessor
    scenario_view = _postprocessing_scenario_view(scenario, scenario_json)
    parameters_view = _postprocessing_parameters_view(
        parameters,
        run_id=resolved_run_id,
        options=options,
    )

    try:
        solved = postprocess_solution(
            scenario_view,      # type: ignore[arg-type]
            parameters_view,    # type: ignore[arg-type]
            model,
            solver_results,
        )
    except PostprocessingError as exc:
        raise PipelineStageError(
            "POSTPROCESSING",
            str(exc),
            scenario_id=scenario_id,
            run_id=resolved_run_id,
        ) from exc
    except Exception as exc:
        raise PipelineStageError(
            "POSTPROCESSING",
            f"{type(exc).__name__}: {exc}",
            scenario_id=scenario_id,
            run_id=resolved_run_id,
        ) from exc

    return _finalise_postprocessed_output(
        solved,
        run_id=resolved_run_id,
        scenario_json=scenario_json,
    )


def run_pipeline_from_scenario_json(
    scenario_json: Mapping[str, Any],
    *,
    options: PipelineOptions | None = None,
    run_id: str | None = None,
) -> SolvedScenario:
    """Run the full MILP from one already-retrieved raw scenario document."""

    if not isinstance(scenario_json, Mapping):
        raise PipelineError("scenario_json must be a mapping/JSON object.")

    document = dict(scenario_json)
    options = options or PipelineOptions()

    return _run_loaded_document(
        document,
        scenario_loader=lambda: _load_scenario_from_mapping(document),
        options=options,
        run_id=run_id,
    )


def run_pipeline(
    scenario_id: str,
    *,
    options: PipelineOptions | None = None,
    table: str | None = None,
    id_column: str | None = None,
    json_column: str | None = None,
    run_id: str | None = None,
) -> SolvedScenario:
    """Production entry point: Supabase scenario ID -> SolvedScenario."""

    scenario_id = _clean_nonblank(scenario_id, "scenario_id")
    resolved_run_id = (
        _clean_nonblank(run_id, "run_id")
        if run_id is not None
        else _new_run_id(scenario_id)
    )

    try:
        scenario_json = fetch_scenario_json(
            scenario_id,
            table=table,
            id_column=id_column,
            json_column=json_column,
        )
    except ScenarioFetchError as exc:
        raise PipelineStageError(
            "SCENARIO_FETCH",
            str(exc),
            scenario_id=scenario_id,
            run_id=resolved_run_id,
        ) from exc

    return run_pipeline_from_scenario_json(
        scenario_json,
        options=options,
        run_id=resolved_run_id,
    )


def run_pipeline_from_file(
    scenario_path: str | Path,
    *,
    options: PipelineOptions | None = None,
    run_id: str | None = None,
) -> SolvedScenario:
    """Development/test entry point using the same real MILP stages."""

    path = Path(scenario_path)

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineStageError("SCENARIO_FILE", str(exc)) from exc

    if not isinstance(raw, dict):
        raise PipelineStageError(
            "SCENARIO_FILE",
            "Scenario file must contain one JSON object.",
        )

    options = options or PipelineOptions()

    # Unlike the Supabase path, a local file already satisfies the loader's
    # native interface, so do not create an unnecessary second temporary file.
    return _run_loaded_document(
        raw,
        scenario_loader=lambda: load_scenario(path, strict=True),
        options=options,
        run_id=run_id,
    )


def solved_scenario_to_dict(solved: SolvedScenario) -> dict[str, Any]:
    """Convert the final output dataclass to plain JSON-compatible Python data."""

    return asdict(solved)


def solved_scenario_to_json(
    solved: SolvedScenario,
    *,
    indent: int | None = 2,
) -> str:
    """Serialise strictly: NaN/Infinity or non-JSON values are rejected."""

    try:
        return json.dumps(
            solved_scenario_to_dict(solved),
            indent=indent,
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise PipelineError(
            "SolvedScenario contains a value that is not valid JSON."
        ) from exc


def write_solved_scenario_json(
    solved: SolvedScenario,
    output_path: str | Path,
) -> Path:
    """Atomically write the final output JSON for local integration/debugging."""

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
    print(
        "Output consistency: "
        f"{solved.validation.output_consistency.status}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the complete AquaBlend MILP pipeline."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--scenario-id",
        help="Scenario ID whose raw JSON/JSONB is stored in Supabase.",
    )
    source.add_argument(
        "--file",
        type=Path,
        help="Local scenario JSON for development/testing.",
    )

    parser.add_argument(
        "--scenario-table",
        help="Override AQUABLEND_SCENARIO_TABLE.",
    )
    parser.add_argument(
        "--scenario-id-column",
        help="Override AQUABLEND_SCENARIO_ID_COLUMN.",
    )
    parser.add_argument(
        "--scenario-json-column",
        help="Override AQUABLEND_SCENARIO_JSON_COLUMN.",
    )
    parser.add_argument(
        "--run-id",
        help="Optional externally assigned run ID.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path for serialised SolvedScenario JSON.",
    )
    parser.add_argument(
        "--tee",
        action="store_true",
        help="Print the HiGHS solver log.",
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
    options = PipelineOptions(
        tee=args.tee,
        validate_capacity_feasibility=not args.skip_capacity_check,
        validate_quality_feasibility=not args.skip_quality_check,
    )

    try:
        if args.scenario_id:
            solved = run_pipeline(
                args.scenario_id,
                options=options,
                table=args.scenario_table,
                id_column=args.scenario_id_column,
                json_column=args.scenario_json_column,
                run_id=args.run_id,
            )
        else:
            solved = run_pipeline_from_file(
                args.file,
                options=options,
                run_id=args.run_id,
            )

        _print_run_summary(solved)

        if args.output:
            written = write_solved_scenario_json(solved, args.output)
            print(f"Result JSON: {written}")

    except PipelineError as exc:
        print(f"AquaBlend pipeline failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
