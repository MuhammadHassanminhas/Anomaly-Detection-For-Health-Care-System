"""Phase 3 step 8: the run loop + cost report. `python -m cdss.run` (wrapped
by `scripts/run.ps1`) is the first place every earlier step's pieces are
wired together into one command: load active checks (step 3), plan each
check's watermark scan window (step 4), execute with preflight (step 5),
materialize findings (step 6), surface pervasive indeterminacy (step 7), and
emit a deterministic markdown cost report.

**Watermark strategy is caller-supplied per view** (`watermark_plans: Mapping
[str, WatermarkPlan]`), continuing the precedent step 4's own docstring set
("which strategy applies to a given view... is an argument the caller
supplies, not something this module looks up itself"): there is still no
per-view strategy config table, and the Phase 1 semantic catalog is the only
source of that classification -- reading it is `main()`'s job (the
production CLI path, against the real source), not `run_once`'s. A view
absent from `watermark_plans` is treated as unwatermarkable (fallback: a
bounded trailing scan) -- the safe default when nothing is known about it.

**Fixture-DB flag, continuing step 5's own note**: the LocalDB fixture's 4
views have no real `InsertedAt`/`UpdatedAt` columns (step 5: "there's no
genuine Phase-1-classified watermark column to test scoping against"). The
live proof of this module (`tests/executor/test_run.py`) therefore always
runs with `watermark_plans={}` -- every fixture check goes through the
bounded-full-scan fallback, which is why a second consecutive run against
unchanged fixture data still emits a `reseen` event per already-failing row
(the fallback's own "every run re-scans the same window" semantics, not a
materialization bug) even though it produces zero *new* findings. This is a
real tension against the phase's own exit criterion 3 ("second run: zero new
findings/events") that only the watermarked path resolves by advancing past
already-seen data -- flagged here for phase-close review, not silently
declared satisfied.

**System-check dispatch, continuing step 7's own flag**: a check whose slug
is in `SYSTEM_CHECK_SLUGS` is never compiled/executed like a normal check
(its `check_versions.definition` isn't DSL-shaped) -- it's evaluated once
per practice, against that practice's own already-computed target-check
results, via `cdss.indeterminacy.build_indeterminacy_check_result`.

**`enabled`/`demoted` split**: `cdss.check_registry.load_active_checks`
only enforces F3 (`status='active'`); a `practice_check_config.enabled=False`
row is check_registry's business to load (a caller might want to see it) but
this run loop's business to skip executing -- same now true of `demoted`
(Phase 6 step 3, F5): a demoted (practice, check) pair is excluded from
`target_checks` exactly like a disabled one, for that practice only -- the
check still runs normally for every other practice where it isn't demoted.
Corrected from this docstring's earlier (Phase 6 step-2-era) claim that a
demoted check "still runs normally" -- that was true only because step 3
(the thing that makes it stop) hadn't been built yet.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from os import environ
from pathlib import Path

import sqlalchemy as sa

from cdss.app_db import load_app_db_url
from cdss.app_db_repo import SourceAuditLogRepository
from cdss.authoring.llm_draft import LLMClient, OpenAIClient, load_llm_config
from cdss.check_registry import LoadedCheck, load_active_checks
from cdss.config import load_source_config
from cdss.connection import connect
from cdss.dsl import check_doc_from_dict
from cdss.executor import (
    CheckExecutionResult,
    execute_check_with_preflight,
    fetch_live_columns,
    finish_run,
)
from cdss.executor import create_run as _create_run
from cdss.feedback import ReasonCodeDistributionEntry, compute_reason_code_distribution
from cdss.indeterminacy import ENTITY_KEY_COLUMNS as _INDETERMINACY_ENTITY_KEY_COLUMNS
from cdss.indeterminacy import build_indeterminacy_check_result
from cdss.materialize import CreatedFinding, MaterializationStats, materialize_check_result
from cdss.narrate import ComposeResult, TemplateCache, compose, persist_narrative
from cdss.source import AuditedSourceConnection
from cdss.watermark_manager import (
    ScanWindow,
    compute_bounded_full_scan_window,
    compute_watermarked_window,
    get_watermark,
    set_watermark,
)

SYSTEM_CHECK_SLUGS: frozenset[str] = frozenset({"system-indeterminate-rate"})
DEFAULT_INDETERMINATE_THRESHOLD = 0.2
DEFAULT_WATERMARK_LOOKBACK: timedelta | None = None
DEFAULT_BOUNDED_SCAN_LOOKBACK = timedelta(days=30)
DEFAULT_REPORT_DIR = Path("artifacts/runs")


@dataclass(frozen=True)
class WatermarkPlan:
    """`column=None` means unwatermarkable -- the caller (usually built from
    the Phase 1 semantic catalog's `watermark_classification`) has no
    watermark column for this view, so the bounded-full-scan fallback
    applies. `is_hot` feeds `watermark_manager.should_escalate_to_ask`."""

    column: str | None
    is_hot: bool = False


#: values `render_cost_report` renders verbatim -- "watermarked" narrows on a
#: real column; "bounded_full_scan" is the fallback-a trailing-window scan
#: (F10/step 4/exit criterion 5: "unwatermarkable-view executions visibly
#: marked with their fallback strategy"); "system" is the F6 indeterminacy
#: check, which has no scan window of its own at all.
WatermarkStrategy = str


@dataclass(frozen=True)
class NarrationStats:
    """Phase 5 step 7: per-check tally of what happened to each *new*
    finding's narration attempt -- `composed` (a fresh LLM call produced a
    valid narrative), `cached` (the template cache, step 5, served it
    without a call), `blocked` (a real LLM response the validator/renderer
    rejected -- `blocked_fallback`), `fallback` (the LLM was unreachable or
    returned garbage -- `fallback_static`). Every new finding lands in
    exactly one bucket; none are ever skipped (F8: a finding is never lost
    or delayed by narration)."""

    composed: int = 0
    cached: int = 0
    blocked: int = 0
    fallback: int = 0


@dataclass(frozen=True)
class CheckRunSummary:
    slug: str
    practice_id: str
    status: str
    duration_ms: int
    rows_examined: int
    n_pass: int
    n_fail: int
    n_indeterminate: int
    watermark_from: datetime | None
    watermark_to: datetime | None
    watermark_strategy: WatermarkStrategy
    materialization: MaterializationStats
    narration: NarrationStats = NarrationStats()


@dataclass(frozen=True)
class RunReport:
    run_id: str
    started_at: datetime
    finished_at: datetime
    summaries: tuple[CheckRunSummary, ...]
    ask_recommendations: tuple[str, ...]
    # Phase 6 step 5: all-time (not run-scoped) genuine_issue/not_genuine
    # split per check -- D-029 scoped this down from the spec's own
    # data_entry_lag/policy_difference-specific text, since neither code
    # exists in the real REASON_CODES vocabulary (step 1). Defaulted so
    # every pre-existing RunReport(...) construction site is unaffected.
    reason_code_distribution: tuple[ReasonCodeDistributionEntry, ...] = ()


_SELECT_CATALOG_VERSION_SQL = sa.text("SELECT id FROM catalog_versions WHERE sha256 = :sha256")
_INSERT_CATALOG_VERSION_SQL = sa.text(
    "INSERT INTO catalog_versions (sha256, source_path) VALUES (:sha256, :source_path) RETURNING id"
)


def get_or_create_catalog_version(conn: sa.Connection, sha256: str, source_path: str) -> int:
    """Find-or-insert by `sha256` -- re-running against an unchanged catalog
    file reuses the same `catalog_versions` row rather than accumulating a
    new one every run."""
    existing = conn.execute(_SELECT_CATALOG_VERSION_SQL, {"sha256": sha256}).one_or_none()
    if existing is not None:
        return int(existing.id)
    inserted = conn.execute(
        _INSERT_CATALOG_VERSION_SQL, {"sha256": sha256, "source_path": source_path}
    ).one()
    return int(inserted.id)


def _resolve_scan_window(
    conn: sa.Connection,
    plan: WatermarkPlan,
    driving_view: str,
    *,
    watermark_lookback: timedelta | None,
    bounded_scan_lookback: timedelta,
    now: datetime,
) -> ScanWindow:
    if plan.column is None:
        return compute_bounded_full_scan_window(lookback=bounded_scan_lookback, now=now)
    last = get_watermark(conn, driving_view, plan.column)
    return compute_watermarked_window(watermark=last, lookback=watermark_lookback, now=now)


def _narrate_created_findings(
    conn: sa.Connection,
    check: LoadedCheck,
    created_findings: Sequence[CreatedFinding],
    *,
    narration_client: LLMClient,
    narration_model_id: str,
    narration_cache: TemplateCache,
) -> NarrationStats:
    """Phase 5 step 7's own deliverable: every *new* finding from this check
    (never a reseen/reopened recurrence -- `materialize_check_result` only
    reports genuinely new ones) gets a narrative composed and persisted
    inline, so `findings` and `narratives` never drift apart. A cache hit is
    distinguished from a fresh LLM call by checking the cache before calling
    `compose` -- `compose` itself doesn't report which path it took, only
    whether the end result is valid."""
    stats = NarrationStats()
    for created in created_findings:
        had_cache_hit = narration_cache.get(check.check_version_id, created.evidence) is not None
        result: ComposeResult = compose(
            narration_client,
            model_id=narration_model_id,
            check_version_id=check.check_version_id,
            definition=check.definition,
            rationale=check.rationale or "",
            fallback_template=check.fallback_template,
            evidence=created.evidence,
            params=check.params,
            cache=narration_cache,
        )
        persist_narrative(conn, finding_id=created.finding_id, result=result)
        if result.validation_status == "valid":
            stats = replace(
                stats,
                cached=stats.cached + 1 if had_cache_hit else stats.cached,
                composed=stats.composed + 1 if not had_cache_hit else stats.composed,
            )
        elif result.validation_status == "blocked_fallback":
            stats = replace(stats, blocked=stats.blocked + 1)
        else:  # "fallback_static"
            stats = replace(stats, fallback=stats.fallback + 1)
    return stats


def _run_target_check(
    conn: sa.Connection,
    source_conn: AuditedSourceConnection,
    run_id: str,
    check: LoadedCheck,
    *,
    catalog_version_id: int,
    watermark_plans: Mapping[str, WatermarkPlan],
    watermark_lookback: timedelta | None,
    bounded_scan_lookback: timedelta,
    auto_resolve: bool,
    now: datetime,
    narration_client: LLMClient | None,
    narration_model_id: str | None,
    narration_cache: TemplateCache,
) -> tuple[CheckExecutionResult, CheckRunSummary, str | None]:
    doc = check_doc_from_dict(check.definition)
    driving_view = doc.entity.view
    plan = watermark_plans.get(driving_view, WatermarkPlan(column=None))

    ask = None
    if plan.is_hot and plan.column is None:
        ask = (
            f"{driving_view}: hot and unwatermarkable -- "
            "recommend an ASK-NNN with the source-DB team"
        )

    scan_window = _resolve_scan_window(
        conn,
        plan,
        driving_view,
        watermark_lookback=watermark_lookback,
        bounded_scan_lookback=bounded_scan_lookback,
        now=now,
    )
    pinned_columns = fetch_live_columns(source_conn, driving_view, run_id=run_id)
    result = execute_check_with_preflight(
        conn,
        source_conn,
        run_id,
        check,
        driving_view,
        pinned_columns,
        catalog_version_id,
        watermark_column=plan.column,
        scan_window=scan_window,
    )
    if result.status == "ok" and plan.column is not None:
        set_watermark(conn, driving_view, plan.column, scan_window.to_ts)

    stats = materialize_check_result(
        conn,
        run_id,
        result,
        entity_key_columns=tuple(doc.entity.key),
        severity=check.default_severity,
        auto_resolve=auto_resolve,
    )
    narration = NarrationStats()
    if narration_client is not None and narration_model_id is not None and stats.created_findings:
        narration = _narrate_created_findings(
            conn,
            check,
            stats.created_findings,
            narration_client=narration_client,
            narration_model_id=narration_model_id,
            narration_cache=narration_cache,
        )
    summary = CheckRunSummary(
        slug=check.slug,
        practice_id=check.practice_id,
        status=result.status,
        duration_ms=result.duration_ms,
        rows_examined=result.rows_examined,
        n_pass=result.n_pass,
        n_fail=result.n_fail,
        n_indeterminate=result.n_indeterminate,
        watermark_from=result.watermark_from,
        watermark_to=result.watermark_to,
        watermark_strategy="watermarked" if plan.column is not None else "bounded_full_scan",
        materialization=stats,
        narration=narration,
    )
    return result, summary, ask


def run_once(
    conn: sa.Connection,
    source_conn: AuditedSourceConnection,
    checks: Sequence[LoadedCheck],
    *,
    catalog_version_id: int,
    watermark_plans: Mapping[str, WatermarkPlan] | None = None,
    indeterminate_threshold: float = DEFAULT_INDETERMINATE_THRESHOLD,
    watermark_lookback: timedelta | None = DEFAULT_WATERMARK_LOOKBACK,
    bounded_scan_lookback: timedelta = DEFAULT_BOUNDED_SCAN_LOOKBACK,
    auto_resolve: bool = True,
    system_check_slugs: frozenset[str] = SYSTEM_CHECK_SLUGS,
    now: datetime | None = None,
    narration_client: LLMClient | None = None,
    narration_model_id: str | None = None,
    narration_cache: TemplateCache | None = None,
) -> RunReport:
    """One full run: every enabled, non-system `checks` entry executed with
    preflight + materialized, then every system check (F6 indeterminacy
    surfacing) evaluated once per practice against that practice's own
    target-check results from this same run.

    Phase 5 step 7: when `narration_client` is given, every genuinely new
    finding produced this run gets a narrative composed and persisted
    inline (system-check findings are never narrated -- their `definition`
    isn't DSL-shaped). Narration is opt-in and defaults off, so callers that
    don't pass a client (most existing tests, and any run that only cares
    about execution/materialization) see identical behavior to before this
    step. `narration_cache` defaults to a fresh `TemplateCache` per call,
    shared across every check in this run (F10: LLM calls are O(active
    checks), never O(findings))."""
    watermark_plans = watermark_plans or {}
    moment = now if now is not None else datetime.now(UTC)
    run_id = _create_run(conn)
    cache = narration_cache if narration_cache is not None else TemplateCache()

    target_checks = [
        c for c in checks if c.slug not in system_check_slugs and c.enabled and not c.demoted
    ]
    system_checks = {c.practice_id: c for c in checks if c.slug in system_check_slugs and c.enabled}

    summaries: list[CheckRunSummary] = []
    ask_recommendations: list[str] = []
    results_by_practice: dict[str, list[CheckExecutionResult]] = {}

    for check in target_checks:
        result, summary, ask = _run_target_check(
            conn,
            source_conn,
            run_id,
            check,
            catalog_version_id=catalog_version_id,
            watermark_plans=watermark_plans,
            watermark_lookback=watermark_lookback,
            bounded_scan_lookback=bounded_scan_lookback,
            auto_resolve=auto_resolve,
            now=moment,
            narration_client=narration_client,
            narration_model_id=narration_model_id,
            narration_cache=cache,
        )
        summaries.append(summary)
        if ask is not None:
            ask_recommendations.append(ask)
        results_by_practice.setdefault(check.practice_id, []).append(result)

    for practice_id, target_results in results_by_practice.items():
        system_check = system_checks.get(practice_id)
        if system_check is None:
            continue
        for target_result in target_results:
            system_result = build_indeterminacy_check_result(
                system_check.check_id,
                system_check.check_version_id,
                target_result,
                threshold=indeterminate_threshold,
            )
            if not system_result.rows:
                continue
            stats = materialize_check_result(
                conn,
                run_id,
                system_result,
                entity_key_columns=_INDETERMINACY_ENTITY_KEY_COLUMNS,
                severity=system_check.default_severity,
                auto_resolve=True,
            )
            summaries.append(
                CheckRunSummary(
                    slug=system_check.slug,
                    practice_id=practice_id,
                    status="ok",
                    duration_ms=0,
                    rows_examined=system_result.rows_examined,
                    n_pass=system_result.n_pass,
                    n_fail=system_result.n_fail,
                    n_indeterminate=0,
                    watermark_from=None,
                    watermark_to=None,
                    watermark_strategy="system",
                    materialization=stats,
                )
            )

    finish_run(conn, run_id, status="completed")
    return RunReport(
        run_id=run_id,
        started_at=moment,
        finished_at=datetime.now(UTC),
        summaries=tuple(summaries),
        ask_recommendations=tuple(ask_recommendations),
        reason_code_distribution=compute_reason_code_distribution(conn),
    )


def render_cost_report(report: RunReport) -> str:
    """Deterministic markdown -- every value comes straight from `report`,
    nothing computed or guessed here beyond formatting (Accuracy rules: no
    fabricated/estimated values, ever)."""
    lines = [
        f"# Run report — {report.run_id}",
        "",
        f"Started: {report.started_at.isoformat()}",
        f"Finished: {report.finished_at.isoformat()}",
        "",
        "## Per-check results",
        "",
        "| Check | Practice | Status | Strategy | Duration (ms) | Rows examined | Pass | Fail | "
        "Indeterminate | Watermark span | Findings (new/reseen/reopened/resolved) | "
        "Narratives (composed/cached/blocked/fallback) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in report.summaries:
        span = (
            f"{s.watermark_from.isoformat() if s.watermark_from else '(none)'} -> "
            f"{s.watermark_to.isoformat()}"
            if s.watermark_to is not None
            else "—"
        )
        m = s.materialization
        findings = f"{m.created}/{m.reseen}/{m.reopened}/{m.resolved_system}"
        n = s.narration
        narratives = f"{n.composed}/{n.cached}/{n.blocked}/{n.fallback}"
        lines.append(
            f"| {s.slug} | {s.practice_id} | {s.status} | {s.watermark_strategy} | "
            f"{s.duration_ms} | {s.rows_examined} | "
            f"{s.n_pass} | {s.n_fail} | {s.n_indeterminate} | {span} | {findings} | {narratives} |"
        )

    # Exit criterion 5: unwatermarkable-view executions visibly marked with
    # their fallback strategy -- called out again here, not just buried in
    # the per-row table, since "visibly marked" is the criterion's own word.
    lines += ["", "## Fallback-strategy executions (unwatermarkable views)", ""]
    fallback = [s for s in report.summaries if s.watermark_strategy == "bounded_full_scan"]
    if fallback:
        for s in fallback:
            lines.append(f"- {s.slug} ({s.practice_id}): bounded_full_scan fallback")
    else:
        lines.append("(none — every executed check ran against a real watermark column)")

    lines += ["", "## Top-cost checks (by duration)", ""]
    top = sorted(report.summaries, key=lambda s: s.duration_ms, reverse=True)[:5]
    if top:
        for s in top:
            lines.append(
                f"- {s.slug} ({s.practice_id}): {s.duration_ms} ms, {s.rows_examined} rows"
            )
    else:
        lines.append("(no checks executed)")

    lines += ["", "## Narration", ""]
    total_composed = sum(s.narration.composed for s in report.summaries)
    total_cached = sum(s.narration.cached for s in report.summaries)
    total_blocked = sum(s.narration.blocked for s in report.summaries)
    total_fallback = sum(s.narration.fallback for s in report.summaries)
    if total_composed or total_cached or total_blocked or total_fallback:
        lines.append(
            f"- {total_composed} composed, {total_cached} cached, "
            f"{total_blocked} blocked (validator-rejected), {total_fallback} fallback (LLM outage)"
        )
    else:
        lines.append("(no new findings narrated this run)")

    # Phase 6 step 5 (D-029-scoped): all-time genuine_issue/not_genuine
    # split per check, not run-scoped -- a check dismissed mostly
    # `not_genuine` is a calibration/design candidate to flag for review,
    # not something this report decides on its own.
    lines += ["", "## Reason-Code Distribution (all-time)", ""]
    if report.reason_code_distribution:
        lines.append("| Check | genuine_issue | not_genuine |")
        lines.append("|---|---|---|")
        for entry in report.reason_code_distribution:
            lines.append(
                f"| {entry.slug} | {entry.genuine_issue_count} | {entry.not_genuine_count} |"
            )
    else:
        lines.append("(no reason-coded dismissals recorded yet)")

    lines += ["", "## ASK recommendations", ""]
    if report.ask_recommendations:
        for a in report.ask_recommendations:
            lines.append(f"- {a}")
    else:
        lines.append("(none)")

    return "\n".join(lines) + "\n"


def _report_path(run_id: str, report_dir: Path = DEFAULT_REPORT_DIR) -> Path:
    return report_dir / f"run-{run_id}-report.md"


def write_cost_report(report: RunReport, report_dir: Path = DEFAULT_REPORT_DIR) -> Path:
    path = _report_path(report.run_id, report_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_cost_report(report), encoding="utf-8")
    return path


class _ForceOutageClient:
    """Phase 5 step 7's own kill switch (`CDSS_LLM_FORCE_OUTAGE=1`, named in
    the phase spec's own verification commands): simulates an LLM outage
    without spending a real API call, so the deterministic fallback path
    (`compose`'s `fallback_static`) can be proven live against the real
    production wiring, not just a unit test's `FakeLLMClient`."""

    def complete(self, prompt: str) -> str:
        raise RuntimeError("CDSS_LLM_FORCE_OUTAGE=1 -- simulated LLM outage")


def _build_narration_client(env: Mapping[str, str] | None = None) -> tuple[LLMClient, str]:
    source = env if env is not None else environ
    if source.get("CDSS_LLM_FORCE_OUTAGE", "").strip() == "1":
        return _ForceOutageClient(), "forced-outage"
    config = load_llm_config(env)
    return OpenAIClient(config), config.model


def main() -> int:
    """Production entrypoint: the real source DB (`CDSS_SOURCE_*`, same
    connection factory `verify_env`/`profile` use) and the app DB
    (`CDSS_APP_DB_URL`). No `--catalog`-driven watermark planning is wired up
    yet -- every view runs unwatermarked (bounded full scan) until a future
    step reads `semantic-catalog-v3.json` into `WatermarkPlan`s; flagged
    rather than silently assumed complete.

    Every source statement gets both audit sinks (D-016, exit criterion 6):
    `AuditedSourceConnection`'s own JSONL line, plus `SourceAuditLogRepository`
    mirroring the same event into `source_audit_log` -- the dual-sink wiring
    step 2 built but this module hadn't connected until this pass.

    Runs under AUTOCOMMIT, not one wrapping transaction: `SourceAuditLogRepository
    .record()` opens its own engine-level transaction per call, synchronously,
    mid-run (step 2's own durability design -- an audit write must survive
    even a later rollback of the surrounding business transaction) -- against
    a single wrapping transaction, its FK reference to the run this same call
    is part of would not yet be visible, and every source statement would
    fail with a ForeignKeyViolation. Caught live proving exit criterion 6.

    The SQL guard's allowlist starts empty and is expanded to exactly the
    loaded checks' own `affected_views` (D-015/constraint 3's own defense-in-
    depth pattern, same as `verify_env`'s `with_allowed_objects` use) --
    caught live in the same pass: an unscoped connection rejected every real
    check with `StatementRejectedError`, silently downgraded to
    `check_executions.status='error'` by `execute_check`'s broad catch,
    which is exactly why this needed a live run to surface at all."""
    app_engine = sa.create_engine(load_app_db_url())
    source_config = load_source_config()
    source_raw = connect(source_config)
    source_conn = AuditedSourceConnection(
        source_raw, component="run", app_db_sink=SourceAuditLogRepository(app_engine)
    )
    narration_client, narration_model_id = _build_narration_client()
    try:
        with app_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            checks = load_active_checks(conn)
            allowed_objects = frozenset(
                view.lower() for check in checks for view in check.affected_views
            )
            scoped_source_conn = source_conn.with_allowed_objects(allowed_objects)
            catalog_version_id = get_or_create_catalog_version(
                conn, sha256="unpinned", source_path="(none)"
            )
            report = run_once(
                conn,
                scoped_source_conn,
                checks,
                catalog_version_id=catalog_version_id,
                narration_client=narration_client,
                narration_model_id=narration_model_id,
            )
    finally:
        source_raw.close()
        app_engine.dispose()

    path = write_cost_report(report)
    print(render_cost_report(report))
    print(f"Run {report.run_id}: {len(report.summaries)} check executions")
    print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CheckRunSummary",
    "NarrationStats",
    "RunReport",
    "SYSTEM_CHECK_SLUGS",
    "WatermarkPlan",
    "get_or_create_catalog_version",
    "main",
    "render_cost_report",
    "run_once",
    "write_cost_report",
]
