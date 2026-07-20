"""Phase 6 step 3: auto-demotion, `cdss.calibration.demotion`. All DB-gated
via the shared `conn` fixture -- CDSS_APP_DB_URL required, skipped
otherwise (D-009.1).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa

from cdss.calibration.demotion import demote_if_below_floor, run_demotion_job

_T1 = datetime(2026, 1, 1, tzinfo=UTC)
_T2 = datetime(2026, 2, 1, tzinfo=UTC)


def _seed_check_and_practice(conn: sa.Connection, *, slug: str = "check-a") -> str:
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
    conn.execute(
        sa.text("INSERT INTO practices (practice_id, name) VALUES ('practice-1', 'Test Practice')")
    )
    return check_id


def _demoted_row(conn: sa.Connection, check_id: str) -> sa.Row[tuple[object, ...]] | None:
    return conn.execute(
        sa.text(
            "SELECT demoted, demoted_at, demotion_reason FROM practice_check_config "
            "WHERE practice_id = 'practice-1' AND check_id = :check_id"
        ),
        {"check_id": check_id},
    ).one_or_none()


def _insert_precision_stats(
    conn: sa.Connection, check_id: str, *, precision: Decimal, computed_at: datetime, n: int = 20
) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO precision_stats "
            "(practice_id, check_id, window_size, genuine_issue_count, total_feedback_count, "
            " precision, computed_at) "
            "VALUES ('practice-1', :check_id, 50, :genuine, :total, :precision, :computed_at)"
        ),
        {
            "check_id": check_id,
            "genuine": int(precision * n),
            "total": n,
            "precision": precision,
            "computed_at": computed_at,
        },
    )


def test_demote_if_below_floor_demotes_and_snapshots_the_trigger(conn: sa.Connection) -> None:
    check_id = _seed_check_and_practice(conn)

    result = demote_if_below_floor(
        conn,
        practice_id="practice-1",
        check_id=check_id,
        precision=Decimal("0.1"),
        window_size=50,
        genuine_issue_count=2,
        total_feedback_count=20,
        computed_at=_T1,
        floor=Decimal("0.30"),
        now=_T1,
    )

    assert result.already_demoted is False
    assert result.demoted_this_run is True
    row = _demoted_row(conn, check_id)
    assert row is not None
    assert row.demoted is True
    assert row.demoted_at == _T1
    assert row.demotion_reason["precision"] == "0.1"
    assert row.demotion_reason["total_feedback_count"] == 20


def test_demote_if_below_floor_noop_when_precision_at_or_above_floor(conn: sa.Connection) -> None:
    check_id = _seed_check_and_practice(conn)

    result = demote_if_below_floor(
        conn,
        practice_id="practice-1",
        check_id=check_id,
        precision=Decimal("0.5"),
        window_size=50,
        genuine_issue_count=10,
        total_feedback_count=20,
        computed_at=_T1,
        floor=Decimal("0.30"),
        now=_T1,
    )

    assert result.demoted_this_run is False
    assert _demoted_row(conn, check_id) is None


def test_demote_if_below_floor_dry_run_makes_no_write(conn: sa.Connection) -> None:
    check_id = _seed_check_and_practice(conn)

    result = demote_if_below_floor(
        conn,
        practice_id="practice-1",
        check_id=check_id,
        precision=Decimal("0.1"),
        window_size=50,
        genuine_issue_count=2,
        total_feedback_count=20,
        computed_at=_T1,
        floor=Decimal("0.30"),
        now=_T1,
        dry_run=True,
    )

    assert result.demoted_this_run is True  # what *would* happen
    assert _demoted_row(conn, check_id) is None  # but nothing was written


def test_demote_if_below_floor_no_auto_repromotion_second_call_leaves_demoted_at_unchanged(
    conn: sa.Connection,
) -> None:
    """The no-auto-repromotion proof: once demoted, a later call -- even one
    reporting a *worse* precision and a later trigger time -- never rewrites
    `demoted_at`/`demotion_reason` (the SQL's own `WHERE demoted = false`
    guard), and nothing anywhere sets `demoted` back to `false`."""
    check_id = _seed_check_and_practice(conn)
    demote_if_below_floor(
        conn,
        practice_id="practice-1",
        check_id=check_id,
        precision=Decimal("0.2"),
        window_size=50,
        genuine_issue_count=4,
        total_feedback_count=20,
        computed_at=_T1,
        floor=Decimal("0.30"),
        now=_T1,
    )

    result = demote_if_below_floor(
        conn,
        practice_id="practice-1",
        check_id=check_id,
        precision=Decimal("0.05"),
        window_size=50,
        genuine_issue_count=1,
        total_feedback_count=20,
        computed_at=_T2,
        floor=Decimal("0.30"),
        now=_T2,
    )

    assert result.already_demoted is True
    assert result.demoted_this_run is False
    row = _demoted_row(conn, check_id)
    assert row is not None
    assert row.demoted is True
    assert row.demoted_at == _T1  # unchanged -- frozen at the first trigger
    assert row.demotion_reason["precision"] == "0.2"  # the original snapshot, not the new one


def test_run_demotion_job_only_considers_the_latest_precision_stats_row(
    conn: sa.Connection,
) -> None:
    check_id = _seed_check_and_practice(conn)
    _insert_precision_stats(conn, check_id, precision=Decimal("0.8"), computed_at=_T1)
    _insert_precision_stats(conn, check_id, precision=Decimal("0.1"), computed_at=_T2)

    results = run_demotion_job(conn, floor=Decimal("0.30"), now=_T2)

    assert len(results) == 1
    assert results[0].demoted_this_run is True
    row = _demoted_row(conn, check_id)
    assert row is not None and row.demoted is True


def test_run_demotion_job_skips_pair_whose_latest_precision_recovered(
    conn: sa.Connection,
) -> None:
    check_id = _seed_check_and_practice(conn)
    _insert_precision_stats(conn, check_id, precision=Decimal("0.1"), computed_at=_T1)
    _insert_precision_stats(conn, check_id, precision=Decimal("0.9"), computed_at=_T2)

    results = run_demotion_job(conn, floor=Decimal("0.30"), now=_T2)

    assert results == []
    assert _demoted_row(conn, check_id) is None


def test_run_demotion_job_no_auto_repromotion_after_precision_recovers(
    conn: sa.Connection,
) -> None:
    check_id = _seed_check_and_practice(conn)
    _insert_precision_stats(conn, check_id, precision=Decimal("0.1"), computed_at=_T1)
    run_demotion_job(conn, floor=Decimal("0.30"), now=_T1)
    row_after_demotion = _demoted_row(conn, check_id)
    assert row_after_demotion is not None and row_after_demotion.demoted is True

    _insert_precision_stats(conn, check_id, precision=Decimal("0.9"), computed_at=_T2)
    results = run_demotion_job(conn, floor=Decimal("0.30"), now=_T2)

    assert results == []  # the recovered pair is never even evaluated
    row_after_recovery = _demoted_row(conn, check_id)
    assert row_after_recovery is not None
    assert row_after_recovery.demoted is True  # still demoted -- human-only re-promotion
    assert row_after_recovery.demoted_at == row_after_demotion.demoted_at


def test_run_demotion_job_dry_run_writes_nothing(conn: sa.Connection) -> None:
    check_id = _seed_check_and_practice(conn)
    _insert_precision_stats(conn, check_id, precision=Decimal("0.1"), computed_at=_T1)

    results = run_demotion_job(conn, floor=Decimal("0.30"), now=_T1, dry_run=True)

    assert len(results) == 1
    assert results[0].demoted_this_run is True
    assert _demoted_row(conn, check_id) is None
