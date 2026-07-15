from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cdss.rowstats import (
    COLUMNS_QUERY,
    ObjectRowStats,
    WatermarkColumn,
    compute_row_stats,
)
from cdss.source import AuditedSourceConnection
from cdss.surface import SurfaceObject


class ScriptedCursor:
    def __init__(self, responder: Any, connection: "ScriptedConnection") -> None:
        self._responder = responder
        self._connection = connection
        self._last_statement = ""

    def execute(self, statement: str, _params: Any = None) -> None:
        self._last_statement = statement

    def fetchall(self) -> list[tuple[Any, ...]]:
        result = self._responder(self._last_statement, self._connection.timeout)
        if isinstance(result, Exception):
            raise result
        return result  # type: ignore[no-any-return]


class ScriptedConnection:
    def __init__(self, responder: Any) -> None:
        self._responder = responder
        self.timeout = 0

    def cursor(self) -> ScriptedCursor:
        return ScriptedCursor(self._responder, self)


class FakeTimeoutError(Exception):
    """Stands in for pyodbc's timeout error (SQLSTATE HYT00)."""

    def __str__(self) -> str:
        return "('HYT00', '[HYT00] Query timeout expired')"


def _make_audited(
    tmp_path: Path,
    responder: Any,
    allowed_objects: frozenset[str] = frozenset({"dbo.patient", "dbo.appointments"}),
) -> AuditedSourceConnection:
    return AuditedSourceConnection(
        ScriptedConnection(responder),  # type: ignore[arg-type]
        component="test",
        allowed_objects=allowed_objects,
        audit_dir=tmp_path,
        clock=lambda: datetime(2026, 7, 14, tzinfo=UTC),
    )


COLUMNS_ROWS = [
    ("dbo", "Patient", "InsertedAt", "datetime"),
    ("dbo", "Patient", "UpdatedAt", "datetime"),
    ("dbo", "Patient", "PatientID", "int"),
    ("dbo", "Appointments", "PracticeID", "int"),  # no watermark columns at all
]

SURFACE = [
    SurfaceObject(schema="dbo", name="Patient", object_type="view", can_select=True),
    SurfaceObject(schema="dbo", name="Appointments", object_type="table", can_select=True),
]


def test_compute_row_stats_exact_count_and_watermarks(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        if statement == COLUMNS_QUERY:
            return COLUMNS_ROWS
        if statement == "SELECT COUNT(*) FROM dbo.Patient":
            return [(1000,)]
        if statement == "SELECT COUNT(*) FROM dbo.Appointments":
            return [(5,)]
        if statement.startswith("SELECT MIN([InsertedAt])"):
            return [("2020-01-01", "2026-01-01", "2020-01-02", "2026-01-02")]
        raise AssertionError(f"unexpected statement: {statement}")

    stats = compute_row_stats(_make_audited(tmp_path, responder), SURFACE, timeout_seconds=5)
    by_name = {s.qualified_name: s for s in stats}

    patient = by_name["dbo.Patient"]
    assert patient.row_count == 1000
    assert patient.row_count_status == "exact"
    assert patient.watermark_columns == [
        WatermarkColumn(
            column_name="InsertedAt",
            data_type="datetime",
            min_value="2020-01-01",
            max_value="2026-01-01",
        ),
        WatermarkColumn(
            column_name="UpdatedAt",
            data_type="datetime",
            min_value="2020-01-02",
            max_value="2026-01-02",
        ),
    ]

    appointments = by_name["dbo.Appointments"]
    assert appointments.row_count == 5
    assert appointments.row_count_status == "exact"
    assert appointments.watermark_columns == []


def test_compute_row_stats_table_count_timeout_falls_back_to_approximate(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]] | Exception:
        if statement == COLUMNS_QUERY:
            return []
        if statement == "SELECT COUNT(*) FROM dbo.Appointments":
            return FakeTimeoutError()
        if "sys.partitions" in statement:
            return [(123456,)]
        raise AssertionError(f"unexpected statement: {statement}")

    surface = [
        SurfaceObject(schema="dbo", name="Appointments", object_type="table", can_select=True)
    ]
    stats = compute_row_stats(_make_audited(tmp_path, responder), surface, timeout_seconds=5)

    assert stats[0].row_count == 123456
    assert stats[0].row_count_status == "approximate"


def test_compute_row_stats_view_count_timeout_is_indeterminate(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]] | Exception:
        if statement == COLUMNS_QUERY:
            return []
        if statement == "SELECT COUNT(*) FROM dbo.Patient":
            return FakeTimeoutError()
        raise AssertionError(f"unexpected statement: {statement}")

    surface = [SurfaceObject(schema="dbo", name="Patient", object_type="view", can_select=True)]
    stats = compute_row_stats(_make_audited(tmp_path, responder), surface, timeout_seconds=5)

    assert stats[0].row_count is None
    assert stats[0].row_count_status == "indeterminate"


def test_compute_row_stats_non_timeout_error_propagates(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]] | Exception:
        if statement == COLUMNS_QUERY:
            return []
        if statement == "SELECT COUNT(*) FROM dbo.Patient":
            return ValueError("some unrelated real error")
        raise AssertionError(f"unexpected statement: {statement}")

    surface = [SurfaceObject(schema="dbo", name="Patient", object_type="view", can_select=True)]
    with pytest.raises(ValueError, match="unrelated real error"):
        compute_row_stats(_make_audited(tmp_path, responder), surface, timeout_seconds=5)


def test_object_row_stats_and_watermark_column_are_frozen() -> None:
    wc = WatermarkColumn(
        column_name="InsertedAt", data_type="datetime", min_value=None, max_value=None
    )
    stats = ObjectRowStats(
        qualified_name="dbo.Patient",
        object_type="view",
        row_count=0,
        row_count_status="exact",
        duration_ms=1.0,
        watermark_columns=[wc],
    )
    assert stats.qualified_name == "dbo.Patient"
