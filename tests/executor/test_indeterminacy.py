"""Phase 3 step 7: indeterminacy surfacing (F6). Pure tests for the rate/
row-building primitives need no DB; the materialization-integration test (the
step's own named deliverable) requires CDSS_APP_DB_URL and skips (never
fails) otherwise -- D-009.1, via the shared `conn` fixture.
"""

from __future__ import annotations

import sqlalchemy as sa

from cdss.executor import CheckExecutionResult, create_run
from cdss.indeterminacy import (
    ENTITY_KEY_COLUMNS,
    build_indeterminacy_check_result,
    compute_indeterminate_rate,
)
from cdss.materialize import compute_dedupe_key, materialize_check_result

# --- pure ---------------------------------------------------------------


def _target_result(
    check_id: str = "target-check", *, n_pass: int, n_fail: int, n_indeterminate: int
) -> CheckExecutionResult:
    return CheckExecutionResult(
        check_id=check_id,
        check_version_id="target-version",
        practice_id="practice-1",
        sql_hash="deadbeef",
        watermark_from=None,
        watermark_to=None,
        duration_ms=5,
        rows_examined=n_pass + n_fail + n_indeterminate,
        n_pass=n_pass,
        n_fail=n_fail,
        n_indeterminate=n_indeterminate,
        status="ok",
        error_message=None,
        rows=(),
    )


def test_compute_indeterminate_rate() -> None:
    result = _target_result(n_pass=1, n_fail=1, n_indeterminate=8)
    assert compute_indeterminate_rate(result) == 0.8


def test_compute_indeterminate_rate_none_when_nothing_examined() -> None:
    result = _target_result(n_pass=0, n_fail=0, n_indeterminate=0)
    assert compute_indeterminate_rate(result) is None


def test_build_result_is_fail_row_when_rate_exceeds_threshold() -> None:
    target = _target_result(n_pass=1, n_fail=1, n_indeterminate=8)
    built = build_indeterminacy_check_result(
        "system-check", "system-version", target, threshold=0.2
    )
    assert built.rows_examined == 1
    assert len(built.rows) == 1
    row = built.rows[0]
    assert row.tri_state == "fail"
    assert row.entity_key == ("target-check",)
    assert row.evidence["rate"] == 0.8
    assert row.evidence["n_indeterminate"] == 8
    assert row.evidence["rows_examined"] == 10
    assert row.evidence["target_check_id"] == "target-check"


def test_build_result_is_pass_row_when_rate_at_or_under_threshold() -> None:
    target = _target_result(n_pass=8, n_fail=1, n_indeterminate=1)
    built = build_indeterminacy_check_result(
        "system-check", "system-version", target, threshold=0.2
    )
    assert len(built.rows) == 1
    assert built.rows[0].tri_state == "pass"


def test_build_result_is_empty_when_rate_not_evaluable() -> None:
    target = _target_result(n_pass=0, n_fail=0, n_indeterminate=0)
    built = build_indeterminacy_check_result(
        "system-check", "system-version", target, threshold=0.2
    )
    assert built.rows == ()
    assert built.rows_examined == 0


def test_build_result_never_emits_more_than_one_row_regardless_of_indeterminate_count() -> None:
    target = _target_result(n_pass=0, n_fail=0, n_indeterminate=500)
    built = build_indeterminacy_check_result(
        "system-check", "system-version", target, threshold=0.2
    )
    assert len(built.rows) == 1


# --- DB-gated: the named deliverable -------------------------------------


def _seed_checks_and_practice(conn: sa.Connection) -> tuple[str, str, str, str, str]:
    """Returns (system_check_id, system_version_id, target_check_id,
    target_version_id, practice_id)."""
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
    system_version_id = str(
        conn.execute(
            sa.text(
                "INSERT INTO check_versions "
                "(check_id, version_number, definition, definition_hash, "
                "affected_views, params_schema) "
                "VALUES (:check_id, 1, '{\"kind\": \"system\"}'::jsonb, 'hash', "
                "ARRAY[]::text[], '{}'::jsonb) RETURNING id"
            ),
            {"check_id": system_check_id},
        )
        .one()
        .id
    )
    target_check_id = str(
        conn.execute(
            sa.text(
                "INSERT INTO checks (slug, title, category, default_severity, source, status) "
                "VALUES ('target-check', 'Target', 'data-quality', 'medium', 'manual', 'active') "
                "RETURNING id"
            )
        )
        .one()
        .id
    )
    target_version_id = str(
        conn.execute(
            sa.text(
                "INSERT INTO check_versions "
                "(check_id, version_number, definition, definition_hash, "
                "affected_views, params_schema) "
                "VALUES (:check_id, 1, '{}'::jsonb, 'hash', ARRAY[]::text[], '{}'::jsonb) "
                "RETURNING id"
            ),
            {"check_id": target_check_id},
        )
        .one()
        .id
    )
    conn.execute(
        sa.text("INSERT INTO practices (practice_id, name) VALUES ('practice-1', 'Test Practice')")
    )
    return system_check_id, system_version_id, target_check_id, target_version_id, "practice-1"


