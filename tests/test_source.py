import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cdss.source import AuditedSourceConnection, AuditEvent, StatementRejectedError


class FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self._rows = rows or []
        self.executed: list[tuple[str, Sequence[Any] | None]] = []

    def execute(self, statement: str, params: Sequence[Any] | None = None) -> None:
        self.executed.append((statement, params))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class FakeConnection:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.last_cursor = FakeCursor(rows)
        self.timeout = 0

    def cursor(self) -> FakeCursor:
        return self.last_cursor


def _make_source(
    tmp_path: Path,
    rows: list[tuple[Any, ...]] | None = None,
    allowed_objects: frozenset[str] = frozenset(),
) -> tuple[AuditedSourceConnection, FakeConnection]:
    fake_conn = FakeConnection(rows)
    audited = AuditedSourceConnection(
        fake_conn,  # type: ignore[arg-type]
        component="test-component",
        allowed_objects=allowed_objects,
        audit_dir=tmp_path,
        clock=lambda: datetime(2026, 7, 14, 3, 0, 0, tzinfo=UTC),
    )
    return audited, fake_conn


def _read_audit_lines(tmp_path: Path) -> list[dict[str, Any]]:
    audit_file = tmp_path / "source-audit-20260714.jsonl"
    if not audit_file.exists():
        return []
    lines = audit_file.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line]


# --- accepted statements -----------------------------------------------------


def test_select_against_information_schema_is_accepted(tmp_path: Path) -> None:
    audited, fake_conn = _make_source(tmp_path, rows=[("dbo", "vw_appointments")])
    rows = audited.execute_query("SELECT table_schema, table_name FROM INFORMATION_SCHEMA.VIEWS")
    assert rows == [("dbo", "vw_appointments")]
    assert fake_conn.last_cursor.executed[0][0] == (
        "SELECT table_schema, table_name FROM INFORMATION_SCHEMA.VIEWS"
    )


def test_select_against_sys_catalog_is_accepted(tmp_path: Path) -> None:
    audited, _ = _make_source(tmp_path, rows=[(1,)])
    rows = audited.execute_query("SELECT type FROM sys.objects")
    assert rows == [(1,)]


def test_select_against_allowlisted_view_is_accepted(tmp_path: Path) -> None:
    audited, _ = _make_source(
        tmp_path, rows=[(1,)], allowed_objects=frozenset({"dbo.vw_appointments"})
    )
    rows = audited.execute_query("SELECT * FROM dbo.vw_appointments")
    assert rows == [(1,)]


# --- rejected statements ------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO dbo.vw_appointments (id) VALUES (1)",
        "UPDATE dbo.vw_appointments SET id = 1",
        "DELETE FROM dbo.vw_appointments",
        "DROP TABLE dbo.vw_appointments",
        "CREATE TABLE dbo.evil (id INT)",
        "ALTER TABLE dbo.vw_appointments ADD COLUMN evil INT",
        "TRUNCATE TABLE dbo.vw_appointments",
        "EXEC sp_configure 'show advanced options', 1",
    ],
)
def test_non_select_statements_are_rejected(tmp_path: Path, statement: str) -> None:
    audited, fake_conn = _make_source(tmp_path, allowed_objects=frozenset({"dbo.vw_appointments"}))
    with pytest.raises(StatementRejectedError):
        audited.execute_query(statement)
    assert fake_conn.last_cursor.executed == []


def test_multi_statement_is_rejected(tmp_path: Path) -> None:
    audited, fake_conn = _make_source(tmp_path, allowed_objects=frozenset({"dbo.vw_appointments"}))
    with pytest.raises(StatementRejectedError):
        audited.execute_query("SELECT 1 FROM dbo.vw_appointments; DROP TABLE dbo.vw_appointments")
    assert fake_conn.last_cursor.executed == []


def test_base_table_not_on_allowlist_is_rejected(tmp_path: Path) -> None:
    audited, fake_conn = _make_source(tmp_path, allowed_objects=frozenset({"dbo.vw_appointments"}))
    with pytest.raises(StatementRejectedError, match="dbo.patient"):
        audited.execute_query("SELECT * FROM dbo.Patient")
    assert fake_conn.last_cursor.executed == []


