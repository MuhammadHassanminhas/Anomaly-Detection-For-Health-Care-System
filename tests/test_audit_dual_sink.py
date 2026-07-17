"""Phase 3 step 2 deliverable: the audit dual-sink test -- one accepted
source statement produces exactly one JSONL line AND exactly one
source_audit_log row.

Only the app-DB (mirror) side needs a real database; the source side is the
same FakeConnection used throughout tests/test_source.py, since this test
proves the dual-write wiring, not source-DB connectivity. Requires
CDSS_APP_DB_URL and skips (never fails) otherwise -- D-009.1.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from cdss.app_db_repo import SourceAuditLogRepository, source_audit_log
from cdss.source import AuditedSourceConnection


class FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows
        self.executed: list[str] = []

    def execute(self, statement: str, params: Any = None) -> None:
        self.executed.append(statement)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class FakeConnection:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.last_cursor = FakeCursor(rows)
        self.timeout = 0

    def cursor(self) -> FakeCursor:
        return self.last_cursor


@pytest.fixture
def clean_audit_log(migrated_db: Engine) -> Iterator[Engine]:
    yield migrated_db
    with migrated_db.begin() as conn:
        conn.execute(sa.delete(source_audit_log))


def _read_audit_lines(audit_dir: Path) -> list[dict[str, Any]]:
    files = list(audit_dir.glob("source-audit-*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line]


def test_one_accepted_statement_produces_one_jsonl_line_and_one_row(
    tmp_path: Path, clean_audit_log: Engine
) -> None:
    repo = SourceAuditLogRepository(clean_audit_log)
    audited = AuditedSourceConnection(
        FakeConnection(rows=[(1,), (2,)]),  # type: ignore[arg-type]
        component="dual-sink-test",
        allowed_objects=frozenset({"dbo.vw_appointments"}),
        audit_dir=tmp_path,
        app_db_sink=repo,
    )

    audited.execute_query("SELECT * FROM dbo.vw_appointments")

    jsonl_events = _read_audit_lines(tmp_path)
    assert len(jsonl_events) == 1
    assert jsonl_events[0]["statement"] == "SELECT * FROM dbo.vw_appointments"
    assert jsonl_events[0]["component"] == "dual-sink-test"
    assert jsonl_events[0]["rows_returned"] == 2

    with clean_audit_log.connect() as conn:
        db_rows = conn.execute(sa.select(source_audit_log)).all()
    assert len(db_rows) == 1
    assert db_rows[0].statement == "SELECT * FROM dbo.vw_appointments"
    assert db_rows[0].component == "dual-sink-test"
    assert db_rows[0].row_count == 2
