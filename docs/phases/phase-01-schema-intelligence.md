# Phase 1 — Schema intelligence

## Objective

Automated, re-runnable profiling of every in-scope view producing the **semantic catalog** — the machine-readable artifact that is the sole schema authority for the check compiler (D-017) — plus a human-readable profiling report.

## Preconditions

- Phase 0 closed: confirmed view surface (D-001 DECIDED), row counts, watermark candidates in `artifacts/env-report.json`.
- D-010 ruled: the profiling scope list = confirmed surface minus approved exclusions.
- All reads via the Phase 0 audited source access layer (SELECT-only, allowlisted, audited). Profiling adds no new access paths.

## Steps (one deliverable each; TDD throughout)

1. **Catalog schema first.** `src/cdss/schemas/semantic-catalog.schema.json` defining the full catalog shape: per-view → per-column stats, candidate keys, relationships, domains, sentinels, watermarks, profiling metadata (catalog version, produced_at, source row counts, per-query costs). Versioned; the compiler will later refuse any catalog that does not validate.
   *Deliverable:* schema file + a hand-built minimal fixture catalog that validates.
2. **Column profiler.** Per column: SQL type (from `INFORMATION_SCHEMA.COLUMNS`), null rate, distinct count, min/max (typed), top-K frequent values with frequencies (K configurable, default 20; only for columns under a cardinality ceiling), string length stats. All set-based SQL, one statement per view where feasible (batched aggregates), per-query timeout + cost capture (F10). Large views profile on a deterministic sample (`TABLESAMPLE` or top-N by watermark) with the sampling method **recorded in the catalog** — sampled stats are never presented as exact.
   *Deliverable:* profiler module + unit tests against fixture data; live run populates per-column stats.
3. **Candidate-key detection.** Columns/column-pairs where distinct count = row count (exact on small views, verified-on-sample + flagged on large ones). `*ID` naming used only as a hint to order candidates, never as evidence.
   *Deliverable:* per-view candidate-key list with the evidence (counts) attached.
4. **Cross-view join/containment analysis.** For plausible key pairs (name/type-compatible, e.g., `PatientID` across views): containment ratio both directions (what fraction of A's values exist in B), orphan counts, computed set-based with per-pair cost budget; pairs exceeding budget are recorded as `skipped: cost` rather than guessed (F10). Output: typed relationship edges with containment stats — the seed for profiling-derived referential checks (F3a).
   *Deliverable:* relationship edge list in the catalog with per-edge evidence and cost.
5. **Sentinel & domain detection.** Low-cardinality columns get enumerated value domains (value + frequency). Sentinel candidates flagged by heuristics: placeholder dates (e.g., 1900-01-01), zero/negative IDs, empty-string vs NULL usage, overloaded magic values. Test-record indicators (`IsTestRecord`, `IsDummy`, `IsDeleted`, `IsActive`) profiled explicitly with their prevalence — these become standard `base_filters` defaults in Phase 2.
   *Deliverable:* domains + sentinel candidates + test-record-indicator section in the catalog.
6. **Watermark verification.** For each view: confirm watermark candidate columns live (type, null rate, monotonicity spot-check via `MAX` per recent window); classify each view `watermarkable | fallback-needed` — refining the export-derived list of 12 from live data.
   *Deliverable:* watermark classification per view in the catalog.
7. **Export reconciliation & discrepancy log.** Compare live findings against `schema_for_SQL_PROJ.txt` hypotheses (names, columns, implied relations). Every disagreement logged in the report; live wins (D-017).
   *Deliverable:* discrepancy section of the human report.
8. **One-command orchestration + reports.** `scripts/profile.ps1` (wrapping `python -m cdss.profile`) runs steps 2–7 end-to-end, writes `artifacts/catalog/semantic-catalog-v<N>.json` (validated) + `artifacts/catalog/profiling-report.md`, and records the catalog version + artifact hash. Idempotent; re-run produces a new version. Interim failures resume cleanly (per-view checkpointing) — no manual steps.
   *Deliverable:* both artifacts from one command on a clean checkout.

## Exit criteria

1. `scripts/check.ps1` exits 0 (lint, mypy strict, all tests — profiler logic unit-tested against synthetic fixtures, clearly labeled synthetic).
2. `scripts/profile.ps1` exits 0 against the live source, end-to-end, unattended.
3. `artifacts/catalog/semantic-catalog-v1.json` validates against `semantic-catalog.schema.json` (`python -m cdss.validate_catalog` exits 0).
4. Catalog covers **every** in-scope view: per-column stats, ≥1 candidate-key judgment per view (possibly "none found"), watermark classification; sampled/skipped items explicitly marked, never silently absent.
5. Relationship edges exist with containment evidence; cost-skipped pairs are listed as such.
6. `profiling-report.md` contains the export discrepancy log.
7. Every live statement audited; audit line count matches statement count.

## Verification (gatekeeper commands)

```powershell
.\scripts\check.ps1
.\scripts\profile.ps1
python -m cdss.validate_catalog artifacts/catalog/semantic-catalog-v1.json
Select-String -Path artifacts/catalog/profiling-report.md -Pattern "Discrepanc"
Get-Content artifacts/audit/source-audit-*.jsonl | Measure-Object -Line
```

## Risks / dependencies / open questions

- **Profiling cost on large views** (`dbo.Patient` 201 cols, `dbo.Measurements` 140 cols; row counts known from Phase 0): mitigated by batched aggregates, sampling with recorded method, per-query budgets. If a view cannot be profiled within budget even sampled → `ASK-NNN` (e.g., a stats-friendly view), never a base-table workaround.
- **Join/containment pair explosion:** candidate pairs are name/type-gated and budget-capped; the skip list is visible in the catalog so coverage gaps are explicit.
- **Free-text/PHI columns** (e.g., `ClinicalNotes` in `dbo.HTIReferral`): profiled by null rate/length only — **no top-K value collection on free-text columns** (PHI at rest in the catalog). Column classification (code-like vs free-text) is a profiler heuristic recorded in the catalog; Tier M redaction (D-003) applies to anything leaving for LLM authoring later.
- **Depends on:** Phase 0 artifacts; D-010 ruling at start.
- No new `DECISIONS.md` entries required by this spec.
