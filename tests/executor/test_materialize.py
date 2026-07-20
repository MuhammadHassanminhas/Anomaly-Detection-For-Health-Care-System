"""Phase 3 step 6: finding materialization. Pure tests for the dedupe-key
primitives need no DB; the transition/idempotency tests require
CDSS_APP_DB_URL and skip (never fail) otherwise -- D-009.1, via the shared
`conn` fixture (its own rolled-back transaction per test).
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa

from cdss.executor import CheckExecutionResult, ExecutedRow, create_run
from cdss.materialize import (
    CreatedFinding,
    MaterializationStats,
    canonical_entity_key,
    compute_dedupe_key,
    materialize_check_result,
)

# --- pure: dedupe-key primitives --------------------------------------------


def test_canonical_entity_key_pairs_columns_with_values() -> None:
    assert canonical_entity_key(("AppointmentID", "PracticeID"), (7, "p1")) == {
        "AppointmentID": 7,
        "PracticeID": "p1",
    }


def test_compute_dedupe_key_deterministic() -> None:
    key = {"AppointmentID": 7}
    assert compute_dedupe_key("check-1", key) == compute_dedupe_key("check-1", key)


def test_compute_dedupe_key_differs_by_check_id() -> None:
    key = {"AppointmentID": 7}
    assert compute_dedupe_key("check-1", key) != compute_dedupe_key("check-2", key)


def test_compute_dedupe_key_differs_by_entity_key_value() -> None:
    assert compute_dedupe_key("check-1", {"AppointmentID": 7}) != compute_dedupe_key(
        "check-1", {"AppointmentID": 8}
    )


def test_compute_dedupe_key_stable_across_dict_insertion_order() -> None:
    a = {"AppointmentID": 7, "PracticeID": "p1"}
    b = {"PracticeID": "p1", "AppointmentID": 7}
    assert compute_dedupe_key("check-1", a) == compute_dedupe_key("check-1", b)


def test_compute_dedupe_key_handles_non_json_native_values() -> None:
    key = {"AppointmentID": 7, "ScheduleDate": datetime(2026, 1, 1, tzinfo=UTC)}
    digest = compute_dedupe_key("check-1", key)
    assert isinstance(digest, str) and len(digest) == 64


# --- DB-gated: materialization transitions ----------------------------------


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


def _result(
    check_id: str, version_id: str, practice_id: str, rows: tuple[ExecutedRow, ...]
) -> CheckExecutionResult:
    return CheckExecutionResult(
        check_id=check_id,
        check_version_id=version_id,
        practice_id=practice_id,
        sql_hash="deadbeef",
        watermark_from=None,
        watermark_to=None,
        duration_ms=1,
        rows_examined=len(rows),
        n_pass=sum(1 for r in rows if r.tri_state == "pass"),
        n_fail=sum(1 for r in rows if r.tri_state == "fail"),
        n_indeterminate=sum(1 for r in rows if r.tri_state == "indeterminate"),
        status="ok",
        error_message=None,
        rows=rows,
    )


def _finding_row(conn: sa.Connection, check_id: str, dedupe_key: str) -> sa.Row[tuple[object, ...]]:
    return conn.execute(
        sa.text("SELECT * FROM findings WHERE check_id = :check_id AND dedupe_key = :dedupe_key"),
        {"check_id": check_id, "dedupe_key": dedupe_key},
    ).one()


def _events(conn: sa.Connection, finding_id: object) -> list[str]:
    rows = conn.execute(
        sa.text("SELECT event FROM finding_events WHERE finding_id = :id ORDER BY occurred_at"),
        {"id": finding_id},
    ).all()
    return [r.event for r in rows]


def test_new_fail_row_creates_open_finding_with_created_event(conn: sa.Connection) -> None:
    check_id, version_id, practice_id = _seed_check_and_practice(conn)
    run_id = create_run(conn)
    row = ExecutedRow(entity_key=(7,), tri_state="fail", evidence={"Status": "cancelled"})
    result = _result(check_id, version_id, practice_id, (row,))

    stats = materialize_check_result(
        conn,
        run_id,
        result,
        entity_key_columns=("AppointmentID",),
        severity="medium",
        auto_resolve=False,
    )

    assert stats.created == 1
    dedupe_key = compute_dedupe_key(check_id, {"AppointmentID": 7})
    finding = _finding_row(conn, check_id, dedupe_key)
    assert finding.status == "open"
    assert str(finding.first_seen_run_id) == run_id
    assert str(finding.last_seen_run_id) == run_id
    assert finding.evidence == {"Status": "cancelled"}
    assert _events(conn, finding.id) == ["created"]
    # Phase 5 step 7's own hook: a genuinely new finding is reported back so
    # the caller (cdss.run) can narrate it inline, without a second query.
    assert stats.created_findings == (
        CreatedFinding(finding_id=str(finding.id), evidence={"Status": "cancelled"}),
    )


def test_reseen_fail_row_bumps_last_seen_and_emits_reseen_event(conn: sa.Connection) -> None:
    check_id, version_id, practice_id = _seed_check_and_practice(conn)
    run_1 = create_run(conn)
    row = ExecutedRow(entity_key=(7,), tri_state="fail", evidence={"Status": "cancelled"})
    result = _result(check_id, version_id, practice_id, (row,))
    materialize_check_result(
        conn,
        run_1,
        result,
        entity_key_columns=("AppointmentID",),
        severity="medium",
        auto_resolve=False,
    )

    run_2 = create_run(conn)
    stats = materialize_check_result(
        conn,
        run_2,
        result,
        entity_key_columns=("AppointmentID",),
        severity="medium",
        auto_resolve=False,
    )

    assert stats.reseen == 1
    assert stats.created == 0
    dedupe_key = compute_dedupe_key(check_id, {"AppointmentID": 7})
    finding = _finding_row(conn, check_id, dedupe_key)
    assert finding.status == "open"
    assert str(finding.last_seen_run_id) == run_2
    assert _events(conn, finding.id) == ["created", "reseen"]
    # a recurrence is never treated as a new finding to narrate again.
    assert stats.created_findings == ()


def test_resolved_system_finding_reopens_on_recurring_fail(conn: sa.Connection) -> None:
    check_id, version_id, practice_id = _seed_check_and_practice(conn)
    fail_row = ExecutedRow(entity_key=(7,), tri_state="fail", evidence={"Status": "cancelled"})
    pass_row = ExecutedRow(entity_key=(7,), tri_state="pass", evidence={"Status": "completed"})

    run_1 = create_run(conn)
    materialize_check_result(
        conn,
        run_1,
        _result(check_id, version_id, practice_id, (fail_row,)),
        entity_key_columns=("AppointmentID",),
        severity="medium",
        auto_resolve=False,
    )
    run_2 = create_run(conn)
    materialize_check_result(
        conn,
        run_2,
        _result(check_id, version_id, practice_id, (pass_row,)),
        entity_key_columns=("AppointmentID",),
        severity="medium",
        auto_resolve=True,
    )
    dedupe_key = compute_dedupe_key(check_id, {"AppointmentID": 7})
    assert _finding_row(conn, check_id, dedupe_key).status == "resolved_system"

    run_3 = create_run(conn)
    stats = materialize_check_result(
        conn,
        run_3,
        _result(check_id, version_id, practice_id, (fail_row,)),
        entity_key_columns=("AppointmentID",),
        severity="medium",
        auto_resolve=False,
    )

    assert stats.reopened == 1
    finding = _finding_row(conn, check_id, dedupe_key)
    assert finding.status == "open"
    assert str(finding.last_seen_run_id) == run_3
    assert _events(conn, finding.id) == ["created", "resolved_system", "reseen"]


def test_dismissed_finding_stays_dismissed_but_reseen_event_recorded(conn: sa.Connection) -> None:
    check_id, version_id, practice_id = _seed_check_and_practice(conn)
    run_1 = create_run(conn)
    row = ExecutedRow(entity_key=(7,), tri_state="fail", evidence={"Status": "cancelled"})
    materialize_check_result(
        conn,
        run_1,
        _result(check_id, version_id, practice_id, (row,)),
        entity_key_columns=("AppointmentID",),
        severity="medium",
        auto_resolve=False,
    )
    dedupe_key = compute_dedupe_key(check_id, {"AppointmentID": 7})
    finding_id = _finding_row(conn, check_id, dedupe_key).id
    conn.execute(
        sa.text("UPDATE findings SET status = 'dismissed' WHERE id = :id"), {"id": finding_id}
    )
    conn.execute(
        sa.text(
            "INSERT INTO finding_events (finding_id, event, reason_code) "
            "VALUES (:id, 'dismissed', 'not-an-issue')"
        ),
        {"id": finding_id},
    )

    run_2 = create_run(conn)
    stats = materialize_check_result(
        conn,
        run_2,
        _result(check_id, version_id, practice_id, (row,)),
        entity_key_columns=("AppointmentID",),
        severity="medium",
        auto_resolve=False,
    )

    assert stats.reseen == 1
    assert stats.reopened == 0
    finding = _finding_row(conn, check_id, dedupe_key)
    assert finding.status == "dismissed"
    assert str(finding.last_seen_run_id) == run_2
    assert _events(conn, finding_id) == ["created", "dismissed", "reseen"]


def test_pass_row_resolves_open_finding_when_auto_resolve_true(conn: sa.Connection) -> None:
    check_id, version_id, practice_id = _seed_check_and_practice(conn)
    fail_row = ExecutedRow(entity_key=(7,), tri_state="fail", evidence={"Status": "cancelled"})
    run_1 = create_run(conn)
    materialize_check_result(
        conn,
        run_1,
        _result(check_id, version_id, practice_id, (fail_row,)),
        entity_key_columns=("AppointmentID",),
        severity="medium",
        auto_resolve=False,
    )

    pass_row = ExecutedRow(entity_key=(7,), tri_state="pass", evidence={"Status": "completed"})
    run_2 = create_run(conn)
    stats = materialize_check_result(
        conn,
        run_2,
        _result(check_id, version_id, practice_id, (pass_row,)),
        entity_key_columns=("AppointmentID",),
        severity="medium",
        auto_resolve=True,
    )

    assert stats.resolved_system == 1
    dedupe_key = compute_dedupe_key(check_id, {"AppointmentID": 7})
    finding = _finding_row(conn, check_id, dedupe_key)
    assert finding.status == "resolved_system"
    assert str(finding.last_seen_run_id) == run_2
    assert _events(conn, finding.id) == ["created", "resolved_system"]


def test_pass_row_does_not_resolve_when_auto_resolve_false(conn: sa.Connection) -> None:
    check_id, version_id, practice_id = _seed_check_and_practice(conn)
    fail_row = ExecutedRow(entity_key=(7,), tri_state="fail", evidence={"Status": "cancelled"})
    run_1 = create_run(conn)
    materialize_check_result(
        conn,
        run_1,
        _result(check_id, version_id, practice_id, (fail_row,)),
        entity_key_columns=("AppointmentID",),
        severity="medium",
        auto_resolve=False,
    )

    pass_row = ExecutedRow(entity_key=(7,), tri_state="pass", evidence={"Status": "completed"})
    run_2 = create_run(conn)
    stats = materialize_check_result(
        conn,
        run_2,
        _result(check_id, version_id, practice_id, (pass_row,)),
        entity_key_columns=("AppointmentID",),
        severity="medium",
        auto_resolve=False,
    )

    assert stats.resolved_system == 0
    dedupe_key = compute_dedupe_key(check_id, {"AppointmentID": 7})
    assert _finding_row(conn, check_id, dedupe_key).status == "open"


def test_pass_row_no_op_when_no_existing_finding(conn: sa.Connection) -> None:
    check_id, version_id, practice_id = _seed_check_and_practice(conn)
    run_id = create_run(conn)
    pass_row = ExecutedRow(entity_key=(7,), tri_state="pass", evidence={"Status": "completed"})

    stats = materialize_check_result(
        conn,
        run_id,
        _result(check_id, version_id, practice_id, (pass_row,)),
        entity_key_columns=("AppointmentID",),
        severity="medium",
        auto_resolve=True,
    )

    assert stats == MaterializationStats()
    count = conn.execute(
        sa.text("SELECT COUNT(*) FROM findings WHERE check_id = :id"), {"id": check_id}
    ).scalar()
    assert count == 0


def test_indeterminate_row_no_op(conn: sa.Connection) -> None:
    check_id, version_id, practice_id = _seed_check_and_practice(conn)
    run_id = create_run(conn)
    row = ExecutedRow(entity_key=(7,), tri_state="indeterminate", evidence={})

    stats = materialize_check_result(
        conn,
        run_id,
        _result(check_id, version_id, practice_id, (row,)),
        entity_key_columns=("AppointmentID",),
        severity="medium",
        auto_resolve=True,
    )

    assert stats.created == 0
    assert stats.reseen == 0
    assert stats.resolved_system == 0
    count = conn.execute(
        sa.text("SELECT COUNT(*) FROM findings WHERE check_id = :id"), {"id": check_id}
    ).scalar()
    assert count == 0


def test_idempotent_same_run_twice_produces_no_new_findings_or_events(conn: sa.Connection) -> None:
    check_id, version_id, practice_id = _seed_check_and_practice(conn)
    run_id = create_run(conn)
    row = ExecutedRow(entity_key=(7,), tri_state="fail", evidence={"Status": "cancelled"})
    result = _result(check_id, version_id, practice_id, (row,))

    first = materialize_check_result(
        conn,
        run_id,
        result,
        entity_key_columns=("AppointmentID",),
        severity="medium",
        auto_resolve=False,
    )
    second = materialize_check_result(
        conn,
        run_id,
        result,
        entity_key_columns=("AppointmentID",),
        severity="medium",
        auto_resolve=False,
    )

    assert first.created == 1
    assert second.created == 0
    assert second.reseen == 0
    assert second.skipped_idempotent == 1
    count = conn.execute(
        sa.text("SELECT COUNT(*) FROM findings WHERE check_id = :id"), {"id": check_id}
    ).scalar()
    assert count == 1
    dedupe_key = compute_dedupe_key(check_id, {"AppointmentID": 7})
    finding = _finding_row(conn, check_id, dedupe_key)
    assert _events(conn, finding.id) == ["created"]
