# Phase 3 — App database + executor

## Objective

Stand up the system-owned application database (checks, findings, feedback, params, runs, audit) as versioned migrations, and build the executor: incremental watermark execution of compiled checks, tri-state accounting, finding materialization with dedup/snooze/lifecycle, and per-check cost capture.

## Preconditions

- Phase 2 closed: compiler emits deterministic SQL with named watermark placeholders; fixture DB exists.
- **D-005 ruled** (engine; spec assumes PostgreSQL 16 — a SQL Server ruling changes dialect/migration tooling only).
- **D-008 confirmed** (practice-scoped keys assumed throughout).

## Steps (one deliverable each; TDD throughout)

1. **Migrations baseline.** Alembic setup + migration 0001 creating the full ARCHITECTURE.md §6 schema: check library (`checks`, `check_versions`, `action_library`, `check_actions`), per-practice config (`practices`, `practice_check_config`, `calibration_runs`, `precision_stats`), execution & findings (`runs`, `check_executions`, `watermarks`, `findings`, `finding_events`, `narratives`), discovery & governance (`discovery_signals`, `discovery_candidates`, `source_audit_log`, `catalog_versions`, `schema_drift_events`). Constraints as code: `findings.check_version_id` NOT NULL FK (F2), unique `(check_id, dedupe_key)`, `finding_events` append-only (no UPDATE/DELETE grants + trigger guard), `reason_code` NOT NULL when `event='dismissed'` (CHECK).
   *Deliverable:* `alembic upgrade head` from empty DB; downgrade/upgrade round-trip test; constraint tests (each violation rejected by the DB, not just the app).
2. **App-DB access layer + audit mirror.** SQLAlchemy Core repositories; the Phase 0 JSONL audit events now also insert into `source_audit_log` (D-016) — same choke point, both sinks, JSONL remains primary/append-only.
   *Deliverable:* repository unit tests; audit dual-sink test (one event ⇒ one JSONL line + one row).
3. **Check registry + loader.** Load `status=active` checks + the active `check_version` + per-practice config/params; executor structurally cannot see non-active checks (query filters + test proving a `draft` check never executes — F3 gate enforcement).
   *Deliverable:* loader with tests incl. the draft-never-runs proof.
4. **Watermark manager.** Per (view, column) watermark rows; increment scoping `> watermark` plus check-declared lookback; **fallback strategies for unwatermarkable views** (classified live in Phase 1): (a) bounded full scan within declared lookback window, or (b) snapshot-hash diff over the entity key set — chosen per view in config, recorded per execution. A view that is both hot and unwatermarkable triggers an `ASK-NNN` recommendation in the cost report, never a base-table workaround.
   *Deliverable:* watermark tests: first run (no watermark ⇒ initial full window), incremental run, fallback paths.
5. **Executor core.** Per run: preflight (live schema vs pinned catalog version; drift ⇒ affected checks skipped-as-indeterminate + `schema_drift_events` row, run continues) → per (check, practice): bind params + watermark values, execute through the source access layer, collect `(entity_key, tri_state, evidence)` rows → write `check_executions` (sql_hash, watermark span, duration_ms, rows_examined, n_pass/n_fail/n_indeterminate).
   *Deliverable:* executor runs the Phase 2 example checks against the fixture DB end-to-end; tri-state counts match Phase 2's hand-computed expectations.
6. **Finding materialization.** Upsert by `dedupe_key = hash(check_id, canonical entity key)`: new ⇒ `open` + `created` event; re-seen ⇒ bump `last_seen`, `reseen` event; previously-failing-now-passing ⇒ `resolved_system` if the check opts in; snoozed ⇒ suppressed from queue, still tracked. Evidence stored as typed JSONB — deterministic values only, minimum fields the check declares.
   *Deliverable:* materialization tests for every transition; **idempotency test: re-running the same increment twice produces zero new findings and zero duplicate events**.
7. **Indeterminacy surfacing (F6).** Per (check, practice, run): indeterminate rate above configured threshold emits a system data-quality finding via a built-in system check (itself versioned in the library, not hard-coded).
   *Deliverable:* test — fixture data with pervasive NULL prerequisites yields exactly one system finding, not N noise rows.
8. **Cost report + run CLI.** `python -m cdss.run` (wrapped by `scripts/run.ps1`) executes a full run and emits `artifacts/runs/run-<id>-report.md`: per-check duration, rows examined, tri-state counts, watermark spans, top-cost checks, ASK recommendations.
   *Deliverable:* one-command run against fixture DB producing the report.

## Exit criteria

1. `scripts/check.ps1` exits 0 (incl. migration round-trip, constraint, lifecycle-transition, and draft-never-runs tests).
2. `alembic upgrade head` succeeds on an empty database (proven in CI from scratch).
3. `scripts/run.ps1` processes the fixture DB end-to-end; **immediate re-run produces zero duplicate findings and zero new events** (idempotency criterion, test-enforced and demonstrated live).
4. Fixture rows engineered to pass→fail→pass across three runs traverse `open → resolved_system` correctly; dismissal without reason code is rejected by the database itself.
5. Cost report artifact emitted with per-check cost; unwatermarkable-view executions visibly marked with their fallback strategy.
6. Every source statement of a run is audited in both sinks with the run id attached.

## Verification (gatekeeper commands)

```powershell
.\scripts\check.ps1
alembic upgrade head
.\scripts\run.ps1 ; .\scripts\run.ps1        # second run: zero new findings/events (report shows it)
python -m pytest tests/executor tests/migrations -v
Get-ChildItem artifacts/runs/
```

## Risks / dependencies / open questions

- **D-005/D-008 block start** (engine, tenancy keys). Assumed: PostgreSQL 16, practice-scoped.
- **Local Postgres for dev/CI** rides on the same D-009.1 container-runtime answer as the fixture SQL Server; fallback is a native install (documented one-time prerequisite).
- **Dedupe-key stability:** canonical entity-key serialization must be frozen (sorted keys, typed rendering) before first production run — covered by a golden test; changing it later would re-flag everything, so it is versioned with the check.
- **Snapshot-hash fallback cost** on large unwatermarkable views may itself be expensive — measured by the cost report; escalation path is `ASK-NNN`, not a workaround.
- **Depends on:** Phases 0–2 artifacts.
- No new `DECISIONS.md` entries required by this spec (D-016's second sink lands here as planned).
