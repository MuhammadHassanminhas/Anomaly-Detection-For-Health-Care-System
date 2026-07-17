"""Phase 3 step 5: `runs` + `check_executions` bookkeeping. Requires
CDSS_APP_DB_URL and skips (never fails) otherwise -- D-009.1. Every test
runs inside its own rolled-back transaction (the shared `conn` fixture).
"""

from __future__ import annotations

import sqlalchemy as sa

from cdss.executor import CheckExecutionResult, create_run, finish_run, record_check_execution


def _seed_check_and_practice(conn: sa.Connection) -> tuple[str, str, str]:
    check_id = str(
        conn.execute(
            sa.text(
                "INSERT INTO checks (slug, title, category, default_severity, source, status) "
                "VALUES ('test-check', 'Test', 'data-quality', 'medium', 'manual', 'active') "
                "RETURNING id"
            )
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
                "VALUES (:check_id, 1, '{}'::jsonb, 'hash', ARRAY[]::text[], '{}'::jsonb) "
                "RETURNING id"
            ),
            {"check_id": check_id},
        )
        .one()
        .id
    )
    conn.execute(
        sa.text("INSERT INTO practices (practice_id, name) VALUES ('practice-1', 'Test Practice')")
    )
    return check_id, version_id, "practice-1"


def test_create_run_returns_an_id(conn: sa.Connection) -> None:
    run_id = create_run(conn)
    row = conn.execute(sa.text("SELECT status FROM runs WHERE id = :id"), {"id": run_id}).one()
    assert row.status == "running"


def test_finish_run_updates_status_and_finished_at(conn: sa.Connection) -> None:
    run_id = create_run(conn)
    finish_run(conn, run_id, status="completed")
    row = conn.execute(
        sa.text("SELECT status, finished_at FROM runs WHERE id = :id"), {"id": run_id}
    ).one()
    assert row.status == "completed"
    assert row.finished_at is not None


def test_record_check_execution_persists_counts_and_status(conn: sa.Connection) -> None:
    check_id, version_id, practice_id = _seed_check_and_practice(conn)
    run_id = create_run(conn)
    result = CheckExecutionResult(
        check_id=check_id,
        check_version_id=version_id,
        practice_id=practice_id,
        sql_hash="deadbeef",
        watermark_from=None,
        watermark_to=None,
        duration_ms=42,
        rows_examined=10,
        n_pass=6,
        n_fail=3,
        n_indeterminate=1,
        status="ok",
        error_message=None,
        rows=(),
    )

    execution_id = record_check_execution(conn, run_id, result)

    row = conn.execute(
        sa.text("SELECT * FROM check_executions WHERE id = :id"), {"id": execution_id}
    ).one()
    assert row.run_id is not None
    assert str(row.check_id) == check_id
    assert row.sql_hash == "deadbeef"
    assert row.rows_examined == 10
    assert row.n_pass == 6
    assert row.n_fail == 3
    assert row.n_indeterminate == 1
    assert row.status == "ok"
    assert row.error_message is None


def test_record_check_execution_persists_error_status(conn: sa.Connection) -> None:
    check_id, version_id, practice_id = _seed_check_and_practice(conn)
    run_id = create_run(conn)
    result = CheckExecutionResult(
        check_id=check_id,
        check_version_id=version_id,
        practice_id=practice_id,
        sql_hash="deadbeef",
        watermark_from=None,
        watermark_to=None,
        duration_ms=5,
        rows_examined=0,
        n_pass=0,
        n_fail=0,
        n_indeterminate=0,
        status="error",
        error_message="boom",
        rows=(),
    )

    execution_id = record_check_execution(conn, run_id, result)

    row = conn.execute(
        sa.text("SELECT status, error_message FROM check_executions WHERE id = :id"),
        {"id": execution_id},
    ).one()
    assert row.status == "error"
    assert row.error_message == "boom"
