"""Phase 4 step 6: cdss.calibration.learn_defaults -- F4 per-practice
parameter learning. `test_measure_*` needs only the fixture SQL Server
(D-026); `test_learn_defaults_for_check_*` needs both the fixture DB and the
app DB (D-009.1) -- skip, never fail, if either is unreachable.
"""

from __future__ import annotations

import json

import pyodbc
import pytest
import sqlalchemy as sa

from cdss.calibration import (
    MEASURE_REGISTRY,
    MIN_SAMPLE_SIZE,
    UnknownMeasureError,
    learn_defaults_for_check,
)
from cdss.source import AuditedSourceConnection

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

_UNKNOWN_MEASURE_DEFINITION = {
    **_INVOICE_LAG_DEFINITION,
    "params": {
        "invoice_lag_days": {
            "type": "integer",
            "default": {
                "strategy": "percentile",
                "measure": "does_not_exist",
                "p": 50,
                "fallback": 7,
            },
        }
    },
}


def _seed_check(conn: sa.Connection, *, slug: str = "calibration-test-check") -> str:
    row = conn.execute(
        sa.text(
            "INSERT INTO checks (slug, title, category, default_severity, source, status) "
            "VALUES (:slug, 'Test Check', 'revenue-integrity', 'medium', 'manual', 'active') "
            "RETURNING id"
        ),
        {"slug": slug},
    ).one()
    return str(row.id)


def _seed_practice(conn: sa.Connection, practice_id: str) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO practices (practice_id, name) VALUES (:pid, 'Test Practice') "
            "ON CONFLICT (practice_id) DO NOTHING"
        ),
        {"pid": practice_id},
    )


@pytest.fixture
def source_conn(fixture_conn: pyodbc.Connection) -> AuditedSourceConnection:
    class _Adapter:
        timeout = 0

        def cursor(self) -> pyodbc.Cursor:
            return fixture_conn.cursor()

    return AuditedSourceConnection(
        _Adapter(),  # type: ignore[arg-type]
        component="test-calibration",
        allowed_objects=frozenset({"dbo.appointments", "fqb.invoices"}),
    )


# --- measure registry ---------------------------------------------------------


def test_appointment_to_invoice_lag_matches_hand_computed_median(
    source_conn: AuditedSourceConnection,
) -> None:
    # Fixture rows: 10 pairs with lag exactly 1..10 days (PracticeID 200) --
    # PERCENTILE_CONT(0.5) of 1..10 interpolates to the median, 5.5.
    measure_fn = MEASURE_REGISTRY["appointment_to_invoice_lag"]
    distribution = measure_fn(source_conn, "200", 50)
    assert distribution.sample_size == 10
    assert distribution.percentile_value == 5.5


def test_appointment_to_invoice_lag_zero_sample_for_unknown_practice(
    source_conn: AuditedSourceConnection,
) -> None:
    measure_fn = MEASURE_REGISTRY["appointment_to_invoice_lag"]
    distribution = measure_fn(source_conn, "999999", 50)
    assert distribution.sample_size == 0
    assert distribution.percentile_value is None


# --- learn_defaults_for_check (DB-gated: app DB + fixture DB) ----------------


def test_learn_defaults_for_check_learns_when_sample_is_sufficient(
    conn: sa.Connection, source_conn: AuditedSourceConnection
) -> None:
    assert MIN_SAMPLE_SIZE <= 10
    check_id = _seed_check(conn, slug="calibration-learn-check")
    _seed_practice(conn, "200")

    results = learn_defaults_for_check(
        source_conn, conn, check_id=check_id, practice_id="200", definition=_INVOICE_LAG_DEFINITION
    )

    assert len(results) == 1
    learned = results[0]
    assert learned.param_name == "invoice_lag_days"
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


def test_learn_defaults_for_check_falls_back_when_sample_is_insufficient(
    conn: sa.Connection, source_conn: AuditedSourceConnection
) -> None:
    check_id = _seed_check(conn, slug="calibration-fallback-check")
    _seed_practice(conn, "999999")

    results = learn_defaults_for_check(
        source_conn,
        conn,
        check_id=check_id,
        practice_id="999999",
        definition=_INVOICE_LAG_DEFINITION,
    )

    assert len(results) == 1
    learned = results[0]
    assert learned.learned is False
    assert learned.value == 999.0
    assert learned.sample_size == 0

    row = conn.execute(
        sa.text(
            "SELECT params, params_source FROM practice_check_config "
            "WHERE practice_id = '999999' AND check_id = :check_id"
        ),
        {"check_id": check_id},
    ).one()
    assert row.params == {"invoice_lag_days": 999.0}
    assert row.params_source == "default"


def test_learn_defaults_for_check_preserves_existing_param_keys(
    conn: sa.Connection, source_conn: AuditedSourceConnection
) -> None:
    check_id = _seed_check(conn, slug="calibration-preserve-check")
    _seed_practice(conn, "200")
    conn.execute(
        sa.text(
            "INSERT INTO practice_check_config (practice_id, check_id, params, params_source) "
            "VALUES ('200', :check_id, CAST(:params AS jsonb), 'manual')"
        ),
        {"check_id": check_id, "params": json.dumps({"unrelated_param": "kept"})},
    )

    learn_defaults_for_check(
        source_conn, conn, check_id=check_id, practice_id="200", definition=_INVOICE_LAG_DEFINITION
    )

    row = conn.execute(
        sa.text(
            "SELECT params FROM practice_check_config "
            "WHERE practice_id = '200' AND check_id = :check_id"
        ),
        {"check_id": check_id},
    ).one()
    assert row.params == {"unrelated_param": "kept", "invoice_lag_days": 5.5}


def test_learn_defaults_for_check_returns_empty_for_no_percentile_params(
    conn: sa.Connection, source_conn: AuditedSourceConnection
) -> None:
    check_id = _seed_check(conn, slug="calibration-fixed-only-check")
    _seed_practice(conn, "200")

    results = learn_defaults_for_check(
        source_conn, conn, check_id=check_id, practice_id="200", definition=_FIXED_ONLY_DEFINITION
    )

    assert results == []
    count = conn.execute(
        sa.text(
            "SELECT count(*) FROM practice_check_config "
            "WHERE practice_id = '200' AND check_id = :check_id"
        ),
        {"check_id": check_id},
    ).scalar_one()
    assert count == 0


def test_learn_defaults_for_check_raises_naming_unknown_measure(
    conn: sa.Connection, source_conn: AuditedSourceConnection
) -> None:
    check_id = _seed_check(conn, slug="calibration-unknown-measure-check")
    _seed_practice(conn, "200")

    with pytest.raises(UnknownMeasureError, match="does_not_exist"):
        learn_defaults_for_check(
            source_conn,
            conn,
            check_id=check_id,
            practice_id="200",
            definition=_UNKNOWN_MEASURE_DEFINITION,
        )
