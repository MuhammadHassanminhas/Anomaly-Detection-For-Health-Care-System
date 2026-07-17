"""Phase 3 step 5 deliverable, full pipeline: cdss.check_registry loads
checks seeded in the app DB, cdss.executor compiles/binds/executes each
against the live fixture DB and records check_executions -- proving the
loader and executor are actually wired together, not just each individually
correct. Also proves the preflight drift path: a drifted check is skipped
and recorded as such, and the run continues normally for every other check.

Requires both the app DB (CDSS_APP_DB_URL) and the fixture SQL Server
(LocalDB) -- skips (never fails) if either is unreachable, D-009.1.
"""

from __future__ import annotations

import json
from pathlib import Path

import pyodbc
import pytest
import sqlalchemy as sa
import yaml

from cdss.check_registry import load_active_checks
from cdss.executor import (
    create_run,
    execute_check_with_preflight,
    fetch_live_columns,
    finish_run,
)
from cdss.source import AuditedSourceConnection

EXAMPLES_DIR = Path(__file__).parent.parent.parent / "examples" / "checks"
_ALLOWED_OBJECTS = frozenset({"dbo.appointments", "dbo.invoices", "fqb.invoices", "dbo.patient"})

# name -> (params, expected counts)
# Phase 4 step 5 widened the fixture DB (new dbo.Appointments/dbo.Patient/
# fqb.Invoices rows for the LLM-drafted check fixture suite) -- every check
# below whose base_filters don't exclude those new rows now examines more of
# them; appointment-completed-no-invoice is unaffected (its own IsDummy = 0
# base filter excludes every new dbo.Appointments row, all of which use
# IsDummy = 1).
_EXAMPLES: dict[str, tuple[dict[str, object], dict[str, int]]] = {
    "appointment-completed-no-invoice": (
        {"invoice_lag_days": 7},
        {"rows_examined": 6, "n_fail": 1, "n_pass": 3, "n_indeterminate": 2},
    ),
    "appointment-invalid-status-code": (
        {},
        {"rows_examined": 29, "n_pass": 21, "n_fail": 5, "n_indeterminate": 3},
    ),
    "invoice-negative-total-amount": (
        {},
        {"rows_examined": 17, "n_fail": 1, "n_pass": 15, "n_indeterminate": 1},
    ),
    "invoice-stale-unpaid-balance": (
        {"stale_days": 60},
        {"rows_examined": 16, "n_fail": 2, "n_pass": 13, "n_indeterminate": 1},
    ),
    "patient-active-missing-nhi": (
        {},
        {"rows_examined": 6, "n_fail": 2, "n_pass": 3, "n_indeterminate": 1},
    ),
    "patient-no-recent-appointment": (
        {"recall_window_days": 365},
        {"rows_examined": 5, "n_fail": 1, "n_pass": 3, "n_indeterminate": 1},
    ),
}