def test_unparseable_statement_is_rejected(tmp_path: Path) -> None:
    audited, fake_conn = _make_source(tmp_path)
    with pytest.raises(StatementRejectedError):
        audited.execute_query("SELECT FROM WHERE ;;; garbage )))")
    assert fake_conn.last_cursor.executed == []


# --- audit trail ---------------------------------------------------------------


def test_every_accepted_statement_produces_exactly_one_audit_line(tmp_path: Path) -> None:
    audited, _ = _make_source(
        tmp_path, rows=[(1,), (2,)], allowed_objects=frozenset({"dbo.vw_appointments"})
    )
    audited.execute_query("SELECT * FROM dbo.vw_appointments")
    audited.execute_query("SELECT * FROM dbo.vw_appointments")
    audited.execute_query("SELECT * FROM dbo.vw_appointments")

    events = _read_audit_lines(tmp_path)
    assert len(events) == 3
    for event in events:
        assert event["statement"] == "SELECT * FROM dbo.vw_appointments"
        assert event["component"] == "test-component"
        assert event["rows_returned"] == 2
        assert event["timestamp"] == "2026-07-14T03:00:00+00:00"
        assert isinstance(event["duration_ms"], (int, float))
        assert event["duration_ms"] >= 0


def test_rejected_statement_writes_no_audit_line(tmp_path: Path) -> None:
    audited, _ = _make_source(tmp_path)
    with pytest.raises(StatementRejectedError):
        audited.execute_query("DROP TABLE dbo.vw_appointments")
    assert _read_audit_lines(tmp_path) == []


def test_no_params_calls_cursor_execute_with_statement_only(tmp_path: Path) -> None:
    # pyodbc raises if execute() is called with params=None on a statement with
    # zero placeholders ("0 parameter markers, but 1 parameters were supplied").
    audited, fake_conn = _make_source(tmp_path, rows=[(1,)])
    audited.execute_query("SELECT @@VERSION")
    assert fake_conn.last_cursor.executed == [("SELECT @@VERSION", None)]


def test_timeout_seconds_sets_connection_timeout(tmp_path: Path) -> None:
    audited, fake_conn = _make_source(tmp_path, rows=[(1,)])
    audited.execute_query("SELECT @@VERSION", timeout_seconds=5)
    assert fake_conn.timeout == 5


def test_no_timeout_seconds_leaves_connection_timeout_untouched(tmp_path: Path) -> None:
    audited, fake_conn = _make_source(tmp_path, rows=[(1,)])
    audited.execute_query("SELECT @@VERSION")
    assert fake_conn.timeout == 0


# --- allowlist expansion ------------------------------------------------------


def test_with_allowed_objects_permits_newly_added_object(tmp_path: Path) -> None:
    audited, _ = _make_source(tmp_path, rows=[(1,)])
    expanded = audited.with_allowed_objects(frozenset({"dbo.vw_appointments"}))
    rows = expanded.execute_query("SELECT * FROM dbo.vw_appointments")
    assert rows == [(1,)]


def test_with_allowed_objects_is_additive_not_replacing(tmp_path: Path) -> None:
    audited, _ = _make_source(tmp_path, rows=[(1,)], allowed_objects=frozenset({"dbo.patient"}))
    expanded = audited.with_allowed_objects(frozenset({"dbo.vw_appointments"}))
    assert expanded.execute_query("SELECT * FROM dbo.patient") == [(1,)]
    assert expanded.execute_query("SELECT * FROM dbo.vw_appointments") == [(1,)]


def test_with_allowed_objects_does_not_mutate_original(tmp_path: Path) -> None:
    audited, _ = _make_source(tmp_path, rows=[(1,)])
    audited.with_allowed_objects(frozenset({"dbo.vw_appointments"}))
    with pytest.raises(StatementRejectedError):
        audited.execute_query("SELECT * FROM dbo.vw_appointments")


def test_with_allowed_objects_shares_audit_sink(tmp_path: Path) -> None:
    audited, _ = _make_source(tmp_path, rows=[(1,)])
    expanded = audited.with_allowed_objects(frozenset({"dbo.vw_appointments"}))
    expanded.execute_query("SELECT * FROM dbo.vw_appointments")
    assert len(_read_audit_lines(tmp_path)) == 1


