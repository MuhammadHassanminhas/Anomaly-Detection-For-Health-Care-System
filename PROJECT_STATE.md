# PROJECT_STATE.md — where CDSS is right now

> Update on every step completion, blocker change, or task change (see `CLAUDE.md` §3).
> Everything here must be verifiable from the repo, `DECISIONS.md`, or recorded command output — no aspirational status.

## Current phase

**Phase 0 — Environment & access verification** (Stage B execution; spec: `docs/phases/phase-00-environment-access.md`).
Steps 1–8 of 8 complete. All 8 exit criteria have supporting evidence (one caveat, see below) — **Phase 0 exit itself has not been declared**; that is a phase-boundary decision reserved for explicit product-owner approval, not taken unilaterally here.

## Last completed work (2026-07-15, step 8)

Step 8 — environment report artifact:
- New modules (TDD, failing test first): `src/cdss/report.py` (assembles + schema-validates `EnvironmentReport` → JSON/Markdown), `src/cdss/validate_report.py` (`python -m cdss.validate_report <path>` gatekeeper CLI), `src/cdss/schemas/env-report.schema.json`. `src/cdss/verify_env.py` extended with `run_verification()` (orchestrates steps 4–7) and `determine_in_scope_objects()`. `scripts/verify_env.ps1` no longer a stub.
- Design gap found and fixed: `AuditedSourceConnection`'s allowlist was fixed at construction, but step 7 needs to read the objects step 5 discovers live. Added `AuditedSourceConnection.with_allowed_objects()` (new instance, additive, original untouched) — required for step 7 to run at all, so fixed in place rather than deferred.
- **Live run against `INDICI_BI_Full`:** `scripts/verify_env.ps1` exit 0. 2,368 surface objects (320 views, 2,048 tables); all 54 export names now `found_as_view` (D-001's exclusions mean 0 discrepancies remain); row stats for 54 in-scope objects — 53 exact, `dbo.TimeLine` indeterminate (COUNT exceeded 15 s, F6, matches 2026-07-14 finding); 11 watermark-less objects (was 12 of 56 pre-D-001), exact match to the deterministic recount already logged in `ARCHITECTURE.md`/`DECISIONS.md`.
- `artifacts/env-report.json` + `artifacts/env-report.md` written; `python -m cdss.validate_report artifacts/env-report.json` → `VALID`.
- Audit trail: `artifacts/audit/source-audit-20260715.jsonl`, 104 lines. **Caveat (exit criterion 4):** 105 statements were attempted (54 COUNT + 43 watermark + 5 version + 3 catalog); `dbo.TimeLine`'s timed-out `COUNT(*)` produced no audit line — `AuditedSourceConnection.execute_query()` only writes the audit event after a statement completes successfully, so a timeout is unaudited rather than logged-as-failed. This is pre-existing behavior from step 3 (already gated), not introduced by step 8; flagging rather than silently patching it, since it touches an already-approved component outside step 8's stated scope.
- Gate green throughout: 72 tests (was 56), mypy strict clean, ruff clean.

## Last completed work (2026-07-15, D-001)

D-001 resolved: `dbo.PracticeStats` and `AIFinanceAssistant.tblSalary` (base-table-only, no view equivalent) dropped from the CDSS scope by editing `schema_for_SQL_PROJ.txt` (56 → 54 entries). Source database untouched — no DDL/DML, nothing dropped in `INDICI_BI_Full`. Gate re-verified green (56 tests, mypy strict, ruff clean). See `DECISIONS.md` D-001 (now DECIDED) and D-010 (partially decided).

## Last completed work (2026-07-14)

Step 7 — row counts + watermark candidates (`src/cdss/rowstats.py`):
- Live run over all 56 in-scope objects in 85.8 s; 55/56 exact `COUNT(*)`; `dbo.TimeLine` indeterminate (view COUNT exceeded 15 s timeout — reported as indeterminate per F6, never guessed).
- 12 watermark-less objects confirmed live, matching the D-012 risk note exactly.
- Full evidence trail: `artifacts/audit/source-audit-20260714.jsonl` (119 statements).
- Gate green: 56 tests passing, mypy strict, ruff clean (`scripts/check.ps1` exit 0).

Earlier the same day: steps 1–6 (scaffolding; config loader; audited SQL-guard access layer; live version capture — SQL Server 2019 CU32 Developer Edition; surface enumeration — 2,368 visible objects, 320 views / 2,048 base tables, all selectable; D-001 reconciliation — 54/56 found as views, `dbo.PracticeStats` and `AIFinanceAssistant.tblSalary` found as base tables, 0 missing).

## Current branch

`master` — Phase 0 code (`pyproject.toml`, `src/`, `tests/`, `scripts/`, `uv.lock`, `.env.example`) is **not yet committed**. Doc restructure complete (2026-07-15): `CLAUDE.md`, `PROJECT_STATE.md`, `ARCHITECTURE.md` (reshaped into Diagrams/Components/APIs/Dependencies), `DECISIONS.md` (ADR-style, index table added), `SESSION.md` (format-reviewed) — all 5 of 5 files done. See `SESSION.md` → "Resume here".

## Current milestone

**Phase 0 exit** — all 8 steps done; D-001 sign-off recorded. 6 of 7 exit criteria cleanly verified; criterion 4 (audit line count = statement count) has the `dbo.TimeLine` timeout caveat above. Declaring the phase formally exited is a product-owner decision, not yet taken.

## Current blockers

| ID | Blocker | Blocks |
|----|---------|--------|
| — | Formal Phase 0 exit declaration — awaiting product-owner review of the exit-criteria checklist (incl. the criterion-4 caveat) | Phase 1 start |

Non-blocking housekeeping: stray untracked files `echo` and `.coverage` in repo root (flagged 2026-07-14; `.coverage` should be gitignored, `echo` looks accidental — awaiting owner decision). All Phase 0 code + docs remain uncommitted on `master`.

## Next task

Awaiting direction: (a) product-owner review/declaration of Phase 0 exit (with a ruling on the criterion-4 audit-gap caveat — accept as-is, or authorize a fix to `AuditedSourceConnection.execute_query()` to audit failed/timed-out statements too), (b) the commit decision for all uncommitted Phase 0 work, or (c) something else the product owner directs.
