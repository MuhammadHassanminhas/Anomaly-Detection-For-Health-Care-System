"""Phase 3 step 5 deliverable: the executor runs the Phase 2 example checks
against the fixture DB end-to-end; tri-state counts match Phase 2's
hand-computed expectations (tests/execution/test_execution.py's per-row
assertions, aggregated into counts here rather than re-derived from
scratch).

Requires the fixture SQL Server (LocalDB, D-026) via the shared
`fixture_conn` fixture -- skips (never fails) otherwise, D-009.1. Needs no
app DB at all: `LoadedCheck` is constructed directly from the checked-in
YAML, not loaded through cdss.check_registry (that wiring is proven
separately, in tests/executor/test_end_to_end.py).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyodbc
import pytest
import yaml

from cdss.check_registry import LoadedCheck
from cdss.executor import execute_check
from cdss.source import AuditedSourceConnection
from cdss.watermark_manager import ScanWindow

EXAMPLES_DIR = Path(__file__).parent.parent.parent / "examples" / "checks"

_ALLOWED_OBJECTS = frozenset({"dbo.appointments", "dbo.invoices", "fqb.invoices", "dbo.patient"})


class _PyodbcCursorAdapter:
    """cdss.source.AuditedSourceConnection expects a DB-API connection whose
    cursor().execute() takes a bare Sequence, matching pyodbc's own
    `cursor.execute(sql, params)` signature exactly -- pyodbc.Connection
    already satisfies the Protocol as-is, so this class only exists to give
    each test its own `.timeout` without mutating the shared session-scoped
    connection fixture."""

    def __init__(self, connection: pyodbc.Connection) -> None:
        self._connection = connection
        self.timeout = 0

    def cursor(self) -> pyodbc.Cursor:
        return self._connection.cursor()


def _make_loaded_check(name: str, params: dict[str, object]) -> LoadedCheck:
    raw = yaml.safe_load((EXAMPLES_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
    return LoadedCheck(
        check_id="00000000-0000-0000-0000-000000000001",
        slug=name,
        title=raw["title"],
        category=raw["category"],
        default_severity=raw["default_severity"],
        check_version_id="00000000-0000-0000-0000-000000000002",
        version_number=1,
        definition=raw,
        definition_hash="test-hash",
        affected_views=[raw["entity"]["view"]],
        params_schema={},
        practice_id="practice-1",
        enabled=True,
        demoted=False,
        params=params,
        params_source="default",
    )


@pytest.fixture
def source_conn(fixture_conn: pyodbc.Connection, tmp_path: Path) -> AuditedSourceConnection:
    return AuditedSourceConnection(
        _PyodbcCursorAdapter(fixture_conn),  # type: ignore[arg-type]
        component="test-executor",
        allowed_objects=_ALLOWED_OBJECTS,
        audit_dir=tmp_path,
    )


def test_appointment_completed_no_invoice_counts(source_conn: AuditedSourceConnection) -> None:
    check = _make_loaded_check("appointment-completed-no-invoice", {"invoice_lag_days": 7})
    result = execute_check(source_conn, check)
    assert result.status == "ok"
    assert result.rows_examined == 6
    assert result.n_fail == 1
    assert result.n_pass == 3
    assert result.n_indeterminate == 2


def test_appointment_invalid_status_code_counts(source_conn: AuditedSourceConnection) -> None:
    check = _make_loaded_check("appointment-invalid-status-code", {})
    result = execute_check(source_conn, check)
    assert result.status == "ok"
    assert result.rows_examined == 10
    assert result.n_pass == 8
    assert result.n_fail == 1
    assert result.n_indeterminate == 1


def test_invoice_negative_total_amount_counts(source_conn: AuditedSourceConnection) -> None:
    check = _make_loaded_check("invoice-negative-total-amount", {})
    result = execute_check(source_conn, check)
    assert result.status == "ok"
    assert result.rows_examined == 6
    assert result.n_fail == 1
    assert result.n_pass == 4
    assert result.n_indeterminate == 1


def test_invoice_stale_unpaid_balance_counts(source_conn: AuditedSourceConnection) -> None:
    check = _make_loaded_check("invoice-stale-unpaid-balance", {"stale_days": 60})
    result = execute_check(source_conn, check)
    assert result.status == "ok"
    assert result.rows_examined == 5
    assert result.n_fail == 2
    assert result.n_pass == 2
    assert result.n_indeterminate == 1


def test_patient_active_missing_nhi_counts(source_conn: AuditedSourceConnection) -> None:
    check = _make_loaded_check("patient-active-missing-nhi", {})
    result = execute_check(source_conn, check)
    assert result.status == "ok"
    assert result.rows_examined == 5
    assert result.n_fail == 2
    assert result.n_pass == 2
    assert result.n_indeterminate == 1


def test_patient_no_recent_appointment_counts(source_conn: AuditedSourceConnection) -> None:
    check = _make_loaded_check("patient-no-recent-appointment", {"recall_window_days": 365})
    result = execute_check(source_conn, check)
    assert result.status == "ok"
    assert result.rows_examined == 4
    assert result.n_fail == 1
    assert result.n_pass == 2
    assert result.n_indeterminate == 1


def test_entity_key_and_evidence_are_populated(source_conn: AuditedSourceConnection) -> None:
    check = _make_loaded_check("invoice-negative-total-amount", {})
    result = execute_check(source_conn, check)
    row_by_key = {row.entity_key: row for row in result.rows}
    assert row_by_key[(1,)].tri_state == "fail"
    assert "TotalAmount" in row_by_key[(1,)].evidence
    assert row_by_key[(1,)].evidence["TotalAmount"] == -50.00


# --- watermark scan-window plumbing (live proof) ----------------------------
#
# The fixture DB (Phase 2 step 5) predates the executor and has no column
# named InsertedAt/UpdatedAt on any view -- there's no genuine
# Phase-1-classified watermark column to test against here. `ScheduleDate`
# (a real, varying DATETIME2 column on dbo.Appointments) stands in purely to
# prove the scan_window -> compile_check(watermark_column=...) -> bound
# @watermark_from/@watermark_to plumbing narrows a live result set -- not a
# claim about this view's real watermark semantics.


def test_watermark_scan_window_narrows_results_live(
    source_conn: AuditedSourceConnection,
) -> None:
    check = _make_loaded_check("appointment-completed-no-invoice", {"invoice_lag_days": 7})
    far_future = datetime(2126, 1, 1, tzinfo=UTC)
    scan_window = ScanWindow(from_ts=far_future, to_ts=far_future + timedelta(days=1))

    result = execute_check(
        source_conn, check, watermark_column="ScheduleDate", scan_window=scan_window
    )

    assert result.status == "ok"
    assert result.rows_examined == 0
    assert result.watermark_from == far_future
    assert result.watermark_to == far_future + timedelta(days=1)


def test_watermark_column_without_scan_window_is_not_applied(
    source_conn: AuditedSourceConnection,
) -> None:
    check = _make_loaded_check("appointment-completed-no-invoice", {"invoice_lag_days": 7})

    result = execute_check(source_conn, check, watermark_column="ScheduleDate", scan_window=None)

    assert result.status == "ok"
    assert result.rows_examined == 6  # unwatermarked count, unchanged
    assert result.watermark_from is None
    assert result.watermark_to is None


def test_execution_error_is_captured_not_raised(tmp_path: Path) -> None:
    class _BrokenCursor:
        def execute(self, statement: str, params: object = None) -> None:
            raise RuntimeError("simulated source failure")

        def fetchall(self) -> list[object]:
            return []

    class _BrokenConnection:
        timeout = 0

        def cursor(self) -> _BrokenCursor:
            return _BrokenCursor()

    source = AuditedSourceConnection(
        _BrokenConnection(),  # type: ignore[arg-type]
        component="test-executor",
        allowed_objects=_ALLOWED_OBJECTS,
        audit_dir=tmp_path,
    )
    check = _make_loaded_check("invoice-negative-total-amount", {})

    result = execute_check(source, check)

    assert result.status == "error"
    assert result.error_message is not None
    assert "simulated source failure" in result.error_message
    assert result.rows_examined == 0
    assert result.rows == ()