def test_audit_line_records_params(tmp_path: Path) -> None:
    audited, _ = _make_source(
        tmp_path, rows=[(1,)], allowed_objects=frozenset({"dbo.vw_appointments"})
    )
    audited.execute_query("SELECT * FROM dbo.vw_appointments WHERE id = ?", params=[42])
    events = _read_audit_lines(tmp_path)
    assert len(events) == 1
    assert events[0]["params"] == [42]


def test_audit_line_records_non_json_native_params(tmp_path: Path) -> None:
    audited, _ = _make_source(
        tmp_path, rows=[(1,)], allowed_objects=frozenset({"dbo.vw_appointments"})
    )
    watermark = datetime(2026, 1, 1, tzinfo=UTC)
    audited.execute_query(
        "SELECT * FROM dbo.vw_appointments WHERE updated_at > ?", params=[watermark]
    )
    events = _read_audit_lines(tmp_path)
    assert len(events) == 1
    assert events[0]["params"] == [str(watermark)]


def test_audit_line_run_id_defaults_to_none(tmp_path: Path) -> None:
    audited, _ = _make_source(tmp_path, rows=[(1,)])
    audited.execute_query("SELECT @@VERSION")
    events = _read_audit_lines(tmp_path)
    assert events[0]["run_id"] is None


def test_audit_line_records_run_id_when_supplied(tmp_path: Path) -> None:
    audited, _ = _make_source(tmp_path, rows=[(1,)])
    audited.execute_query("SELECT @@VERSION", run_id="run-123")
    events = _read_audit_lines(tmp_path)
    assert events[0]["run_id"] == "run-123"


# --- app-DB audit sink (D-016 dual write) --------------------------------------


class FakeAppDbSink:
    def __init__(self) -> None:
        self.recorded: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self.recorded.append(event)


def test_app_db_sink_called_once_per_accepted_statement(tmp_path: Path) -> None:
    sink = FakeAppDbSink()
    fake_conn = FakeConnection(rows=[(1,)])
    audited = AuditedSourceConnection(
        fake_conn,  # type: ignore[arg-type]
        component="test-component",
        audit_dir=tmp_path,
        clock=lambda: datetime(2026, 7, 14, 3, 0, 0, tzinfo=UTC),
        app_db_sink=sink,
    )
    audited.execute_query("SELECT @@VERSION")
    audited.execute_query("SELECT @@VERSION")
    assert len(sink.recorded) == 2
    assert sink.recorded[0].statement == "SELECT @@VERSION"


def test_app_db_sink_receives_run_id(tmp_path: Path) -> None:
    sink = FakeAppDbSink()
    fake_conn = FakeConnection(rows=[(1,)])
    audited = AuditedSourceConnection(
        fake_conn,  # type: ignore[arg-type]
        component="test-component",
        audit_dir=tmp_path,
        app_db_sink=sink,
    )
    audited.execute_query("SELECT @@VERSION", run_id="run-456")
    assert sink.recorded[0].run_id == "run-456"


def test_app_db_sink_not_called_when_statement_rejected(tmp_path: Path) -> None:
    sink = FakeAppDbSink()
    fake_conn = FakeConnection()
    audited = AuditedSourceConnection(
        fake_conn,  # type: ignore[arg-type]
        component="test-component",
        audit_dir=tmp_path,
        app_db_sink=sink,
    )
    with pytest.raises(StatementRejectedError):
        audited.execute_query("DROP TABLE dbo.vw_appointments")
    assert sink.recorded == []


def test_no_app_db_sink_is_optional(tmp_path: Path) -> None:
    audited, _ = _make_source(tmp_path, rows=[(1,)])
    # Must not raise even though no sink was supplied.
    audited.execute_query("SELECT @@VERSION")


def test_with_allowed_objects_preserves_app_db_sink(tmp_path: Path) -> None:
    sink = FakeAppDbSink()
    fake_conn = FakeConnection(rows=[(1,)])
    audited = AuditedSourceConnection(
        fake_conn,  # type: ignore[arg-type]
        component="test-component",
        audit_dir=tmp_path,
        app_db_sink=sink,
    )
    expanded = audited.with_allowed_objects(frozenset({"dbo.vw_appointments"}))
    expanded.execute_query("SELECT * FROM dbo.vw_appointments")
    assert len(sink.recorded) == 1
