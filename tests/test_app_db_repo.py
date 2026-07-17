"""Phase 3 step 2: SourceAuditLogRepository unit tests.

Requires CDSS_APP_DB_URL to point at a reachable, disposable PostgreSQL
database and skips (never fails) otherwise -- same D-009.1 "required for
exit, not start" pattern as tests/migrations.

Unlike tests/migrations' `conn` fixture, these tests do not run inside a
rolled-back transaction: SourceAuditLogRepository.record() commits its own
engine-level transaction per call by design (an audit write must survive
even if the caller's surrounding business transaction later rolls back --
the same durability guarantee the JSONL sink already has). The `clean_tables`
fixture below deletes what each test wrote instead.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from cdss.app_db_repo import SourceAuditLogRepository, source_audit_log
from cdss.source import AuditEvent


@pytest.fixture
def clean_tables(migrated_db: Engine) -> Iterator[Engine]:
    yield migrated_db
    with migrated_db.begin() as conn:
        conn.execute(sa.delete(source_audit_log))
        conn.execute(sa.text("DELETE FROM runs"))


def _event(**overrides: object) -> AuditEvent:
    defaults: dict[str, object] = {
        "statement": "SELECT 1 FROM dbo.vw_appointments",
        "params": (),
        "timestamp": "2026-07-16T16:55:14.283097+00:00",
        "duration_ms": 12.5,
        "rows_returned": 3,
        "component": "test-component",
    }
    defaults.update(overrides)
    return AuditEvent(**defaults)  # type: ignore[arg-type]


def test_record_inserts_exactly_one_row(clean_tables: Engine) -> None:
    SourceAuditLogRepository(clean_tables).record(_event())
    with clean_tables.connect() as conn:
        rows = conn.execute(sa.select(source_audit_log)).all()
    assert len(rows) == 1


def test_record_stores_statement_component_and_row_count(clean_tables: Engine) -> None:
    SourceAuditLogRepository(clean_tables).record(_event(duration_ms=12.6))
    with clean_tables.connect() as conn:
        row = conn.execute(sa.select(source_audit_log)).one()
    assert row.statement == "SELECT 1 FROM dbo.vw_appointments"
    assert row.component == "test-component"
    assert row.row_count == 3
    assert row.duration_ms == 13  # rounded from 12.6


def test_record_stores_params_as_list(clean_tables: Engine) -> None:
    SourceAuditLogRepository(clean_tables).record(
        _event(statement="SELECT 1 FROM dbo.vw_appointments WHERE id = ?", params=(42,))
    )
    with clean_tables.connect() as conn:
        row = conn.execute(sa.select(source_audit_log)).one()
    assert row.params == [42]


def test_record_with_no_run_id_stores_null(clean_tables: Engine) -> None:
    SourceAuditLogRepository(clean_tables).record(_event(run_id=None))
    with clean_tables.connect() as conn:
        row = conn.execute(sa.select(source_audit_log)).one()
    assert row.run_id is None


def test_record_with_run_id_stores_it(clean_tables: Engine) -> None:
    with clean_tables.begin() as conn:
        run_id = str(conn.execute(sa.text("INSERT INTO runs DEFAULT VALUES RETURNING id")).one().id)

    SourceAuditLogRepository(clean_tables).record(_event(run_id=run_id))
    with clean_tables.connect() as conn:
        row = conn.execute(sa.select(source_audit_log)).one()
    assert row.run_id == uuid.UUID(run_id)


def test_record_twice_inserts_two_rows(clean_tables: Engine) -> None:
    repo = SourceAuditLogRepository(clean_tables)
    repo.record(_event())
    repo.record(_event())
    with clean_tables.connect() as conn:
        rows = conn.execute(sa.select(source_audit_log)).all()
    assert len(rows) == 2
