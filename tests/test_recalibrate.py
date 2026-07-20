"""Phase 6 step 4: `cdss.calibration.recalibrate` -- F4 scheduled
recalibration. `test_recalibrate_check_for_practice_*`/`test_run_recalibration_job_*`
need both the fixture SQL Server (D-026) and the app DB (D-009.1) --
skip, never fail, if either is unreachable.
"""

from __future__ import annotations

import json

import pyodbc
import pytest
import sqlalchemy as sa

from cdss.calibration.recalibrate import (
    recalibrate_check_for_practice,
    run_recalibration_job,
)
from cdss.check_registry import LoadedCheck, load_active_checks
from cdss.source import AuditedSourceConnection

# Same fixture check the fixture SQL Server has real rows for
# (tests/test_calibration.py's own fixture: 10 appointment/invoice pairs at
# PracticeID 200, lag 1..10 days -- PERCENTILE_CONT(0.5) => 5.5).
_INVOICE_LAG_DEFINITION = {
    "id": "appointment-completed-no-invoice",
    "title": "Completed appointment has no invoice",
    "category": "revenue-integrity",
    "default_severity": "medium",
    "entity": {
        "view": "dbo.Appointments",
        "key": ["AppointmentID"],
        "practice_column": "PracticeID",
        "base_filters": ["IsDeleted = 0", "IsDummy = 0"],
    },
    "params": {
        "invoice_lag_days": {
            "type": "integer",
            "default": {
                "strategy": "percentile",
                "measure": "appointment_to_invoice_lag",
                "p": 50,
                "fallback": 999,
            },
        }
    },
    "prerequisites": ["AppointmentCompleted IS NOT NULL", "ScheduleDate IS NOT NULL"],
    "predicate": {
        "all": [
            "AppointmentCompleted = 1",
            "ScheduleDate <= DATEADD(day, -{invoice_lag_days}, sysdatetime())",
            {
                "not_exists": {
                    "view": "dbo.Invoices",
                    "on": "dbo.Invoices.AppointmentID = dbo.Appointments.AppointmentID",
                    "where": "dbo.Invoices.IsActive = 1",
                }
            },
        ]
    },
    "evidence": ["AppointmentID", "PatientID", "ScheduleDate", "PracticeID"],
    "actions": ["verify-invoice"],
    "resolution": "An active invoice exists for the appointment, or the finding is dismissed.",
}

_FIXED_ONLY_DEFINITION = {
    **_INVOICE_LAG_DEFINITION,
    "params": {"stale_days": {"type": "integer", "default": {"strategy": "fixed", "value": 60}}},
}


@pytest.fixture
def source_conn(fixture_conn: pyodbc.Connection) -> AuditedSourceConnection:
    class _Adapter:
        timeout = 0

        def cursor(self) -> pyodbc.Cursor:
            return fixture_conn.cursor()

    return AuditedSourceConnection(
        _Adapter(),  # type: ignore[arg-type]
        component="test-recalibrate",
        allowed_objects=frozenset({"dbo.appointments", "fqb.invoices"}),
    )


def _seed_check(conn: sa.Connection, *, slug: str, definition: dict[str, object]) -> str:
    check_id = str(
        conn.execute(
            sa.text(
                "INSERT INTO checks (slug, title, category, default_severity, source, status) "
                "VALUES (:slug, 'Test', 'revenue-integrity', 'medium', 'manual', 'active') "
                "RETURNING id"
            ),
            {"slug": slug},
        )
        .one()
        .id
    )
    conn.execute(
        sa.text(
            "INSERT INTO check_versions "
            "(check_id, version_number, definition, definition_hash, "
            "affected_views, params_schema) "
            "VALUES (:check_id, 1, CAST(:definition AS jsonb), 'hash', "
            "ARRAY['dbo.Appointments','dbo.Invoices']::text[], '{}'::jsonb)"
        ),
        {"check_id": check_id, "definition": json.dumps(definition)},
    )
    return check_id


def _seed_practice(conn: sa.Connection, practice_id: str) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO practices (practice_id, name) VALUES (:pid, 'Test Practice') "
            "ON CONFLICT (practice_id) DO NOTHING"
        ),
        {"pid": practice_id},
    )


def _seed_config(
    conn: sa.Connection,
    *,
    practice_id: str,
    check_id: str,
    params: dict[str, object] | None = None,
    params_source: str = "default",
) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO practice_check_config (practice_id, check_id, params, params_source) "
            "VALUES (:pid, :check_id, CAST(:params AS jsonb), :params_source)"
        ),
        {
            "pid": practice_id,
            "check_id": check_id,
            "params": json.dumps(params or {}),
            "params_source": params_source,
        },
    )


def _loaded_check(conn: sa.Connection, *, check_id: str, practice_id: str) -> LoadedCheck:
    (match,) = [
        c for c in load_active_checks(conn, practice_id=practice_id) if c.check_id == check_id
    ]
    return match


