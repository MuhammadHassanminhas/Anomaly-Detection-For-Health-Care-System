"""Phase 6 step 2: `cdss.calibration.precision` -- per-(practice, check)
precision over the trailing window of reason-coded feedback events, the
input step 3's auto-demotion job will read (`precision_stats` rows), per
D-011 (still OPEN, values proposed): floor 0.30, trailing window 50,
min-n 10 -- "all config, not constants" per the phase spec's own text, but
this codebase has no config-table/env-var precedent for tunable business
numbers; the only existing D-011-sourced value, `MIN_SAMPLE_SIZE` in the
sibling `learn_defaults_for_check` (Phase 4 step 6), is a plain overridable
module constant, not read from any external source -- `TRAILING_WINDOW`
follows that same precedent rather than inventing new config plumbing.

"Reason-coded feedback event" = a `finding_events` row with `event =
'dismissed'` (the only writer of `reason_code`, per `cdss.feedback.dismiss`)
-- `reason_code IS NOT NULL` is an equivalent filter, used directly since it
needs no join to `cdss.feedback`'s own vocabulary.

`precision_stats` has no unique constraint on `(practice_id, check_id)` (no
"current window" pointer, same shape gap `check_registry` already flagged
for `check_versions`) -- "idempotent" (spec's own word) means *value*-
idempotent: recomputing the same window inserts a new row with an identical
`(window_size, genuine_issue_count, total_feedback_count, precision)` tuple,
never a mutated one. Below `min_sample_size` writes no row at all -- the
spec's own "no stat row => no demotion possible" deliverable text, kept
literal rather than writing a zero/null placeholder row.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from decimal import Decimal

import sqlalchemy as sa

TRAILING_WINDOW = 50  # D-011 precedent: "trailing 50 feedback events"
PRECISION_FLOOR = Decimal("0.30")  # D-011 precedent: "precision < 0.30"

_MIN_SAMPLE_SIZE_DEFAULT = 10  # D-011 precedent: "minimum 10 events" -- matches calibration's own


@dataclass(frozen=True)
class PrecisionResult:
    practice_id: str
    check_id: str
    window_size: int  # the *configured* trailing window (e.g. 50) -- schema's own "window bounds"
    genuine_issue_count: int  # of the events actually found within the window
    total_feedback_count: int  # events actually found, <= window_size when fewer exist
    precision: Decimal | None  # None when total_feedback_count < min_sample_size -- no row written


_SELECT_TRAILING_REASON_CODES_SQL = sa.text(
    """
    SELECT fe.reason_code
    FROM finding_events fe
    JOIN findings f ON f.id = fe.finding_id
    WHERE f.practice_id = :practice_id AND f.check_id = :check_id
      AND fe.event = 'dismissed' AND fe.reason_code IS NOT NULL
    ORDER BY fe.occurred_at DESC
    LIMIT :window_size
    """
)

_INSERT_PRECISION_STATS_SQL = sa.text(
    """
    INSERT INTO precision_stats
        (practice_id, check_id, window_size, genuine_issue_count, total_feedback_count, precision)
    VALUES
        (:practice_id, :check_id, :window_size, :genuine_issue_count, :total_feedback_count,
         :precision)
    """
)


def compute_precision(
    conn: sa.Connection,
    *,
    practice_id: str,
    check_id: str,
    trailing_window: int = TRAILING_WINDOW,
    min_sample_size: int = _MIN_SAMPLE_SIZE_DEFAULT,
) -> PrecisionResult:
    """Reads the trailing `trailing_window` reason-coded dismissal events for
    one (practice, check) pair and computes `precision =
    genuine_issue_count / total_feedback_count`. Pure computation, no write
    -- `record_precision` is the separate step that persists it, matching
    `cdss.feedback`'s own compute-then-persist split."""
    reason_codes = [
        row.reason_code
        for row in conn.execute(
            _SELECT_TRAILING_REASON_CODES_SQL,
            {"practice_id": practice_id, "check_id": check_id, "window_size": trailing_window},
        )
    ]
    total = len(reason_codes)
    genuine = sum(1 for code in reason_codes if code == "genuine_issue")
    precision = (
        (Decimal(genuine) / Decimal(total)) if total >= min_sample_size and total > 0 else None
    )
    return PrecisionResult(
        practice_id=practice_id,
        check_id=check_id,
        window_size=trailing_window,
        genuine_issue_count=genuine,
        total_feedback_count=total,
        precision=precision,
    )


def record_precision(conn: sa.Connection, result: PrecisionResult) -> bool:
    """Writes one `precision_stats` row -- only when `result.precision` is
    not `None` (below `min_sample_size` => no row, per the spec's own "no
    stat row => no demotion possible" text). Returns whether a row was
    written."""
    if result.precision is None:
        return False
    conn.execute(
        _INSERT_PRECISION_STATS_SQL,
        {
            "practice_id": result.practice_id,
            "check_id": result.check_id,
            "window_size": result.window_size,
            "genuine_issue_count": result.genuine_issue_count,
            "total_feedback_count": result.total_feedback_count,
            "precision": result.precision,
        },
    )
    return True


_SELECT_ACTIVE_PAIRS_SQL = sa.text(
    """
    SELECT DISTINCT f.practice_id, f.check_id
    FROM finding_events fe
    JOIN findings f ON f.id = fe.finding_id
    WHERE fe.event = 'dismissed' AND fe.reason_code IS NOT NULL
    """
)


def run_precision_job(
    conn: sa.Connection,
    *,
    trailing_window: int = TRAILING_WINDOW,
    min_sample_size: int = _MIN_SAMPLE_SIZE_DEFAULT,
    dry_run: bool = False,
) -> list[PrecisionResult]:
    """Computes precision for every (practice, check) pair that has at least
    one reason-coded dismissal, ever -- the production entrypoint `main()`
    wraps this. `dry_run=True` computes and returns results without calling
    `record_precision` (the CLI's own `--dry-run` flag, no other job module
    in this codebase has one yet -- this is the first)."""
    pairs = conn.execute(_SELECT_ACTIVE_PAIRS_SQL).all()
    results: list[PrecisionResult] = []
    for pair in pairs:
        result = compute_precision(
            conn,
            practice_id=pair.practice_id,
            check_id=str(pair.check_id),
            trailing_window=trailing_window,
            min_sample_size=min_sample_size,
        )
        results.append(result)
        if not dry_run:
            record_precision(conn, result)
    return results


def main() -> int:
    from cdss.app_db import load_app_db_url

    dry_run = "--dry-run" in sys.argv[1:]
    engine = sa.create_engine(load_app_db_url())
    # dry_run=True makes run_precision_job skip every record_precision call,
    # so this transaction never has anything to write -- committing an
    # empty transaction is a no-op, no explicit rollback needed.
    with engine.begin() as conn:
        results = run_precision_job(conn, dry_run=dry_run)
    for result in results:
        status = (
            "no stat row (below min-n)"
            if result.precision is None
            else f"precision={result.precision}"
        )
        print(
            f"{result.practice_id} / {result.check_id}: "
            f"n={result.total_feedback_count} genuine={result.genuine_issue_count} {status}"
        )
    print(f"{len(results)} (practice, check) pair(s) evaluated{' [dry-run]' if dry_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "PRECISION_FLOOR",
    "TRAILING_WINDOW",
    "PrecisionResult",
    "compute_precision",
    "main",
    "record_precision",
    "run_precision_job",
]
