"""Phase 3 step 5: preflight schema-drift detection. `compute_missing_columns`
is pure; `fetch_live_columns` needs the fixture SQL Server (LocalDB);
`record_schema_drift_event` needs the app DB. Each skips (never fails) when
its own dependency isn't reachable -- D-009.1.
"""

from __future__ import annotations

import pyodbc
import sqlalchemy as sa

from cdss.executor import compute_missing_columns, fetch_live_columns, record_schema_drift_event

# --- compute_missing_columns (pure) -----------------------------------------


def test_no_drift_when_pinned_is_subset_of_live() -> None:
    pinned = frozenset({"AppointmentID", "PatientID"})
    live = frozenset({"AppointmentID", "PatientID", "NewColumn"})
    assert compute_missing_columns(pinned, live) == frozenset()


def test_drift_when_pinned_column_missing_live() -> None:
    pinned = frozenset({"AppointmentID", "PatientID", "RenamedColumn"})
    live = frozenset({"AppointmentID", "PatientID"})
    assert compute_missing_columns(pinned, live) == frozenset({"RenamedColumn"})


def test_extra_live_column_alone_is_not_drift() -> None:
    pinned = frozenset({"AppointmentID"})
    live = frozenset({"AppointmentID", "BrandNewColumn"})
    assert compute_missing_columns(pinned, live) == frozenset()


def test_identical_column_sets_have_no_drift() -> None:
    columns = frozenset({"AppointmentID", "PatientID"})
    assert compute_missing_columns(columns, columns) == frozenset()


# --- fetch_live_columns (fixture SQL Server) --------------------------------


def test_fetch_live_columns_matches_known_fixture_view(fixture_conn: pyodbc.Connection) -> None:
    from cdss.source import AuditedSourceConnection

    class _Adapter:
        timeout = 0

        def cursor(self) -> pyodbc.Cursor:
            return fixture_conn.cursor()

    source = AuditedSourceConnection(_Adapter(), component="test-drift")  # type: ignore[arg-type]
    columns = fetch_live_columns(source, "dbo.Appointments")
    assert {"AppointmentID", "PatientID", "AppointmentStatus", "IsDeleted"} <= columns


def test_fetch_live_columns_detects_a_genuinely_missing_column(
    fixture_conn: pyodbc.Connection,
) -> None:
    from cdss.source import AuditedSourceConnection

    class _Adapter:
        timeout = 0

        def cursor(self) -> pyodbc.Cursor:
            return fixture_conn.cursor()

    source = AuditedSourceConnection(_Adapter(), component="test-drift")  # type: ignore[arg-type]
    live = fetch_live_columns(source, "dbo.Appointments")
    pinned = live | {"ThisColumnWasRenamedAway"}
    assert compute_missing_columns(pinned, live) == frozenset({"ThisColumnWasRenamedAway"})


# --- record_schema_drift_event (app DB) -------------------------------------


def test_record_schema_drift_event_persists_detail(conn: sa.Connection) -> None:
    run_id = str(conn.execute(sa.text("INSERT INTO runs DEFAULT VALUES RETURNING id")).one().id)
    catalog_version_id = (
        conn.execute(
            sa.text(
                "INSERT INTO catalog_versions (id, sha256, source_path) "
                "VALUES (1, 'deadbeef', 'artifacts/catalog/semantic-catalog-v3.json') "
                "RETURNING id"
            )
        )
        .one()
        .id
    )

    record_schema_drift_event(
        conn,
        run_id,
        "dbo.Appointments",
        catalog_version_id,
        {"missing_columns": ["RenamedColumn"]},
    )

    row = conn.execute(
        sa.text("SELECT view_name, detail FROM schema_drift_events WHERE run_id = :run_id"),
        {"run_id": run_id},
    ).one()
    assert row.view_name == "dbo.Appointments"
    assert row.detail == {"missing_columns": ["RenamedColumn"]}
