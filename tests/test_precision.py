"""Phase 6 step 2: the precision computation job, `cdss.calibration.precision`.
All DB-gated (every behavior needs the real `finding_events`/`findings`/
`precision_stats` tables) via the shared `conn` fixture -- CDSS_APP_DB_URL
required, skipped otherwise (D-009.1).

`occurred_at` is deliberately set explicitly on every seeded event rather
than relying on `finding_events`' own `now()` server default: Postgres'
`now()` returns the *transaction's* start timestamp, constant for every
statement inside one transaction -- and every test here runs inside one
transaction (the `conn` fixture, rolled back after). Relying on the
default would make every seeded event's `occurred_at` identical, breaking
the trailing-window `ORDER BY occurred_at DESC` boundary this module's own
correctness depends on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa

from cdss.calibration.precision import (
    PRECISION_FLOOR,
    TRAILING_WINDOW,
    compute_precision,
    record_precision,
    run_precision_job,
)

_BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def test_d011_defaults() -> None:
    assert TRAILING_WINDOW == 50
    assert Decimal("0.30") == PRECISION_FLOOR


def _seed_check(conn: sa.Connection, *, slug: str = "test-check") -> tuple[str, str]:
    check_id = str(
        conn.execute(
            sa.text(
                "INSERT INTO checks (slug, title, category, default_severity, source, status) "
                "VALUES (:slug, 'Test', 'data-quality', 'medium', 'manual', 'active') "
                "RETURNING id"
            ),
            {"slug": slug},
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
    return check_id, version_id


def _seed_practice(conn: sa.Connection, practice_id: str) -> None:
    conn.execute(
        sa.text("INSERT INTO practices (practice_id, name) VALUES (:id, :id)"),
        {"id": practice_id},
    )


def _seed_finding(conn: sa.Connection, check_id: str, version_id: str, practice_id: str) -> str:
    run_id = str(conn.execute(sa.text("INSERT INTO runs DEFAULT VALUES RETURNING id")).one().id)
    return str(
        conn.execute(
            sa.text(
                "INSERT INTO findings "
                "(check_id, check_version_id, practice_id, dedupe_key, entity_key, "
                " status, severity, evidence, first_seen_run_id, last_seen_run_id) "
                "VALUES (:check_id, :version_id, :practice_id, :dedupe_key, '{}'::jsonb, "
                " 'dismissed', 'medium', '{}'::jsonb, :run_id, :run_id) "
                "RETURNING id"
            ),
            {
                "check_id": check_id,
                "version_id": version_id,
                "practice_id": practice_id,
                "dedupe_key": f"dk-{practice_id}-{run_id}",
                "run_id": run_id,
            },
        )
        .one()
        .id
    )


def _insert_dismissal(
    conn: sa.Connection, finding_id: str, reason_code: str, occurred_at: datetime
) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO finding_events (finding_id, event, reason_code, actor, occurred_at) "
            "VALUES (:finding_id, 'dismissed', :reason_code, 'alice', :occurred_at)"
        ),
        {"finding_id": finding_id, "reason_code": reason_code, "occurred_at": occurred_at},
    )


def _seed_pair(conn: sa.Connection, *, practice_id: str, slug: str) -> tuple[str, str, str]:
    check_id, version_id = _seed_check(conn, slug=slug)
    _seed_practice(conn, practice_id)
    finding_id = _seed_finding(conn, check_id, version_id, practice_id)
    return check_id, version_id, finding_id


def test_compute_precision_hand_computed_ratio(conn: sa.Connection) -> None:
    check_id, _, finding_id = _seed_pair(conn, practice_id="practice-1", slug="check-a")
    for i in range(7):
        _insert_dismissal(conn, finding_id, "genuine_issue", _BASE_TIME + timedelta(minutes=i))
    for i in range(3):
        _insert_dismissal(conn, finding_id, "not_genuine", _BASE_TIME + timedelta(minutes=7 + i))

    result = compute_precision(
        conn, practice_id="practice-1", check_id=check_id, trailing_window=10, min_sample_size=10
    )

    assert result.total_feedback_count == 10
    assert result.genuine_issue_count == 7
    assert result.precision == Decimal("0.7")
    assert result.window_size == 10


def test_compute_precision_window_boundary_ignores_older_events(conn: sa.Connection) -> None:
    check_id, _, finding_id = _seed_pair(conn, practice_id="practice-1", slug="check-a")
    # 8 older events, all 'not_genuine' -- must be excluded by a window of 5.
    for i in range(8):
        _insert_dismissal(conn, finding_id, "not_genuine", _BASE_TIME + timedelta(days=i))
    # 5 more-recent events, all 'genuine_issue' -- the only ones a trailing
    # window of 5 should ever see.
    for i in range(5):
        _insert_dismissal(
            conn, finding_id, "genuine_issue", _BASE_TIME + timedelta(days=100, minutes=i)
        )

    result = compute_precision(
        conn, practice_id="practice-1", check_id=check_id, trailing_window=5, min_sample_size=5
    )

    assert result.total_feedback_count == 5
    assert result.genuine_issue_count == 5
    assert result.precision == Decimal("1")


def test_compute_precision_below_min_sample_size_yields_no_precision(conn: sa.Connection) -> None:
    check_id, _, finding_id = _seed_pair(conn, practice_id="practice-1", slug="check-a")
    for i in range(3):
        _insert_dismissal(conn, finding_id, "genuine_issue", _BASE_TIME + timedelta(minutes=i))

    result = compute_precision(
        conn, practice_id="practice-1", check_id=check_id, trailing_window=50, min_sample_size=10
    )

    assert result.total_feedback_count == 3
    assert result.window_size == 50
    assert result.precision is None


def test_compute_precision_no_events_yields_no_precision(conn: sa.Connection) -> None:
    check_id, _, _finding_id = _seed_pair(conn, practice_id="practice-1", slug="check-a")

    result = compute_precision(
        conn, practice_id="practice-1", check_id=check_id, trailing_window=50, min_sample_size=10
    )

    assert result.total_feedback_count == 0
    assert result.precision is None


def test_record_precision_skips_write_when_precision_is_none(conn: sa.Connection) -> None:
    check_id, _, finding_id = _seed_pair(conn, practice_id="practice-1", slug="check-a")
    _insert_dismissal(conn, finding_id, "genuine_issue", _BASE_TIME)
    result = compute_precision(
        conn, practice_id="practice-1", check_id=check_id, trailing_window=50, min_sample_size=10
    )

    written = record_precision(conn, result)

    assert written is False
    count = conn.execute(sa.text("SELECT COUNT(*) FROM precision_stats")).scalar_one()
    assert count == 0


def test_record_precision_writes_row_when_precision_present(conn: sa.Connection) -> None:
    check_id, _, finding_id = _seed_pair(conn, practice_id="practice-1", slug="check-a")
    for i in range(10):
        _insert_dismissal(conn, finding_id, "genuine_issue", _BASE_TIME + timedelta(minutes=i))
    result = compute_precision(
        conn, practice_id="practice-1", check_id=check_id, trailing_window=10, min_sample_size=10
    )

    written = record_precision(conn, result)

    assert written is True
    row = conn.execute(
        sa.text(
            "SELECT window_size, genuine_issue_count, total_feedback_count, precision "
            "FROM precision_stats WHERE practice_id = 'practice-1' AND check_id = :check_id"
        ),
        {"check_id": check_id},
    ).one()
    assert row.window_size == 10
    assert row.genuine_issue_count == 10
    assert row.total_feedback_count == 10
    assert row.precision == Decimal("1")


def test_recompute_same_window_is_value_idempotent(conn: sa.Connection) -> None:
    check_id, _, finding_id = _seed_pair(conn, practice_id="practice-1", slug="check-a")
    for i in range(7):
        _insert_dismissal(conn, finding_id, "genuine_issue", _BASE_TIME + timedelta(minutes=i))
    for i in range(3):
        _insert_dismissal(conn, finding_id, "not_genuine", _BASE_TIME + timedelta(minutes=7 + i))

    first = compute_precision(
        conn, practice_id="practice-1", check_id=check_id, trailing_window=10, min_sample_size=10
    )
    second = compute_precision(
        conn, practice_id="practice-1", check_id=check_id, trailing_window=10, min_sample_size=10
    )

    assert first == second
    record_precision(conn, first)
    record_precision(conn, second)
    rows = conn.execute(
        sa.text(
            "SELECT window_size, genuine_issue_count, total_feedback_count, precision "
            "FROM precision_stats WHERE practice_id = 'practice-1' AND check_id = :check_id"
        ),
        {"check_id": check_id},
    ).all()
    assert len(rows) == 2
    assert rows[0] == rows[1]


def test_run_precision_job_covers_every_pair_with_feedback(conn: sa.Connection) -> None:
    check_a, _, finding_a = _seed_pair(conn, practice_id="practice-1", slug="check-a")
    check_b, _, finding_b = _seed_pair(conn, practice_id="practice-2", slug="check-b")
    for i in range(10):
        _insert_dismissal(conn, finding_a, "genuine_issue", _BASE_TIME + timedelta(minutes=i))
    for i in range(10):
        _insert_dismissal(conn, finding_b, "not_genuine", _BASE_TIME + timedelta(minutes=i))
    # A third pair with no feedback at all must never appear -- it has no
    # reason-coded event to compute a precision from.
    _seed_pair(conn, practice_id="practice-3", slug="check-c")

    results = run_precision_job(conn, trailing_window=10, min_sample_size=10)

    pairs = {(r.practice_id, r.check_id) for r in results}
    assert pairs == {("practice-1", check_a), ("practice-2", check_b)}
    count = conn.execute(sa.text("SELECT COUNT(*) FROM precision_stats")).scalar_one()
    assert count == 2


def test_run_precision_job_dry_run_writes_nothing(conn: sa.Connection) -> None:
    _, _, finding_id = _seed_pair(conn, practice_id="practice-1", slug="check-a")
    for i in range(10):
        _insert_dismissal(conn, finding_id, "genuine_issue", _BASE_TIME + timedelta(minutes=i))

    results = run_precision_job(conn, trailing_window=10, min_sample_size=10, dry_run=True)

    assert len(results) == 1
    assert results[0].precision == Decimal("1")
    count = conn.execute(sa.text("SELECT COUNT(*) FROM precision_stats")).scalar_one()
    assert count == 0