def test_recalibrate_skips_manual_params_without_writing(
    conn: sa.Connection, source_conn: AuditedSourceConnection
) -> None:
    check_id = _seed_check(conn, slug="recal-manual-check", definition=_INVOICE_LAG_DEFINITION)
    _seed_practice(conn, "200")
    _seed_config(
        conn,
        practice_id="200",
        check_id=check_id,
        params={"invoice_lag_days": 42},
        params_source="manual",
    )
    check = _loaded_check(conn, check_id=check_id, practice_id="200")

    result = recalibrate_check_for_practice(source_conn, conn, check)

    assert result.applied is False
    assert result.learned_params == ()
    row = conn.execute(
        sa.text(
            "SELECT params, params_source FROM practice_check_config "
            "WHERE practice_id = '200' AND check_id = :check_id"
        ),
        {"check_id": check_id},
    ).one()
    assert row.params == {"invoice_lag_days": 42}
    assert row.params_source == "manual"
    run_count = conn.execute(
        sa.text(
            "SELECT count(*) FROM calibration_runs WHERE practice_id = '200' "
            "AND check_id = :check_id"
        ),
        {"check_id": check_id},
    ).scalar_one()
    assert run_count == 0


def test_recalibrate_applies_and_records_before_after_when_not_manual(
    conn: sa.Connection, source_conn: AuditedSourceConnection
) -> None:
    check_id = _seed_check(conn, slug="recal-default-check", definition=_INVOICE_LAG_DEFINITION)
    _seed_practice(conn, "200")
    _seed_config(conn, practice_id="200", check_id=check_id, params_source="default")
    check = _loaded_check(conn, check_id=check_id, practice_id="200")

    result = recalibrate_check_for_practice(source_conn, conn, check)

    assert result.applied is True
    assert len(result.learned_params) == 1
    learned = result.learned_params[0]
    assert learned.learned is True
    assert learned.value == 5.5
    assert learned.sample_size == 10

    row = conn.execute(
        sa.text(
            "SELECT params, params_source FROM practice_check_config "
            "WHERE practice_id = '200' AND check_id = :check_id"
        ),
        {"check_id": check_id},
    ).one()
    assert row.params == {"invoice_lag_days": 5.5}
    assert row.params_source == "calibrated"

    run_row = conn.execute(
        sa.text(
            "SELECT params_before, params_after FROM calibration_runs "
            "WHERE practice_id = '200' AND check_id = :check_id"
        ),
        {"check_id": check_id},
    ).one()
    assert run_row.params_before == {}
    assert run_row.params_after == {"invoice_lag_days": 5.5}

    # the deliverable's own "next compiled execution uses the new value" bar
    # -- the loader (what the compiler consumes) now sees the shifted param.
    reloaded = _loaded_check(conn, check_id=check_id, practice_id="200")
    assert reloaded.params == {"invoice_lag_days": 5.5}


def test_run_recalibration_job_only_touches_pairs_with_a_percentile_param(
    conn: sa.Connection, source_conn: AuditedSourceConnection
) -> None:
    percentile_check_id = _seed_check(
        conn, slug="recal-job-percentile-check", definition=_INVOICE_LAG_DEFINITION
    )
    fixed_check_id = _seed_check(
        conn, slug="recal-job-fixed-check", definition=_FIXED_ONLY_DEFINITION
    )
    _seed_practice(conn, "200")
    _seed_config(conn, practice_id="200", check_id=percentile_check_id, params_source="default")
    _seed_config(conn, practice_id="200", check_id=fixed_check_id, params_source="default")

    results = run_recalibration_job(source_conn, conn)

    assert len(results) == 1
    assert results[0].slug == "recal-job-percentile-check"
    assert results[0].applied is True


def test_run_recalibration_job_skips_system_checks(
    conn: sa.Connection, source_conn: AuditedSourceConnection
) -> None:
    system_check_id = str(
        conn.execute(
            sa.text(
                "INSERT INTO checks (slug, title, category, default_severity, source, status) "
                "VALUES ('system-indeterminate-rate', 'Indeterminate rate', 'data-quality', "
                "'medium', 'manual', 'active') RETURNING id"
            )
        )
        .one()
        .id
    )
    conn.execute(
        sa.text(
            "INSERT INTO check_versions "
            "(check_id, version_number, definition, definition_hash, "
            "affected_views, params_schema) "
            "VALUES (:check_id, 1, '{\"kind\": \"system\"}'::jsonb, 'hash', "
            "ARRAY[]::text[], '{}'::jsonb)"
        ),
        {"check_id": system_check_id},
    )
    _seed_practice(conn, "200")
    _seed_config(conn, practice_id="200", check_id=system_check_id, params_source="default")

    results = run_recalibration_job(source_conn, conn)

    assert results == []
