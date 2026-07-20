"""Phase 6 step 6: end-to-end feedback loop demonstration -- the brief's own
exit evidence ("simulated feedback drives an observable demotion and a
parameter shift"), scripted against the real fixture SQL Server + real app
DB (D-009.1: skip, never fail, if either is unreachable).

Two independent scenarios, matching the phase spec's own step 6 text:

(A) seeded findings -> a simulated, clearly-labeled synthetic reason-coded
    feedback stream -> precision drops below the D-011 floor for practice A
    only -> demotion observed for A -> the check still fires for practice B.
(B) a percentile-param check recalibrated from its fallback default to a
    real learned value -> the next execution's compiled SQL is *unchanged*
    (D-030: `sql_hash` is a pure function of the DSL definition, never of
    bound param values, so a value-only shift cannot change it) while the
    bound-parameter audit log shows the new value -- the real evidence for
    the spec's own "next run uses the new value" language.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pyodbc
import pytest
import sqlalchemy as sa
import yaml

from cdss.calibration.demotion import run_demotion_job
from cdss.calibration.precision import run_precision_job
from cdss.calibration.recalibrate import recalibrate_check_for_practice
from cdss.check_registry import load_active_checks
from cdss.executor import execute_check
from cdss.feedback import dismiss
from cdss.run import get_or_create_catalog_version, run_once
from cdss.source import AuditedSourceConnection

EXAMPLES_DIR = Path(__file__).parent.parent.parent / "examples" / "checks"
_ALLOWED_OBJECTS = frozenset({"dbo.appointments", "dbo.invoices", "fqb.invoices", "dbo.patient"})

# Same fixture check tests/test_calibration.py and tests/test_recalibrate.py
# already establish: 10 appointment/invoice pairs at PracticeID 200, lag
# 1..10 days -- PERCENTILE_CONT(0.5) => 5.5.
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


@pytest.fixture
def source_conn(fixture_conn: pyodbc.Connection, tmp_path: Path) -> AuditedSourceConnection:
    class _Adapter:
        timeout = 0

        def cursor(self) -> pyodbc.Cursor:
            return fixture_conn.cursor()

    return AuditedSourceConnection(
        _Adapter(),  # type: ignore[arg-type]
        component="test-e2e-feedback-loop",
        allowed_objects=_ALLOWED_OBJECTS,
        audit_dir=tmp_path,
    )


def _seed_practice(conn: sa.Connection, practice_id: str) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO practices (practice_id, name) VALUES (:pid, 'Test Practice') "
            "ON CONFLICT (practice_id) DO NOTHING"
        ),
        {"pid": practice_id},
    )


def _seed_config(
    conn: sa.Connection, *, practice_id: str, check_id: str, params: dict[str, object]
) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO practice_check_config (practice_id, check_id, params) "
            "VALUES (:pid, :check_id, CAST(:params AS jsonb))"
        ),
        {"pid": practice_id, "check_id": check_id, "params": json.dumps(params)},
    )


# --- (A) demotion loop -------------------------------------------------------


def _seed_example_check(conn: sa.Connection, slug: str) -> tuple[str, str]:
    raw = yaml.safe_load((EXAMPLES_DIR / f"{slug}.yaml").read_text(encoding="utf-8"))
    check_id = str(
        conn.execute(
            sa.text(
                "INSERT INTO checks (slug, title, category, default_severity, source, status) "
                "VALUES (:slug, :title, :category, :severity, 'manual', 'active') "
                "RETURNING id"
            ),
            {
                "slug": slug,
                "title": raw["title"],
                "category": raw["category"],
                "severity": raw["default_severity"],
            },
        )
        .one()
        .id
    )
    version_id = str(
        conn.execute(
            sa.text(
                "INSERT INTO check_versions "
                "(check_id, version_number, definition, definition_hash, "
                "affected_views, params_schema) "
                "VALUES (:check_id, 1, CAST(:definition AS jsonb), 'hash', "
                "ARRAY[:view]::text[], '{}'::jsonb) RETURNING id"
            ),
            {"check_id": check_id, "definition": json.dumps(raw), "view": raw["entity"]["view"]},
        )
        .one()
        .id
    )
    return check_id, version_id


def _seed_finding(
    conn: sa.Connection, *, check_id: str, version_id: str, practice_id: str, dedupe_key: str
) -> str:
    run_id = str(conn.execute(sa.text("INSERT INTO runs DEFAULT VALUES RETURNING id")).one().id)
    return str(
        conn.execute(
            sa.text(
                "INSERT INTO findings "
                "(check_id, check_version_id, practice_id, dedupe_key, entity_key, "
                " status, severity, evidence, first_seen_run_id, last_seen_run_id) "
                "VALUES (:check_id, :version_id, :practice_id, :dedupe_key, '{}'::jsonb, "
                " 'open', 'medium', '{}'::jsonb, :run_id, :run_id) RETURNING id"
            ),
            {
                "check_id": check_id,
                "version_id": version_id,
                "practice_id": practice_id,
                "dedupe_key": dedupe_key,
                "run_id": run_id,
            },
        )
        .one()
        .id
    )


def test_demotion_loop_from_synthetic_feedback_to_executor_skip(
    conn: sa.Connection, source_conn: AuditedSourceConnection
) -> None:
    """(A) Every dismissal below is a SYNTHETIC, labeled reason-code event
    (actor='synthetic-feedback-stream') -- not real user feedback -- built
    purely to drive the precision -> demotion pipeline deterministically."""
    slug = "invoice-negative-total-amount"
    check_id, version_id = _seed_example_check(conn, slug)
    _seed_practice(conn, "practice-A")
    _seed_practice(conn, "practice-B")
    _seed_config(conn, practice_id="practice-A", check_id=check_id, params={})
    _seed_config(conn, practice_id="practice-B", check_id=check_id, params={})

    # practice A: 12 'not_genuine' + 3 'genuine_issue' -> precision 3/15 = 0.2,
    # below the 0.30 floor.
    for i in range(12):
        finding_id = _seed_finding(
            conn,
            check_id=check_id,
            version_id=version_id,
            practice_id="practice-A",
            dedupe_key=f"synthetic-a-not-genuine-{i}",
        )
        dismiss(conn, finding_id, reason_code="not_genuine", actor="synthetic-feedback-stream")
    for i in range(3):
        finding_id = _seed_finding(
            conn,
            check_id=check_id,
            version_id=version_id,
            practice_id="practice-A",
            dedupe_key=f"synthetic-a-genuine-{i}",
        )
        dismiss(conn, finding_id, reason_code="genuine_issue", actor="synthetic-feedback-stream")
    # practice B: 13 'genuine_issue' + 2 'not_genuine' -> precision 13/15,
    # comfortably at/above the floor.
    for i in range(13):
        finding_id = _seed_finding(
            conn,
            check_id=check_id,
            version_id=version_id,
            practice_id="practice-B",
            dedupe_key=f"synthetic-b-genuine-{i}",
        )
        dismiss(conn, finding_id, reason_code="genuine_issue", actor="synthetic-feedback-stream")
    for i in range(2):
        finding_id = _seed_finding(
            conn,
            check_id=check_id,
            version_id=version_id,
            practice_id="practice-B",
            dedupe_key=f"synthetic-b-not-genuine-{i}",
        )
        dismiss(conn, finding_id, reason_code="not_genuine", actor="synthetic-feedback-stream")

    # --- precision drops below the floor for practice A only ---
    precision_results = run_precision_job(conn)
    by_practice = {r.practice_id: r for r in precision_results}
    precision_a = by_practice["practice-A"].precision
    precision_b = by_practice["practice-B"].precision
    assert precision_a is not None and precision_b is not None
    assert precision_a == Decimal(3) / Decimal(15)
    assert precision_a < Decimal("0.30")
    assert precision_b == Decimal(13) / Decimal(15)
    assert precision_b >= Decimal("0.30")

    # --- demotion observed for A only ---
    demotion_results = run_demotion_job(conn)
    demoted_pairs = {(r.practice_id, r.check_id) for r in demotion_results if r.demoted_this_run}
    assert ("practice-A", check_id) in demoted_pairs
    assert ("practice-B", check_id) not in demoted_pairs
    config = {
        row.practice_id: row.demoted
        for row in conn.execute(
            sa.text("SELECT practice_id, demoted FROM practice_check_config WHERE check_id = :cid"),
            {"cid": check_id},
        )
    }
    assert config["practice-A"] is True
    assert config["practice-B"] is False

    # --- executor skips the demoted pair for A, still fires for B ---
    checks = load_active_checks(conn)
    catalog_version_id = get_or_create_catalog_version(
        conn, sha256="e2e-feedback-loop-hash", source_path="e2e-feedback-loop"
    )
    report = run_once(
        conn, source_conn, checks, catalog_version_id=catalog_version_id, watermark_plans={}
    )
    pairs = {(s.slug, s.practice_id) for s in report.summaries}
    assert (slug, "practice-A") not in pairs
    assert (slug, "practice-B") in pairs


# --- (B) recalibration loop --------------------------------------------------


def _seed_percentile_check(conn: sa.Connection) -> str:
    check_id = str(
        conn.execute(
            sa.text(
                "INSERT INTO checks (slug, title, category, default_severity, source, status) "
                "VALUES ('e2e-invoice-lag-check', 'Test', 'revenue-integrity', 'medium', "
                "'manual', 'active') RETURNING id"
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
            "VALUES (:check_id, 1, CAST(:definition AS jsonb), 'hash', "
            "ARRAY['dbo.Appointments','dbo.Invoices']::text[], '{}'::jsonb)"
        ),
        {"check_id": check_id, "definition": json.dumps(_INVOICE_LAG_DEFINITION)},
    )
    return check_id


def test_recalibration_loop_shifts_bound_param_but_not_sql_hash(
    conn: sa.Connection, source_conn: AuditedSourceConnection, tmp_path: Path
) -> None:
    """(B) D-030: the compiled SQL's `sql_hash` is identical before and
    after recalibration (it depends only on the DSL definition, which never
    changes here) -- the bound-parameter audit log is the real evidence
    the next execution actually uses the newly learned value."""
    check_id = _seed_percentile_check(conn)
    _seed_practice(conn, "200")
    _seed_config(conn, practice_id="200", check_id=check_id, params={"invoice_lag_days": 999})

    loaded_before = next(
        c for c in load_active_checks(conn, practice_id="200") if c.check_id == check_id
    )
    assert loaded_before.params == {"invoice_lag_days": 999}
    result_before = execute_check(source_conn, loaded_before)

    recalibrate_check_for_practice(source_conn, conn, loaded_before)

    loaded_after = next(
        c for c in load_active_checks(conn, practice_id="200") if c.check_id == check_id
    )
    assert loaded_after.params == {"invoice_lag_days": 5.5}
    result_after = execute_check(source_conn, loaded_after)

    # the compiled SQL structure never changed -- only the bound value did.
    assert result_before.sql_hash == result_after.sql_hash

    audit_events = [
        json.loads(line)
        for path in sorted(tmp_path.glob("source-audit-*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(999 in event["params"] for event in audit_events)
    assert any(5.5 in event["params"] for event in audit_events)
