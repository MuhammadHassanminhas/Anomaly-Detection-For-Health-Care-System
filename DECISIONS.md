# DECISIONS.md — Decision Log

Single log of (a) open decisions awaiting the product owner, (b) decisions made by engineering within its remit, (c) blocked asks against the source-database team. Nothing in this project deviates from the brief or from findings F1–F12 without an entry here.

**Entry rules.** Every entry has an ID (`D-NNN`), a status (`OPEN` / `PARTIALLY DECIDED` / `DECIDED` / `SUPERSEDED`), what it blocks, and — for any revision of a brief default — what was observed, why the default fails, and the replacement. Each entry follows a fixed field order (Status → Blocks → Context → Decision/Recommendation → Evidence/Consequences) so any entry can be scanned the same way. Asks against the source DB team live in [Blocked asks](#blocked-asks) with ID `ASK-NNN`.

## Decision register

Quick-scan index; full entries follow below.

| ID | Title | Status | Blocks |
|----|-------|--------|--------|
| [D-001](#d-001) | Confirm the view surface | **DECIDED** 2026-07-15 | Phase 0 exit — unblocked |
| [D-002](#d-002) | Source connection details + SQL Server version | **DECIDED** 2026-07-14 | Phase 0 start — unblocked |
| [D-003](#d-003) | PHI redaction policy (constraint 5 sign-off) | OPEN | Phase 5 gate |
| [D-004](#d-004) | LLM provider, model, hosting boundary | OPEN | Phases 4, 5, 7 (LLM work only) |
| [D-005](#d-005) | Application database engine | OPEN | Phase 3 start |
| [D-006](#d-006) | Deployment target + CI runner | **PARTIALLY DECIDED** 2026-07-15 | Phase 11 |
| [D-007](#d-007) | UI authentication model | OPEN | Phase 11 (Phase 9 stubs) |
| [D-008](#d-008) | Organization / tenant model | OPEN | Phase 3 schema (soft) |
| [D-009](#d-009) | Test SQL Server + evaluation test copy | OPEN | Phase 2 exit, Phase 8 |
| [D-010](#d-010) | Scope exclusions | **PARTIALLY DECIDED** 2026-07-15 | Phase 1 input |
| [D-011](#d-011) | Operating parameters | OPEN | Phase 8 gate |
| [D-012](#d-012) | Adopt F1–F12 unrevised | **DECIDED** | — |
| [D-013](#d-013) | Phase skeleton resized (9 → 9+10, hardening → 11) | **DECIDED** | — |
| [D-014](#d-014) | Implementation stack | **DECIDED** | — |
| [D-015](#d-015) | System-catalog metadata reads permitted | **DECIDED** | — |
| [D-016](#d-016) | Audit-log sink: JSONL → mirrored to app DB | **DECIDED** | — |
| [D-017](#d-017) | `schema_for_SQL_PROJ.txt` is unverified, never an authority | **DECIDED** | — |

---

## Part 1 — Product-owner decisions (open or decided)

### D-001
**Confirm the view surface (what exactly are "the provided views"?)**
**Status:** DECIDED (2026-07-15) · **Blocks:** Phase 0 exit — now unblocked.

**Context.** The only schema input, `schema_for_SQL_PROJ.txt`, lists **56 objects named like base tables** (`dbo.Appointments`, `dbo.Patient`, …; one is `AIFinanceAssistant.VU_PracticeTotalExpenses`). Each entry is labeled `"table"`. The brief's hard constraint 1 says all reads go through **provided views only** and never base tables. Live reconciliation (Phase 0 step 6) found 54/56 export names resolve to actual views; two — `dbo.PracticeStats` and `AIFinanceAssistant.tblSalary` — exist only as base tables, with no view equivalent found on the visible surface.

**Decision.** Drop both `dbo.PracticeStats` and `AIFinanceAssistant.tblSalary` from the CDSS scope. This is a **document/scope change only** — the two entries were removed from `schema_for_SQL_PROJ.txt` (54 objects remain); nothing was dropped or altered in `INDICI_BI_Full` itself, and the views-only constraint is preserved untouched (constraint 1 stays "never base tables," satisfied by exclusion rather than exception). `tblSalary` exclusion also satisfies D-010 (HR-sensitive, already recommended for exclusion). `PracticeStats` exclusion removes a finance/operational aggregate from scope — if it's later needed, the correct path is an `ASK-NNN` for a source-DB team-provided view, never a base-table read.

**Evidence.** `python -c "import json; print(len(json.loads(open('schema_for_SQL_PROJ.txt', encoding='utf-8').read())))"` → `54`. Gate re-verified green after the edit: 56 tests passing, mypy strict clean, ruff clean (`scripts/check.ps1` exit 0) — no test hardcoded the removed names or the count of 56.

### D-002
**Source connection details + SQL Server version**
**Status:** DECIDED (2026-07-14) · **Blocks:** Phase 0 start — now unblocked.

**Decision.** Host `192.168.0.9`, port `1433`, database `INDICI_BI_Full`, auth method **Windows/Integrated Authentication** (no SQL login — no username/password; the process's Windows identity carries the read-only grant), `Encrypt=yes` with `TrustServerCertificate=yes` (internal on-prem instance, typical for a self-signed/AD-issued cert not in the client's trust store). SQL Server version/edition not a precondition — captured live in Phase 0 step 4 (`SELECT @@VERSION` / `SERVERPROPERTY`).

**Consequences.** Config schema: the phase-00 spec's precondition list (`CDSS_SOURCE_USER`, `CDSS_SOURCE_PASSWORD` unconditionally required) is revised — added `CDSS_SOURCE_AUTH` (`windows` | `sql`); `CDSS_SOURCE_USER`/`CDSS_SOURCE_PASSWORD` are required only when `auth=sql`. New var `CDSS_SOURCE_TRUST_SERVER_CERTIFICATE` (bool) added alongside `CDSS_SOURCE_ENCRYPT`. `docs/phases/phase-00-environment-access.md` preconditions updated to match.

### D-003
**PHI redaction policy (constraint 5 sign-off)**
**Status:** OPEN · **Blocks:** Phase 5 gate (hard, per brief constraint 5). Also shapes D-004 stakes.

**Recommendation.** Two tiers, both configurable, defined precisely in `ARCHITECTURE.md` §2.6 (Narration layer):
- **Tier S — narration (default):** the LLM composing finding narratives receives **field names, types, and placeholder tokens only — zero actual values, zero identifiers, zero free text**. Real values are interpolated deterministically after the LLM returns a template. Under Tier S, no PHI ever reaches the LLM at runtime.
- **Tier M — offline authoring & discovery characterization:** the LLM receives the semantic catalog (column names, types, null rates, cardinalities, enumerated code/status domains) and **aggregate** statistics. No row-level records, no identifiers, no free-text field contents.

**Needed from you.** Adopt Tier S/Tier M as stated, or specify a stricter/looser policy. Also: whether practice names and provider names count as PHI-equivalent for this deployment (default: treated as restricted, pseudonymized in Tier M contexts).

### D-004
**LLM provider, model, hosting boundary**
**Status:** OPEN · **Blocks:** Phase 4 (LLM-drafted checks), Phase 5 (narration), Phase 7 (discovery characterization). Non-LLM work in those phases proceeds.

**Recommendation.** Anthropic API, `claude-sonnet-5` for check drafting and narrative templates (authoring quality matters; volume is O(checks + findings), tiny). Keys via environment variables. Under Tier S redaction (D-003) no PHI leaves the boundary at runtime, so the hosting question mainly concerns Tier M catalog metadata.

**Alternatives.** AWS Bedrock `ap-southeast-2` (Sydney) or Azure-hosted models if data-residency policy requires all metadata to stay in-region.

**Needed from you.** Provider + model approval, and whether catalog metadata (column names/statistics, no patient rows) may leave the deployment network.

### D-005
**Application database engine**
**Status:** OPEN · **Blocks:** Phase 3 start (migrations are written against the chosen engine).

**Recommendation.** **PostgreSQL 16** — first-class JSONB for check definitions/evidence/audit payloads, transactional DDL for migrations, trivially containerized for dev/CI, zero licence cost.

**Alternative.** SQL Server (a second database on the existing estate) — one operational stack for your DBAs, at the cost of clunkier JSON handling and licence/instance considerations. Viable; say the word and Phase 3 targets it instead.

### D-006
**Deployment target + CI runner**
**Status:** PARTIALLY DECIDED (2026-07-15) · **Blocks:** Phase 11 (hard); influences container/runtime assumptions from Phase 3 on.

**Decided (partial).** Repo location: `https://github.com/MuhammadHassanminhas/Anomaly-Detection-For-Health-Care-System`, pushed 2026-07-15 (`main`, commit `cdc0652`). **Repo is currently public**, not private — flagged to the product owner (no `gh`/GitHub MCP available on this machine to set visibility via automation), who explicitly confirmed pushing while public rather than waiting. It contains real infra details (source DB internal IP, full schema/column names, connection config). Flipping to private later (github.com → Settings → General → Danger Zone) is a manual, low-risk action whenever wanted — nothing here depends on it being private.

**Recommendation (still open).** Docker Compose on an on-network Linux VM (co-located with the SQL Server for latency and so PHI never leaves the network); GitHub Actions for CI now that the repo is on GitHub.

**Needed from you.** Where this will actually run (on-prem VM / Azure / other), and whether to set the repo private.

### D-007
**UI authentication model**
**Status:** OPEN · **Blocks:** Phase 11 (real authN); Phase 9 builds a pluggable auth layer with a stub so API work is not blocked.

**Recommendation.** OIDC against Microsoft Entra ID if the practices already live in M365 (typical for NZ general practice); role claims → `triage_user` / `check_reviewer` / `admin`.

**Fallback.** Local accounts (argon2id hashes) with the same role model.

### D-008
**Organization / tenant model**
**Status:** OPEN · **Blocks:** Phase 3 schema finalization (soft — a default is proposed).

**Evidence.** `PracticeID` appears (case-insensitive; entries `PracticeID`/`practiceid`) on **54 of 54** currently-in-scope objects (recount after D-001; was 56 of 56 before the scope reduction) — the source is inherently multi-practice.

**Recommendation.** Practice-scoped from day one: every app-DB row that is per-org carries `practice_id`; check parameters, calibration, precision tracking, and demotion are all per-practice (this is exactly F4/F5's "per-organization"). Single deployment, no per-tenant infrastructure. UI gets a practice filter; auth can later restrict users to practices.

**Needed from you.** Confirm, and confirm findings triage is done practice-by-practice (vs. one merged queue).

### D-009
**Test SQL Server + evaluation test copy**
**Status:** OPEN · **Blocks:** Phase 2 exit (compiled SQL must execute against a real SQL Server), Phase 8 entirely.

**Two needs.**
1. **Fixture instance (Phases 2–7, CI):** a disposable SQL Server holding tiny **synthetic** datasets shaped like the views. Recommendation: `mcr.microsoft.com/mssql/server:2022` via Docker. **Question:** is Docker Desktop (or any container runtime) available on the dev machine / CI runner? If not: SQL Server Express or LocalDB installed locally is the fallback.
2. **Test copy (Phase 8, recall measurement):** a restored copy/subset of `INDICI_BI_Full` that we MAY mutate (inject synthetic anomalies). The production source is never written. **Question:** who can provision this restore, and where?

### D-010
**Scope exclusions**
**Status:** PARTIALLY DECIDED (2026-07-15) · **Blocks:** nothing until Phase 1 (profiling scope list is an input).

**Decided (via D-001).** `AIFinanceAssistant.tblSalary` is excluded — both for the original HR-sensitivity rationale and because D-001 found it has no view equivalent. `dbo.PracticeStats` is also now excluded (D-001: base-table-only, no view found), which supersedes the original recommendation that named it as an in-scope cross-check target.

**Still open.** Confirm everything else stays in scope, including the remaining finance aggregates (`dbo.OutStandingBalance`, `dbo.agedbalancesummaryreportmonthly`), which serve as cross-check targets for consistency checks.

### D-011
**Operating parameters**
**Status:** OPEN (defaults proposed; confirm or amend) · **Blocks:** nothing now; must be confirmed by the Phase 8 gate (they parameterize the harness and load tests).

**Proposed defaults.**
- **Run cadence:** nightly incremental at 02:00 local + on-demand manual trigger.
- **Findings retention:** dismissed/resolved findings kept 24 months, then archived out of the live queue tables.
- **Precision floor (F5):** auto-demote a check for a practice when precision < 0.30 over the trailing 50 feedback events (minimum 10 events before any demotion).
- **Load targets (contractual, to be met in Phase 11):** nightly run completes < 30 min at current source volumes (measured in Phase 0/1, recorded in the environment report); API p95 < 500 ms for queue queries at 20 concurrent users; UI first meaningful paint < 3 s on the practice network. These are **proposed requirements, not measurements**; actuals get measured against them in Phases 8/11.

---

## Part 2 — Engineering-remit decisions (flag if you disagree)

### D-012
**Adopt F1–F12 unrevised**
**Status:** DECIDED

**Decision.** Nothing observed in the schema export contradicts any of F1–F12. All twelve are adopted as stated in the brief; `ARCHITECTURE.md` §2.10 maps each to components and phases.

**Risk notes (not revisions).** 11 of 54 in-scope objects lack `InsertedAt`/`UpdatedAt` (recount after D-001; was 12 of 56) — F10 watermark fallback needed, see `docs/phases/phase-03-app-db-executor.md`; F3a containment analysis must respect per-view cost budgets (F10).

### D-013
**Phase skeleton resized: brief's Phase 9 split into Phase 9 (API) + Phase 10 (UI); hardening becomes Phase 11**
**Status:** DECIDED

**Decision.** The brief sizes phases to one working session and marks the skeleton "revise as evidence demands". FastAPI backend + React UI + full E2E in one session is not credible. No F-finding is affected; total scope is identical. Final numbering: 0–11 (see `PLAN.md`).

### D-014
**Implementation stack**
**Status:** DECIDED

**Decision.** Python 3.12+; FastAPI; SQLAlchemy Core + Alembic (app DB); `pyodbc` + ODBC Driver 18 (source, read-only); `sqlglot` for SQL-guard parsing; pytest (+coverage); ruff; mypy (strict on `src/`); YAML check-DSL validated by a JSON Schema; React 18 + Vite + TypeScript; Playwright for E2E; structlog for JSON logs.

**Rationale.** The brief already fixes FastAPI/React; the rest is the boring, well-supported center of that ecosystem. Any change later requires a superseding entry.

### D-015
**System-catalog metadata reads are permitted**
**Status:** DECIDED (flag if you disagree)

**Decision.** Enumerating the visible view surface and column types requires reading `INFORMATION_SCHEMA.VIEWS` / `INFORMATION_SCHEMA.COLUMNS` / `sys.*` catalog views. These are metadata about the permitted views, not base-table data reads; constraint 1 is interpreted as permitting them. Every such statement is still audited (constraint 7).

**Fallback if you disagree.** Phase 0 falls back to the static 54-name list from the export and `SELECT TOP 0` probes per view.

### D-016
**Audit-log sink: JSONL file from Phase 0; mirrored to app DB from Phase 3**
**Status:** DECIDED

**Decision.** Constraint 7 requires auditing every source-DB statement from the first connection — before the app DB exists (Phase 3). Sink is an append-only JSONL artifact (`artifacts/audit/source-audit-YYYYMMDD.jsonl`) from day one; from Phase 3 the same events also land in the `source_audit_log` table. The JSONL trail remains permanently (file trail survives app-DB restores).

### D-017
**`schema_for_SQL_PROJ.txt` is unverified documentation, never an authority**
**Status:** DECIDED

**Decision.** The export shows signs of tool generation: entries labeled `"table"`, a trailing empty string in every `columns` array, prose summaries with empty metadata on 7 entries, UTF-8 read artifacts, no data types anywhere. It is used only to seed Phase 1 hypotheses (names, plausible relations, value domains). The **empirical semantic catalog built in Phase 1 from the live database is the sole authority** the check compiler accepts. Where export and live schema disagree, the live schema wins and the discrepancy is logged in the Phase 1 report.

---

## Part 3 — Blocked asks (source-DB team) {#blocked-asks}

Requests for view changes, indexes, or new columns. Never worked around against base tables (constraint 1).

| ID | Status | Ask | Motivation | Raised |
|----|--------|-----|------------|--------|
| — | — | *(none yet)* | | |

Template: `ASK-NNN · status · exact view/column change requested · which check/measurement needs it · cost evidence (from executor cost report)`.
