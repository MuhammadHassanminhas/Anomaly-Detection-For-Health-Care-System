"""Phase 6 step 4: `cdss.calibration.recalibrate` -- F4 scheduled
recalibration. Re-runs Phase 4's `learn_defaults_for_check` (percentile-
strategy params only) for every active, configured (practice, check) pair
that has at least one such param, applying the freshly learned value only
when `params_source != 'manual'` -- the spec's own step 4 guard: "human-set
params are never overwritten silently".

**The guard lives here, not in `learn_defaults_for_check` itself**:
Phase 4's function has no manual-params check because nothing called it
more than once against a config a human might since have edited by hand --
recalibration is the first caller for which "first-time learn" and
"scheduled recalibrate" actually differ. Leaving Phase 4's own function
unmodified (still callable exactly as it already is, still relied on by
`tests/test_calibration.py` unchanged) rather than growing an
already-tested function a new conditional it never needed before.

**"Every applied shift appears in the run report" (spec's own step 4
text)** is already satisfied by `learn_defaults_for_check`'s own existing
`calibration_runs` row (`params_before`/`params_after`, one per call) --
this module's `main()` additionally prints a per-pair line (applied vs.
skipped-manual), the same standalone-job-report precedent `precision.py`/
`demotion.py` already set; there is no shared `RunReport` this job's output
naturally belongs in (it isn't part of the check-execution run loop).

**`--dry-run` is a transaction-rollback, not a compute/write split**:
unlike `precision.py`/`demotion.py`, the actual write here happens inside
the reused `learn_defaults_for_check` call itself, which has no dry-run
mode of its own -- rather than growing one, `main()` wraps the whole job in
one connection-owned transaction and rolls it back instead of committing
when `--dry-run` is given. Every `RecalibrationResult` returned is still
the real, fully-computed outcome (real source-DB reads, real learned
values) either way -- only the app-DB write is undone.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import sqlalchemy as sa

from cdss.app_db_repo import SourceAuditLogRepository
from cdss.calibration import LearnedParam, learn_defaults_for_check
from cdss.check_registry import LoadedCheck, load_active_checks
from cdss.dsl import check_doc_from_dict
from cdss.source import AuditedSourceConnection

SYSTEM_CHECK_SLUGS = frozenset({"system-indeterminate-rate"})


@dataclass(frozen=True)
class RecalibrationResult:
    practice_id: str
    check_id: str
    slug: str
    applied: bool  # False when skipped because params_source == 'manual'
    learned_params: tuple[LearnedParam, ...] = ()


def _has_percentile_param(check: LoadedCheck) -> bool:
    if check.slug in SYSTEM_CHECK_SLUGS:
        return False  # {"kind": "system"} isn't DSL-shaped -- never parseable, never a candidate
    doc = check_doc_from_dict(check.definition)
    return any(p.default.strategy == "percentile" for p in doc.params.values())


def recalibrate_check_for_practice(
    source_conn: AuditedSourceConnection, conn: sa.Connection, check: LoadedCheck
) -> RecalibrationResult:
    """Re-learns every percentile param on `check.definition` for
    `check.practice_id`, unless its current `params_source == 'manual'` --
    a human who set this practice's params by hand is never silently
    overridden by a scheduled job."""
    if check.params_source == "manual":
        return RecalibrationResult(
            practice_id=check.practice_id, check_id=check.check_id, slug=check.slug, applied=False
        )
    learned = learn_defaults_for_check(
        source_conn,
        conn,
        check_id=check.check_id,
        practice_id=check.practice_id,
        definition=check.definition,
    )
    return RecalibrationResult(
        practice_id=check.practice_id,
        check_id=check.check_id,
        slug=check.slug,
        applied=True,
        learned_params=tuple(learned),
    )


def run_recalibration_job(
    source_conn: AuditedSourceConnection, conn: sa.Connection
) -> list[RecalibrationResult]:
    """Every active, configured (practice, check) pair with at least one
    percentile-strategy param -- a check with none (e.g. a plain domain or
    referential check) is skipped outright, never even attempted, since
    `learn_defaults_for_check` is itself a no-op for it."""
    checks = load_active_checks(conn)
    return [
        recalibrate_check_for_practice(source_conn, conn, check)
        for check in checks
        if _has_percentile_param(check)
    ]


def main() -> int:
    from cdss.app_db import load_app_db_url
    from cdss.config import load_source_config
    from cdss.connection import connect

    dry_run = "--dry-run" in sys.argv[1:]
    app_engine = sa.create_engine(load_app_db_url())
    source_config = load_source_config()
    source_raw = connect(source_config)
    try:
        source_conn = AuditedSourceConnection(
            source_raw, component="recalibrate", app_db_sink=SourceAuditLogRepository(app_engine)
        )
        with app_engine.connect() as conn:
            trans = conn.begin()
            checks = load_active_checks(conn)
            allowed_objects = frozenset(
                view.lower() for check in checks for view in check.affected_views
            )
            scoped_source_conn = source_conn.with_allowed_objects(allowed_objects)
            results = run_recalibration_job(scoped_source_conn, conn)
            if dry_run:
                trans.rollback()
            else:
                trans.commit()
    finally:
        source_raw.close()
        app_engine.dispose()

    applied = [r for r in results if r.applied]
    skipped_manual = [r for r in results if not r.applied]
    for result in results:
        status = "applied" if result.applied else "skipped (manual params)"
        print(f"{result.practice_id} / {result.slug}: {status}")
    print(
        f"{len(results)} (practice, check) pair(s) with percentile params evaluated, "
        f"{len(applied)} applied, {len(skipped_manual)} skipped-manual"
        f"{' [dry-run]' if dry_run else ''}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "RecalibrationResult",
    "main",
    "recalibrate_check_for_practice",
    "run_recalibration_job",
]
