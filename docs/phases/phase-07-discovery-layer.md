# Phase 7 — Discovery layer

## Objective

Build the F9 lane: engineered per-entity/per-practice aggregates, deliberately boring drift/outlier detectors writing to an internal signal queue, LLM characterization of recurring patterns, and candidate-check drafting into the Phase 4 review gate — with a construction-level and test-enforced guarantee that discovery can never create a staff-facing finding (F9/F12).

## Preconditions

- Phases 3–6 closed: `discovery_signals`/`discovery_candidates` tables, review gate CLI, Tier M redaction harness, fixture pipeline.
- **D-004 ruled** for steps 4–5 only (characterization/drafting); steps 1–3, 6 are LLM-free.

## Steps (one deliverable each; TDD throughout)

1. **Aggregate builder.** `cdss.discovery.aggregates`: scheduled set-based SQL over the views (through the audited access layer) computing per-practice, per-week engineered series — e.g., invoice-lag percentiles, lab turnaround percentiles, recall-completion rate, inbox-aging distribution, appointment-volume by type, indeterminacy rates from `check_executions`. Series definitions are versioned config (YAML), each citing the views/columns it reads (catalog-validated like checks); results land in an app-DB `discovery_aggregates` table (new migration).
   *Deliverable:* builder + migration + tests on fixture data with hand-computed aggregates; incremental (only new weeks recomputed).
2. **Detectors (deliberately boring).** `cdss.discovery.detect`: (a) outlier lane — robust z-score (median/MAD) of a practice's latest point against its own history; (b) drift lane — week-over-week shift vs a seasonal baseline (same-week-of-year median where history allows, trailing median otherwise). Both write `discovery_signals(lane, scope, signal, score, run_id, status='new')`. Thresholds are config; no ML dependencies, no opaque scores — every signal row carries the numbers that produced it.
   *Deliverable:* detectors + unit tests on synthetic series with known outliers/drifts (all caught, quiet series silent).
3. **Signal lifecycle + dedup.** Recurring identical signals (same series, same direction, adjacent windows) coalesce rather than re-queue (`status: new → triaged → characterized → promoted | discarded`); a retention/expiry policy keeps the internal queue finite. CLI: `python -m cdss.discovery list/triage`.
   *Deliverable:* lifecycle tests incl. coalescing; queue browsable via CLI.
4. **LLM characterization (Tier M).** For recurring/triaged signal clusters: prompt = series definition, signal statistics, related catalog context — aggregates only, redaction-enforced, recorded payloads. LLM output: a structured characterization (suspected pattern, affected workflow, confidence rationale) stored on the cluster; **prose for internal triage only, never staff-facing**.
   *Deliverable:* characterization harness + redaction-boundary test (same standard as Phase 4/5).
5. **Candidate-check drafting (F3c).** From a characterized cluster the LLM drafts a DSL check (same validation path as Phase 4: parse → catalog-validate → auto-reject/repair-once); survivors land as `discovery_candidates` → `checks(source=discovery, status=draft)` with rationale linking back to the originating signals. From there, the one review gate (Phase 4 CLI) — no shortcut.
   *Deliverable:* drafting harness + test: characterized fixture cluster ⇒ valid draft in the gate with signal lineage attached.
6. **F9 isolation proof.** Two enforcement layers, both tested: (a) construction — the discovery package has no import path to finding-materialization code, and its DB role/session has no INSERT/UPDATE grant on `findings`/`finding_events` (engine-level where supported); (b) CI guard — a static test failing if `cdss.discovery.*` references findings tables or materialization modules.
   *Deliverable:* both guards green; attempted write from discovery code in a test raises at the DB layer.

## Exit criteria

1. `scripts/check.ps1` exits 0, including detector, lifecycle, redaction, and both F9 isolation guards.
2. **The brief's exit scenario, as one e2e test:** a synthetic pattern injected into fixture data (e.g., practice B's invoice lag doubles for three consecutive weeks) is detected (signal rows with the evidencing numbers), characterized (structured LLM output recorded), and yields a valid draft check in the review gate — **while `findings` row count from the discovery lane is provably zero** throughout.
3. Signal dedup proven: the same persistent pattern coalesces instead of flooding the queue.
4. Redaction: recorded characterization payloads contain aggregates only — no row-level values, no identifiers.
5. Discovery runs end-to-end by one command (`scripts/discover.ps1`), unattended, audited.

## Verification (gatekeeper commands)

```powershell
.\scripts\check.ps1
python -m pytest tests/discovery -v          # incl. e2e injected-pattern test + F9 guards
.\scripts\discover.ps1
python -m cdss.discovery list --status new
```

## Risks / dependencies / open questions

- **Aggregate lane cost:** weekly series over large views could be expensive; same discipline as everywhere — per-query budgets, cost in run report, `ASK-NNN` if a series is unaffordable (e.g., ask for a pre-aggregated view), never a base-table workaround.
- **Seasonal baselines need history:** early runs lack a year of data; drift lane degrades to trailing-median explicitly (recorded per signal) rather than pretending seasonality it can't compute.
- **Signal quality is deliberately not a product goal:** the queue is internal (F9); noisy detectors cost triage time, not staff trust. Tuning thresholds is config work informed by the queue itself.
- **LLM characterization may mislabel a pattern:** it only ever produces a *draft check with lineage* that you review at the gate with fixture tests required (Phase 4 rules apply unchanged).
- **Depends on:** Phases 3–6; D-004 for steps 4–5.
- New migration (`discovery_aggregates`) extends the Phase 3 schema — versioned Alembic migration, no `DECISIONS.md` entry needed (additive, within ARCHITECTURE.md §2.8's stated design).
