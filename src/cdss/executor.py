"""Executor core (Phase 3 step 5): compile -> bind -> execute -> classify ->
account, one (check, practice) at a time, plus the run-level bookkeeping
(`runs`, `check_executions`) and preflight schema-drift detection that wraps
it.

**Named-param bridging, the piece `cdss.compiler`'s own docstring calls out
as "meant for the not-yet-built Phase 3 executor to bind"**: compiled SQL
uses T-SQL named parameters (`@stale_days`, ...), but the source access
layer (`cdss.source.AuditedSourceConnection`) binds positionally, ODBC-style
(`?`) -- and, more fundamentally, a bare `@name` reference with no preceding
`DECLARE` in the same batch is not valid standalone T-SQL, while a
`DECLARE ...; SELECT ...` pair is two statements, which the source access
layer's single-SELECT guard (D-015/constraint 1) rejects outright. This
module's `bind_named_params` rewrites every `@name` token to `?` (in
left-to-right occurrence order, matching ODBC's positional contract) and
returns the matching positional value list -- a real, working single
`SELECT`, not a workaround for the guard.

Preflight drift detection compares a view's *pinned* column set (the
catalog version a check was compiled against) to its *live* one fetched via
`INFORMATION_SCHEMA.COLUMNS` (D-015, always allowed). Only a *missing*
pinned column counts as drift -- a check's compiled SQL can only break by a
column disappearing or being renamed out from under it; an extra live
column that no check references yet is not itself a problem.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa

from cdss.check_registry import LoadedCheck
from cdss.compiler import compile_check, project_columns
from cdss.dsl import CheckDoc, check_doc_from_dict
from cdss.source import AuditedSourceConnection
from cdss.watermark_manager import ScanWindow

_PARAM_TOKEN = re.compile(r"@(\w+)")


def bind_named_params(sql_text: str, params: dict[str, Any]) -> tuple[str, list[Any]]:
    """Rewrite every `@name` token in `sql_text` to `?`, returning the
    rewritten SQL and the positional value list (in occurrence order) ODBC
    needs. Raises KeyError naming the first token with no bound value --
    an unbound param is an authoring/config error, never silently NULL."""
    positional: list[Any] = []

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in params:
            raise KeyError(f"no bound value for parameter '@{name}'")
        positional.append(params[name])
        return "?"

    rewritten = _PARAM_TOKEN.sub(_replace, sql_text)
    return rewritten, positional


def _array_element_bindings(doc: CheckDoc) -> dict[str, Any]:
    """Array-typed params compile to one named param per element
    (`cdss.compiler._expand_array_params`); the element *values* come from
    the check's own fixed default (never practice config -- an
    in-against-catalog-domain array is static, human-reviewed, D-014/dsl.md),
    so they must be re-derived here the same way the compiler names them."""
    bindings: dict[str, Any] = {}
    for name, param in doc.params.items():
        if param.type == "array":
            for i, value in enumerate(param.default.value):
                bindings[f"{name}_{i}"] = value
    return bindings


@dataclass(frozen=True)
class ExecutedRow:
    entity_key: tuple[Any, ...]
    tri_state: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class CheckExecutionResult:
    check_id: str
    check_version_id: str
    practice_id: str
    sql_hash: str
    watermark_from: datetime | None
    watermark_to: datetime | None
    duration_ms: int
    rows_examined: int
    n_pass: int
    n_fail: int
    n_indeterminate: int
    status: str
    error_message: str | None
    rows: tuple[ExecutedRow, ...]


def execute_check(
    source_conn: AuditedSourceConnection,
    loaded_check: LoadedCheck,
    *,
    watermark_column: str | None = None,
    scan_window: ScanWindow | None = None,
    run_id: str | None = None,
) -> CheckExecutionResult:
    """Compile `loaded_check`'s definition, bind its params (array elements
    + the practice's resolved scalar overrides) plus the watermark window if
    one is given, execute through `source_conn`, and tri-state-classify
    every returned row. Never raises for a source-execution failure --
    caught and returned as `status='error'` (`check_executions.status`
    already has this value; one bad check must not abort the run).

    `run_id`, when given, is threaded into `source_conn.execute_query` so
    the resulting audit event (both the JSONL line and, if an app_db_sink is
    wired, the `source_audit_log` mirror) carries it -- F10/D-016's "every
    source statement of a run is audited... with the run id attached"."""
    doc = check_doc_from_dict(loaded_check.definition)
    use_watermark = watermark_column is not None and scan_window is not None
    compiled = compile_check(doc, watermark_column=watermark_column if use_watermark else None)

    bind_params: dict[str, Any] = {**_array_element_bindings(doc), **loaded_check.params}
    if use_watermark:
        assert scan_window is not None
        bind_params["watermark_from"] = scan_window.from_ts
        bind_params["watermark_to"] = scan_window.to_ts

    head, tail = project_columns(doc)
    tri_state_index = len(head)
    key_len = len(doc.entity.key)

    start = time.perf_counter()
    try:
        rewritten_sql, positional_params = bind_named_params(compiled.sql_text, bind_params)
        raw_rows = source_conn.execute_query(rewritten_sql, positional_params, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 -- deliberately broad: any source
        # failure must degrade to one failed check_execution, never abort
        # the run or propagate to the caller.
        duration_ms = round((time.perf_counter() - start) * 1000)
        return CheckExecutionResult(
            check_id=loaded_check.check_id,
            check_version_id=loaded_check.check_version_id,
            practice_id=loaded_check.practice_id,
            sql_hash=compiled.sql_hash,
            watermark_from=scan_window.from_ts if scan_window is not None else None,
            watermark_to=scan_window.to_ts if scan_window is not None else None,
            duration_ms=duration_ms,
            rows_examined=0,
            n_pass=0,
            n_fail=0,
            n_indeterminate=0,
            status="error",
            error_message=str(exc),
            rows=(),
        )
    duration_ms = round((time.perf_counter() - start) * 1000)

    rows: list[ExecutedRow] = []
    n_pass = n_fail = n_indeterminate = 0
    for raw_row in raw_rows:
        head_values = raw_row[:tri_state_index]
        tri_state = raw_row[tri_state_index]
        tail_values = raw_row[tri_state_index + 1 :]
        column_values = dict(zip(head + tail, head_values + tail_values, strict=True))
        entity_key = tuple(head_values[:key_len])
        evidence = {column: column_values[column] for column in doc.evidence}
        rows.append(ExecutedRow(entity_key=entity_key, tri_state=tri_state, evidence=evidence))
        if tri_state == "pass":
            n_pass += 1
        elif tri_state == "fail":
            n_fail += 1
        else:
            n_indeterminate += 1

    return CheckExecutionResult(
        check_id=loaded_check.check_id,
        check_version_id=loaded_check.check_version_id,
        practice_id=loaded_check.practice_id,
        sql_hash=compiled.sql_hash,
        watermark_from=scan_window.from_ts if scan_window is not None else None,
        watermark_to=scan_window.to_ts if scan_window is not None else None,
        duration_ms=duration_ms,
        rows_examined=len(raw_rows),
        n_pass=n_pass,
        n_fail=n_fail,
        n_indeterminate=n_indeterminate,
        status="ok",
        error_message=None,
        rows=tuple(rows),
    )


# --- run + check_execution bookkeeping (app DB) -----------------------------

_INSERT_RUN_SQL = sa.text(
    "INSERT INTO runs (catalog_version_id, triggered_by) "
    "VALUES (:catalog_version_id, :triggered_by) RETURNING id"
)

_FINISH_RUN_SQL = sa.text(
    "UPDATE runs SET status = :status, finished_at = now() WHERE id = :run_id"
)

_INSERT_CHECK_EXECUTION_SQL = sa.text(
    """
    INSERT INTO check_executions
        (run_id, check_id, check_version_id, practice_id, sql_hash,
         watermark_from, watermark_to, duration_ms, rows_examined,
         n_pass, n_fail, n_indeterminate, status, error_message)
    VALUES
        (:run_id, :check_id, :check_version_id, :practice_id, :sql_hash,
         :watermark_from, :watermark_to, :duration_ms, :rows_examined,
         :n_pass, :n_fail, :n_indeterminate, :status, :error_message)
    RETURNING id
    """
)


def create_run(
    conn: sa.Connection, *, catalog_version_id: int | None = None, triggered_by: str | None = None
) -> str:
    row = conn.execute(
        _INSERT_RUN_SQL, {"catalog_version_id": catalog_version_id, "triggered_by": triggered_by}
    ).one()
    return str(row.id)


def finish_run(conn: sa.Connection, run_id: str, *, status: str) -> None:
    conn.execute(_FINISH_RUN_SQL, {"run_id": run_id, "status": status})


def record_check_execution(conn: sa.Connection, run_id: str, result: CheckExecutionResult) -> str:
    row = conn.execute(
        _INSERT_CHECK_EXECUTION_SQL,
        {
            "run_id": run_id,
            "check_id": result.check_id,
            "check_version_id": result.check_version_id,
            "practice_id": result.practice_id,
            "sql_hash": result.sql_hash,
            "watermark_from": result.watermark_from,
            "watermark_to": result.watermark_to,
            "duration_ms": result.duration_ms,
            "rows_examined": result.rows_examined,
            "n_pass": result.n_pass,
            "n_fail": result.n_fail,
            "n_indeterminate": result.n_indeterminate,
            "status": result.status,
            "error_message": result.error_message,
        },
    ).one()
    return str(row.id)


# --- preflight: schema drift -------------------------------------------------

_SELECT_LIVE_COLUMNS_SQL = (
    "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?"
)

_INSERT_SCHEMA_DRIFT_EVENT_SQL = sa.text(
    "INSERT INTO schema_drift_events (run_id, view_name, catalog_version_id, detail) "
    "VALUES (:run_id, :view_name, :catalog_version_id, CAST(:detail AS jsonb))"
)


def fetch_live_columns(
    source_conn: AuditedSourceConnection, view_name: str, *, run_id: str | None = None
) -> frozenset[str]:
    """Live columns for `view_name` ("schema.table") via INFORMATION_SCHEMA
    (D-015 catalog metadata -- always allowed, no view allowlist needed)."""
    schema, _, table = view_name.partition(".")
    rows = source_conn.execute_query(_SELECT_LIVE_COLUMNS_SQL, [schema, table], run_id=run_id)
    return frozenset(row[0] for row in rows)


def compute_missing_columns(
    pinned_columns: frozenset[str], live_columns: frozenset[str]
) -> frozenset[str]:
    """Columns the pinned catalog recorded for a view that are no longer
    live -- the only drift shape that can break already-compiled SQL. A
    live column absent from the pinned set (a genuinely new column) is not
    drift by this definition; nothing references it yet."""
    return pinned_columns - live_columns


def record_schema_drift_event(
    conn: sa.Connection,
    run_id: str,
    view_name: str,
    catalog_version_id: int,
    detail: dict[str, Any],
) -> None:
    conn.execute(
        _INSERT_SCHEMA_DRIFT_EVENT_SQL,
        {
            "run_id": run_id,
            "view_name": view_name,
            "catalog_version_id": catalog_version_id,
            "detail": json.dumps(detail),
        },
    )


def execute_check_with_preflight(
    conn: sa.Connection,
    source_conn: AuditedSourceConnection,
    run_id: str,
    loaded_check: LoadedCheck,
    driving_view: str,
    pinned_columns: frozenset[str],
    catalog_version_id: int,
    *,
    watermark_column: str | None = None,
    scan_window: ScanWindow | None = None,
) -> CheckExecutionResult:
    """The full per-(check, practice) run step the phase spec describes:
    preflight (live schema vs. `pinned_columns`) then either skip (drift) or
    execute, always recording a `check_executions` row either way -- a
    drifted check's accounting must not silently vanish from the run."""
    live_columns = fetch_live_columns(source_conn, driving_view, run_id=run_id)
    missing = compute_missing_columns(pinned_columns, live_columns)
    if missing:
        record_schema_drift_event(
            conn,
            run_id,
            driving_view,
            catalog_version_id,
            {"missing_columns": sorted(missing)},
        )
        doc = check_doc_from_dict(loaded_check.definition)
        result = CheckExecutionResult(
            check_id=loaded_check.check_id,
            check_version_id=loaded_check.check_version_id,
            practice_id=loaded_check.practice_id,
            sql_hash=compile_check(doc).sql_hash,
            watermark_from=None,
            watermark_to=None,
            duration_ms=0,
            rows_examined=0,
            n_pass=0,
            n_fail=0,
            n_indeterminate=0,
            status="skipped_drift",
            error_message=f"missing columns on {driving_view}: {sorted(missing)}",
            rows=(),
        )
    else:
        result = execute_check(
            source_conn,
            loaded_check,
            watermark_column=watermark_column,
            scan_window=scan_window,
            run_id=run_id,
        )
    record_check_execution(conn, run_id, result)
    return result


__all__ = [
    "CheckExecutionResult",
    "ExecutedRow",
    "bind_named_params",
    "compute_missing_columns",
    "create_run",
    "execute_check",
    "execute_check_with_preflight",
    "fetch_live_columns",
    "finish_run",
    "record_check_execution",
    "record_schema_drift_event",
]