def _seed_all_examples(conn: sa.Connection) -> None:
    conn.execute(
        sa.text("INSERT INTO practices (practice_id, name) VALUES ('practice-1', 'Test Practice')")
    )
    conn.execute(
        sa.text(
            "INSERT INTO catalog_versions (id, sha256, source_path) "
            "VALUES (1, 'test-hash', 'test-path')"
        )
    )
    for name, (params, _) in _EXAMPLES.items():
        raw = yaml.safe_load((EXAMPLES_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
        check_id = str(
            conn.execute(
                sa.text(
                    "INSERT INTO checks (slug, title, category, default_severity, source, status) "
                    "VALUES (:slug, :title, :category, :severity, 'manual', 'active') "
                    "RETURNING id"
                ),
                {
                    "slug": name,
                    "title": raw["title"],
                    "category": raw["category"],
                    "severity": raw["default_severity"],
                },
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
                "ARRAY[:view]::text[], '{}'::jsonb)"
            ),
            {
                "check_id": check_id,
                "definition": json.dumps(raw),
                "view": raw["entity"]["view"],
            },
        )
        conn.execute(
            sa.text(
                "INSERT INTO practice_check_config (practice_id, check_id, params) "
                "VALUES ('practice-1', :check_id, CAST(:params AS jsonb))"
            ),
            {"check_id": check_id, "params": json.dumps(params)},
        )


@pytest.fixture
def source_conn(fixture_conn: pyodbc.Connection, tmp_path: Path) -> AuditedSourceConnection:
    class _Adapter:
        timeout = 0

        def cursor(self) -> pyodbc.Cursor:
            return fixture_conn.cursor()

    return AuditedSourceConnection(
        _Adapter(),  # type: ignore[arg-type]
        component="test-end-to-end",
        allowed_objects=_ALLOWED_OBJECTS,
        audit_dir=tmp_path,
    )


def test_loader_and_executor_wired_together_match_expected_counts(
    conn: sa.Connection, source_conn: AuditedSourceConnection
) -> None:
    _seed_all_examples(conn)
    loaded = load_active_checks(conn)
    assert len(loaded) == 6

    run_id = create_run(conn)
    for loaded_check in loaded:
        expected = _EXAMPLES[loaded_check.slug][1]
        driving_view = loaded_check.affected_views[0]
        pinned_columns = fetch_live_columns(source_conn, driving_view)

        result = execute_check_with_preflight(
            conn, source_conn, run_id, loaded_check, driving_view, pinned_columns, 1
        )

        assert result.status == "ok", f"{loaded_check.slug}: {result.error_message}"
        assert result.rows_examined == expected["rows_examined"], loaded_check.slug
        assert result.n_fail == expected["n_fail"], loaded_check.slug
        assert result.n_pass == expected["n_pass"], loaded_check.slug
        assert result.n_indeterminate == expected["n_indeterminate"], loaded_check.slug
    finish_run(conn, run_id, status="completed")

    rows = conn.execute(
        sa.text("SELECT status FROM check_executions WHERE run_id = :run_id"), {"run_id": run_id}
    ).all()
    assert len(rows) == 6
    assert all(row.status == "ok" for row in rows)


def test_drifted_check_is_skipped_but_the_run_continues(
    conn: sa.Connection, source_conn: AuditedSourceConnection
) -> None:
    _seed_all_examples(conn)
    loaded = {c.slug: c for c in load_active_checks(conn)}
    run_id = create_run(conn)

    drifted_check = loaded["patient-active-missing-nhi"]
    live_columns = fetch_live_columns(source_conn, "dbo.Patient")
    bogus_pinned = live_columns | {"ThisColumnWasRenamedAway"}

    drift_result = execute_check_with_preflight(
        conn, source_conn, run_id, drifted_check, "dbo.Patient", bogus_pinned, 1
    )
    assert drift_result.status == "skipped_drift"
    assert drift_result.rows_examined == 0

    drift_events = conn.execute(
        sa.text("SELECT view_name, detail FROM schema_drift_events WHERE run_id = :run_id"),
        {"run_id": run_id},
    ).all()
    assert len(drift_events) == 1
    assert drift_events[0].view_name == "dbo.Patient"
    assert drift_events[0].detail == {"missing_columns": ["ThisColumnWasRenamedAway"]}

    # The run continues: a different check, same run, executes normally.
    healthy_check = loaded["invoice-negative-total-amount"]
    healthy_pinned = fetch_live_columns(source_conn, "fqb.Invoices")
    healthy_result = execute_check_with_preflight(
        conn, source_conn, run_id, healthy_check, "fqb.Invoices", healthy_pinned, 1
    )
    assert healthy_result.status == "ok"
    assert healthy_result.rows_examined == 17

    execution_rows = conn.execute(
        sa.text("SELECT status FROM check_executions WHERE run_id = :run_id"), {"run_id": run_id}
    ).all()
    assert sorted(row.status for row in execution_rows) == ["ok", "skipped_drift"]
