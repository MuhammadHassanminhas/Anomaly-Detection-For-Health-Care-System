"""Phase 6 step 3: `cdss.calibration.demotion` -- F5 auto-demotion. Reads
the latest `precision_stats` row per (practice, check) and, when its
precision is below `PRECISION_FLOOR`, sets `practice_check_config.demoted
= true` for that (practice, check) pair only -- the check stays live for
every other practice, and `src/cdss/run.py`'s `run_once` (Phase 6 step 3
change, same commit) now excludes a demoted pair from `target_checks`
exactly like a disabled one.

**Real gap found and fixed, not a pre-existing behavior**: the phase
spec's own step 3 text claims "executor (Phase 3 loader) already skips
demoted (practice, check) pairs" -- checked against the real code, this
was false. `cdss.check_registry.load_active_checks` reads `demoted` into
`LoadedCheck.demoted` but never filters on it, and `run.py`'s own
docstring explicitly documented the opposite ("a demoted check still runs
normally"). `run.py`'s `target_checks` filter and docstring are corrected
alongside this module, in the same step, rather than treating the skip as
already done.

**Idempotent by construction, at the SQL level, not by a read-then-write
race**: `_UPSERT_DEMOTION_SQL`'s `ON CONFLICT ... DO UPDATE ... WHERE
practice_check_config.demoted = false` means a pair already demoted is
never touched again by a later run -- `demoted_at`/`demotion_reason`
freeze at the *first* trigger, and nothing in this module's write path
ever sets `demoted` back to `false`. That absence is the entire
"re-promotion is human-only" guarantee (spec's own step 3 text) -- there
is no repromotion function anywhere in this codebase yet (grepped), so
there is nothing here to accidentally call.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa

from cdss.calibration.precision import PRECISION_FLOOR


@dataclass(frozen=True)
class DemotionResult:
    practice_id: str
    check_id: str
    precision: Decimal
    window_size: int
    total_feedback_count: int
    already_demoted: bool  # True if this (practice, check) pair was demoted before this run
    demoted_this_run: bool  # True only when this run itself flipped demoted false -> true


_SELECT_LATEST_BELOW_FLOOR_SQL = sa.text(
    """
    SELECT practice_id, check_id, window_size, genuine_issue_count, total_feedback_count,
           precision, computed_at
    FROM (
        SELECT DISTINCT ON (practice_id, check_id)
               practice_id, check_id, window_size, genuine_issue_count, total_feedback_count,
               precision, computed_at
        FROM precision_stats
        ORDER BY practice_id, check_id, computed_at DESC
    ) latest
    WHERE precision < :floor
    """
)

_SELECT_CURRENT_DEMOTED_SQL = sa.text(
    "SELECT demoted FROM practice_check_config WHERE practice_id = :practice_id "
    "AND check_id = :check_id"
)

_UPSERT_DEMOTION_SQL = sa.text(
    """
    INSERT INTO practice_check_config (practice_id, check_id, demoted, demoted_at, demotion_reason)
    VALUES (:practice_id, :check_id, true, :demoted_at, CAST(:demotion_reason AS jsonb))
    ON CONFLICT (practice_id, check_id) DO UPDATE SET
        demoted = true, demoted_at = :demoted_at, demotion_reason = CAST(:demotion_reason AS jsonb)
    WHERE practice_check_config.demoted = false
    """
)


def demote_if_below_floor(
    conn: sa.Connection,
    *,
    practice_id: str,
    check_id: str,
    precision: Decimal,
    window_size: int,
    genuine_issue_count: int,
    total_feedback_count: int,
    computed_at: datetime,
    floor: Decimal = PRECISION_FLOOR,
    now: datetime | None = None,
    dry_run: bool = False,
) -> DemotionResult:
    """Demotes one (practice, check) pair if `precision < floor` and it
    isn't already demoted. Always safe to call on an above-floor precision
    or an already-demoted pair -- both are no-ops (caller doesn't need to
    pre-filter)."""
    already_demoted_row = conn.execute(
        _SELECT_CURRENT_DEMOTED_SQL, {"practice_id": practice_id, "check_id": check_id}
    ).one_or_none()
    already_demoted = (
        bool(already_demoted_row.demoted) if already_demoted_row is not None else False
    )

    should_demote = precision < floor and not already_demoted
    if should_demote and not dry_run:
        reason_snapshot = {
            "window_size": window_size,
            "genuine_issue_count": genuine_issue_count,
            "total_feedback_count": total_feedback_count,
            "precision": str(precision),
            "computed_at": computed_at.isoformat(),
        }
        conn.execute(
            _UPSERT_DEMOTION_SQL,
            {
                "practice_id": practice_id,
                "check_id": check_id,
                "demoted_at": now if now is not None else datetime.now(UTC),
                "demotion_reason": json.dumps(reason_snapshot),
            },
        )

    return DemotionResult(
        practice_id=practice_id,
        check_id=check_id,
        precision=precision,
        window_size=window_size,
        total_feedback_count=total_feedback_count,
        already_demoted=already_demoted,
        demoted_this_run=should_demote,
    )


def run_demotion_job(
    conn: sa.Connection,
    *,
    floor: Decimal = PRECISION_FLOOR,
    now: datetime | None = None,
    dry_run: bool = False,
) -> list[DemotionResult]:
    """Evaluates every (practice, check) pair whose *latest* `precision_stats`
    row is below `floor` -- pairs whose latest computation is at or above
    the floor are never even selected, so a recovered pair is left alone
    exactly as `demote_if_below_floor` would leave it (both paths converge
    on "no write" for a healthy pair)."""
    rows = conn.execute(_SELECT_LATEST_BELOW_FLOOR_SQL, {"floor": floor}).all()
    return [
        demote_if_below_floor(
            conn,
            practice_id=row.practice_id,
            check_id=str(row.check_id),
            precision=row.precision,
            window_size=row.window_size,
            genuine_issue_count=row.genuine_issue_count,
            total_feedback_count=row.total_feedback_count,
            computed_at=row.computed_at,
            floor=floor,
            now=now,
            dry_run=dry_run,
        )
        for row in rows
    ]


def main() -> int:
    from cdss.app_db import load_app_db_url

    dry_run = "--dry-run" in sys.argv[1:]
    engine = sa.create_engine(load_app_db_url())
    with engine.begin() as conn:
        results = run_demotion_job(conn, dry_run=dry_run)
    for result in results:
        if result.already_demoted:
            status = "already demoted"
        elif result.demoted_this_run:
            status = "DEMOTED"
        else:
            status = "no change"
        print(
            f"{result.practice_id} / {result.check_id}: "
            f"precision={result.precision} n={result.total_feedback_count} {status}"
        )
    newly = sum(1 for r in results if r.demoted_this_run)
    print(
        f"{len(results)} below-floor pair(s) evaluated, {newly} newly demoted"
        f"{' [dry-run]' if dry_run else ''}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "DemotionResult",
    "demote_if_below_floor",
    "main",
    "run_demotion_job",
]
