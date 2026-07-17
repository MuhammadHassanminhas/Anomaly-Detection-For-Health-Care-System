"""Audited, read-only access layer for the source (INDICI_BI_Full) database.

Every statement passes through AuditedSourceConnection.execute_query(), the
single choke point that:
  (a) rejects anything but one SELECT against the allowlist — DML, DDL,
      multi-statement, and non-allowlisted objects are all refused
      (constraint 1/3; parsed with sqlglot, never a string heuristic), and
  (b) appends exactly one JSONL audit line per accepted statement: statement,
      params, UTC timestamp, duration_ms, rows_returned, component (D-016,
      constraint 7).

INFORMATION_SCHEMA / sys catalog reads are always allowed (D-015); reads
against the enumerated view surface are allowed once passed in explicitly.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

DEFAULT_AUDIT_DIR = Path("artifacts/audit")

# D-015: system-catalog metadata is always readable. The enumerated view
# surface (Phase 0 step 5) is supplied per-connection via `allowed_objects`.
CATALOG_SCHEMA_ALLOWLIST: frozenset[str] = frozenset({"information_schema", "sys"})


class StatementRejectedError(ValueError):
    """A statement failed the SQL guard: not a single SELECT, or not allowlisted."""


class Cursor(Protocol):
    """The subset of DB-API 2.0 cursor behaviour the audited layer relies on."""

    def execute(self, statement: str, params: Sequence[Any] | None = ...) -> Any: ...
    def fetchall(self) -> Sequence[tuple[Any, ...]]: ...


class SourceDBConnection(Protocol):
    """The subset of DB-API 2.0 connection behaviour the audited layer relies on."""

    timeout: int

    def cursor(self) -> Cursor: ...


@dataclass(frozen=True)
class AuditEvent:
    statement: str
    params: tuple[Any, ...]
    timestamp: str
    duration_ms: float
    rows_returned: int
    component: str
    run_id: str | None = None

    def to_json(self) -> str:
        # default=str: params can be non-JSON-native (e.g. datetime, from
        # Phase 3 executor watermark binding) -- the audit line must never
        # fail to write over a param type json can't natively serialize.
        return json.dumps(
            {
                "statement": self.statement,
                "params": list(self.params),
                "timestamp": self.timestamp,
                "duration_ms": self.duration_ms,
                "rows_returned": self.rows_returned,
                "component": self.component,
                "run_id": self.run_id,
            },
            sort_keys=True,
            default=str,
        )


class AppDbAuditSink(Protocol):
    """The app-DB half of the dual audit write (D-016): mirrors an accepted
    statement's AuditEvent into source_audit_log. JSONL remains primary --
    this sink is an additional, optional destination for the same event."""

    def record(self, event: AuditEvent) -> None: ...


def _validate_statement(statement: str, allowed_objects: frozenset[str]) -> exp.Select:
    """Return the parsed SELECT if `statement` is exactly one SELECT referencing
    only allowlisted objects; raise StatementRejectedError otherwise."""
    try:
        parsed = sqlglot.parse(statement, read="tsql")
    except ParseError as exc:
        raise StatementRejectedError(f"could not parse statement: {exc}") from exc

    non_empty = [node for node in parsed if node is not None]
    if len(non_empty) != 1:
        raise StatementRejectedError(f"exactly one statement is required, got {len(non_empty)}")

    node = non_empty[0]
    if not isinstance(node, exp.Select):
        raise StatementRejectedError(
            f"only SELECT statements are permitted, got {type(node).__name__}"
        )

    for table in node.find_all(exp.Table):
        schema = (table.db or "dbo").lower()
        name = table.name.lower()
        qualified = f"{schema}.{name}"
        if schema in CATALOG_SCHEMA_ALLOWLIST:
            continue
        if qualified in allowed_objects:
            continue
        raise StatementRejectedError(f"object '{qualified}' is not on the allowlist")

    return node


class AuditedSourceConnection:
    """Wraps a read-only DB-API connection; execute_query() is the only
    statement entry point — there is no other way to reach the connection."""

    def __init__(
        self,
        connection: SourceDBConnection,
        *,
        component: str,
        allowed_objects: frozenset[str] = frozenset(),
        audit_dir: Path = DEFAULT_AUDIT_DIR,
        clock: Callable[[], datetime] | None = None,
        app_db_sink: AppDbAuditSink | None = None,
    ) -> None:
        self._connection = connection
        self._component = component
        self._allowed_objects = allowed_objects
        self._audit_dir = audit_dir
        self._clock = clock or (lambda: datetime.now(UTC))
        self._app_db_sink = app_db_sink

    def execute_query(
        self,
        statement: str,
        params: Sequence[Any] | None = None,
        *,
        timeout_seconds: int | None = None,
        run_id: str | None = None,
    ) -> list[tuple[Any, ...]]:
        _validate_statement(statement, self._allowed_objects)

        start = time.perf_counter()
        if timeout_seconds is not None:
            self._connection.timeout = timeout_seconds
        cursor = self._connection.cursor()
        if params:
            cursor.execute(statement, params)
        else:
            cursor.execute(statement)
        rows = list(cursor.fetchall())
        duration_ms = (time.perf_counter() - start) * 1000

        event = AuditEvent(
            statement=statement,
            params=tuple(params or ()),
            timestamp=self._clock().isoformat(),
            duration_ms=round(duration_ms, 3),
            rows_returned=len(rows),
            component=self._component,
            run_id=run_id,
        )
        self._write_audit_event(event)
        if self._app_db_sink is not None:
            self._app_db_sink.record(event)
        return rows

    def with_allowed_objects(self, allowed_objects: frozenset[str]) -> AuditedSourceConnection:
        """Return a new connection over the same underlying connection and
        audit sink, additionally permitted to SELECT from `allowed_objects`.
        Used once the view surface is known (Phase 0 step 5) to unlock reads
        against the enumerated objects themselves — the original instance is
        left unchanged."""
        return AuditedSourceConnection(
            self._connection,
            component=self._component,
            allowed_objects=self._allowed_objects | allowed_objects,
            audit_dir=self._audit_dir,
            clock=self._clock,
            app_db_sink=self._app_db_sink,
        )

    def _write_audit_event(self, event: AuditEvent) -> None:
        self._audit_dir.mkdir(parents=True, exist_ok=True)
        date_stamp = self._clock().strftime("%Y%m%d")
        path = self._audit_dir / f"source-audit-{date_stamp}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(event.to_json() + "\n")


__all__ = [
    "AppDbAuditSink",
    "AuditEvent",
    "AuditedSourceConnection",
    "CATALOG_SCHEMA_ALLOWLIST",
    "StatementRejectedError",
]