def test_pervasive_null_prerequisites_yield_exactly_one_system_finding(conn: sa.Connection) -> None:
    system_check_id, system_version_id, target_check_id, target_version_id, practice_id = (
        _seed_checks_and_practice(conn)
    )
    run_id = create_run(conn)

    # Fixture data with pervasive NULL prerequisites: 8 of 10 rows indeterminate.
    target_result = CheckExecutionResult(
        check_id=target_check_id,
        check_version_id=target_version_id,
        practice_id=practice_id,
        sql_hash="deadbeef",
        watermark_from=None,
        watermark_to=None,
        duration_ms=5,
        rows_examined=10,
        n_pass=1,
        n_fail=1,
        n_indeterminate=8,
        status="ok",
        error_message=None,
        rows=(),
    )

    system_result = build_indeterminacy_check_result(
        system_check_id, system_version_id, target_result, threshold=0.2
    )
    stats = materialize_check_result(
        conn,
        run_id,
        system_result,
        entity_key_columns=ENTITY_KEY_COLUMNS,
        severity="medium",
        auto_resolve=True,
    )

    assert stats.created == 1
    count = conn.execute(
        sa.text("SELECT COUNT(*) FROM findings WHERE check_id = :id"), {"id": system_check_id}
    ).scalar()
    assert count == 1  # exactly one system finding, not N noise rows

    dedupe_key = compute_dedupe_key(system_check_id, {"target_check_id": target_check_id})
    finding = conn.execute(
        sa.text("SELECT * FROM findings WHERE check_id = :check_id AND dedupe_key = :dedupe_key"),
        {"check_id": system_check_id, "dedupe_key": dedupe_key},
    ).one()
    assert finding.status == "open"
    assert finding.evidence["n_indeterminate"] == 8
    assert finding.evidence["rate"] == 0.8


def test_rate_below_threshold_creates_no_finding(conn: sa.Connection) -> None:
    system_check_id, system_version_id, target_check_id, target_version_id, practice_id = (
        _seed_checks_and_practice(conn)
    )
    run_id = create_run(conn)
    target_result = CheckExecutionResult(
        check_id=target_check_id,
        check_version_id=target_version_id,
        practice_id=practice_id,
        sql_hash="deadbeef",
        watermark_from=None,
        watermark_to=None,
        duration_ms=5,
        rows_examined=10,
        n_pass=8,
        n_fail=1,
        n_indeterminate=1,
        status="ok",
        error_message=None,
        rows=(),
    )

    system_result = build_indeterminacy_check_result(
        system_check_id, system_version_id, target_result, threshold=0.2
    )
    stats = materialize_check_result(
        conn,
        run_id,
        system_result,
        entity_key_columns=ENTITY_KEY_COLUMNS,
        severity="medium",
        auto_resolve=True,
    )

    assert stats.created == 0
    count = conn.execute(
        sa.text("SELECT COUNT(*) FROM findings WHERE check_id = :id"), {"id": system_check_id}
    ).scalar()
    assert count == 0


def test_recovered_rate_auto_resolves_the_system_finding(conn: sa.Connection) -> None:
    system_check_id, system_version_id, target_check_id, target_version_id, practice_id = (
        _seed_checks_and_practice(conn)
    )
    run_1 = create_run(conn)
    bad_target = CheckExecutionResult(
        check_id=target_check_id,
        check_version_id=target_version_id,
        practice_id=practice_id,
        sql_hash="deadbeef",
        watermark_from=None,
        watermark_to=None,
        duration_ms=5,
        rows_examined=10,
        n_pass=1,
        n_fail=1,
        n_indeterminate=8,
        status="ok",
        error_message=None,
        rows=(),
    )
    materialize_check_result(
        conn,
        run_1,
        build_indeterminacy_check_result(
            system_check_id, system_version_id, bad_target, threshold=0.2
        ),
        entity_key_columns=ENTITY_KEY_COLUMNS,
        severity="medium",
        auto_resolve=True,
    )

    run_2 = create_run(conn)
    good_target = CheckExecutionResult(
        check_id=target_check_id,
        check_version_id=target_version_id,
        practice_id=practice_id,
        sql_hash="deadbeef",
        watermark_from=None,
        watermark_to=None,
        duration_ms=5,
        rows_examined=10,
        n_pass=9,
        n_fail=1,
        n_indeterminate=0,
        status="ok",
        error_message=None,
        rows=(),
    )
    stats = materialize_check_result(
        conn,
        run_2,
        build_indeterminacy_check_result(
            system_check_id, system_version_id, good_target, threshold=0.2
        ),
        entity_key_columns=ENTITY_KEY_COLUMNS,
        severity="medium",
        auto_resolve=True,
    )

    assert stats.resolved_system == 1
    dedupe_key = compute_dedupe_key(system_check_id, {"target_check_id": target_check_id})
    finding = conn.execute(
        sa.text(
            "SELECT status FROM findings WHERE check_id = :check_id AND dedupe_key = :dedupe_key"
        ),
        {"check_id": system_check_id, "dedupe_key": dedupe_key},
    ).one()
    assert finding.status == "resolved_system"
