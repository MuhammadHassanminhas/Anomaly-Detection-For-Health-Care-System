# DECISIONS.md — Decision Log

Single log of (a) open decisions awaiting the product owner, (b) decisions made by engineering within its remit, (c) blocked asks against the source-database team. Nothing in this project deviates from the brief or from findings F1–F12 without an entry here.

**Entry rules.** Every entry has an ID (`D-NNN`), a status (`OPEN` / `DECIDED` / `SUPERSEDED`), what it blocks, and — for any revision of a brief default — what was observed, why the default fails, and the replacement. Asks against the source DB team live in [Blocked asks](#blocked-asks) with ID `ASK-NNN`.

---

## Part 1 — OPEN decisions (need product-owner input)

### D-001 — Confirm the view surface (what exactly are "the provided views"?) · OPEN
- **Blocks:** Phase 0 exit. Phase 0 can start, but cannot pass its gate without this.
- **Context:** The only schema input, `schema_for_SQL_PROJ.txt`, lists **56 objects named like base tables** (`dbo.Appointments`, `dbo.Patient`, …; one is `AIFinanceAssistant.VU_PracticeTotalExpenses`). Each entry is labeled `"table"`. The brief's hard constraint 1 says all reads go through **provided views only** and never base tables.
- **Needed from you:** One of:
  1. Confirmation that these 56 names ARE the read-only views exposed in `INDICI_BI_Full` (i.e., the BI database exposes view objects under these names), or
  2. The actual list of view names (and, if different, the mapping to these 56), or
  3. Confirmation that the read-only account can only see the permitted views, so live enumeration (Phase 0) is authoritative.
- **Default if silent:** none — this is a hard blocker for Phase 0 exit. Phase 0 will enumerate what the account can actually see and reconcile against the 56 names, then stop for your confirmation.

### D-002 — Source connection details + SQL Server version · OPEN
- **Blocks:** Phase 0 start.
- **Needed from you (per brief §2, at Phase 0 start):** server host/port, database name confirmation (`INDICI_BI_Full`), auth method (SQL login vs Windows/Entra), the read-only credentials (delivered out-of-band, consumed via environment variables only), TLS requirements, and SQL Server version/edition (affects T-SQL features the compiler may emit, e.g., `STRING_AGG`, `DATEDIFF_BIG`).

### D-003 — PHI redaction policy (constraint 5 sign-off) · OPEN
- **Blocks:** Phase 5 gate (hard, per brief constraint 5). Also shapes D-004 stakes.
- **Proposal (recommended):** two tiers, both configurable, defined precisely in `ARCHITECTURE.md` §7:
  - **Tier S — narration (default):** the LLM composing finding narratives receives **field names, types, and placeholder tokens only — zero actual values, zero identifiers, zero free text**. Real values are interpolated deterministically after the LLM returns a template. Under Tier S, no PHI ever reaches the LLM at runtime.
  - **Tier M — offline authoring & discovery characterization:** the LLM receives the semantic catalog (column names, types, null rates, cardinalities, enumerated code/status domains) and **aggregate** statistics. No row-level records, no identifiers, no free-text field contents.
- **What needs sign-off:** adopt Tier S/Tier M as stated, or specify a stricter/looser policy. Also: whether practice names and provider names count as PHI-equivalent for this deployment (default: treated as restricted, pseudonymized in Tier M contexts).

### D-004 — LLM provider, model, hosting boundary · OPEN
- **Blocks:** Phase 4 (LLM-drafted checks), Phase 5 (narration), Phase 7 (discovery characterization). Non-LLM work in those phases proceeds.
- **Recommendation:** Anthropic API, `claude-sonnet-5` for check drafting and narrative templates (authoring quality matters; volume is O(checks + findings), tiny). Keys via environment variables. Under Tier S redaction (D-003) no PHI leaves the boundary at runtime, so the hosting question mainly concerns Tier M catalog metadata.
- **Alternatives:** AWS Bedrock `ap-southeast-2` (Sydney) or Azure-hosted models if data-residency policy requires all metadata to stay in-region.
- **Needed from you:** provider + model approval, and whether catalog metadata (column names/statistics, no patient rows) may leave the deployment network.

### D-005 — Application database engine · OPEN
- **Blocks:** Phase 3 start (migrations are written against the chosen engine).
- **Recommendation:** **PostgreSQL 16** — first-class JSONB for check definitions/evidence/audit payloads, transactional DDL for migrations, trivially containerized for dev/CI, zero licence cost.
- **Alternative:** SQL Server (a second database on the existing estate) — one operational stack for your DBAs, at the cost of clunkier JSON handling and licence/instance considerations. Viable; say the word and Phase 3 targets it instead.

### D-006 — Deployment target + CI runner · OPEN
- **Blocks:** Phase 11 (hard); influences container/runtime assumptions from Phase 3 on.
- **Recommendation:** Docker Compose on an on-network Linux VM (co-located with the SQL Server for latency and so PHI never leaves the network); GitHub Actions for CI if this repo lands on GitHub, otherwise the repo's `scripts/ci.ps1` runs the identical gate locally/on any runner.
- **Needed from you:** where this will actually run (on-prem VM / Azure / other), and where the repo will be hosted.

### D-007 — UI authentication model · OPEN
- **Blocks:** Phase 11 (real authN); Phase 9 builds a pluggable auth layer with a stub so API work is not blocked.
- **Recommendation:** OIDC against Microsoft Entra ID if the practices already live in M365 (typical for NZ general practice); role claims → `triage_user` / `check_reviewer` / `admin`.
- **Fallback:** local accounts (argon2id hashes) with the same role model.

### D-008 — Organization / tenant model · OPEN
- **Blocks:** Phase 3 schema finalization (soft — a default is proposed).
- **Evidence:** `PracticeID` appears on **56 of 56** exported objects; the source is inherently multi-practice.
- **Recommendation:** practice-scoped from day one: every app-DB row that is per-org carries `practice_id`; check parameters, calibration, precision tracking, and demotion are all per-practice (this is exactly F4/F5's "per-organization"). Single deployment, no per-tenant infrastructure. UI gets a practice filter; auth can later restrict users to practices.
- **Needed from you:** confirm, and confirm findings triage is done practice-by-practice (vs. one merged queue).

### D-009 — Test SQL Server + evaluation test copy · OPEN
- **Blocks:** Phase 2 exit (compiled SQL must execute against a real SQL Server), Phase 8 entirely.
- **Two needs:**
  1. **Fixture instance (Phases 2–7, CI):** a disposable SQL Server holding tiny **synthetic** datasets shaped like the views. Recommendation: `mcr.microsoft.com/mssql/server:2022` via Docker. **Question: is Docker Desktop (or any container runtime) available on the dev machine / CI runner?** If not: SQL Server Express or LocalDB installed locally is the fallback.
  2. **Test copy (Phase 8, recall measurement):** a restored copy/subset of `INDICI_BI_Full` that we MAY mutate (inject synthetic anomalies). The production source is never written. **Question: who can provision this restore, and where?**

### D-010 — Scope exclusions · OPEN
- **Blocks:** nothing until Phase 1 (profiling scope list is an input).
- **Recommendation:** exclude `AIFinanceAssistant.tblSalary` (staff salary — HR-sensitive, no clinical/operational anomaly value proportional to its sensitivity). Everything else stays in scope, including finance aggregates (`dbo.OutStandingBalance`, `dbo.agedbalancesummaryreportmonthly`, `dbo.PracticeStats`), which serve as cross-check targets for consistency checks.
- **Needed from you:** confirm the exclusion (or direct otherwise).

### D-011 — Operating parameters (defaults proposed; confirm or amend) · OPEN
- **Blocks:** nothing now; must be confirmed by the Phase 8 gate (they parameterize the harness and load tests).
- **Proposed defaults:**
  - **Run cadence:** nightly incremental at 02:00 local + on-demand manual trigger.
  - **Findings retention:** dismissed/resolved findings kept 24 months, then archived out of the live queue tables.
  - **Precision floor (F5):** auto-demote a check for a practice when precision < 0.30 over the trailing 50 feedback events (minimum 10 events before any demotion).
  - **Load targets (contractual, to be met in Phase 11):** nightly run completes < 30 min at current source volumes (measured in Phase 0/1, recorded in the environment report); API p95 < 500 ms for queue queries at 20 concurrent users; UI first meaningful paint < 3 s on the practice network. These are **proposed requirements, not measurements**; actuals get measured against them in Phases 8/11.

---

## Part 2 — DECIDED (engineering remit; flag if you disagree)

### D-012 — Adopt F1–F12 unrevised · DECIDED
Nothing observed in the schema export contradicts any of F1–F12. All twelve are adopted as stated in the brief; `ARCHITECTURE.md` §2 maps each to components and phases. Two risk notes (not revisions): 12 of 56 objects lack `InsertedAt`/`UpdatedAt` (F10 watermark fallback needed — see `docs/phases/phase-03-app-db-executor.md`), and F3a containment analysis must respect per-view cost budgets (F10).

### D-013 — Phase skeleton resized: brief's Phase 9 split into Phase 9 (API) + Phase 10 (UI); hardening becomes Phase 11 · DECIDED
The brief sizes phases to one working session and marks the skeleton "revise as evidence demands". FastAPI backend + React UI + full E2E in one session is not credible. No F-finding is affected; total scope is identical. Final numbering: 0–11 (see `PLAN.md`).

### D-014 — Implementation stack · DECIDED
Python 3.12+; FastAPI; SQLAlchemy Core + Alembic (app DB); `pyodbc` + ODBC Driver 18 (source, read-only); `sqlglot` for SQL-guard parsing; pytest (+coverage); ruff; mypy (strict on `src/`); YAML check-DSL validated by a JSON Schema; React 18 + Vite + TypeScript; Playwright for E2E; structlog for JSON logs. Rationale: brief already fixes FastAPI/React; the rest is the boring, well-supported center of that ecosystem. Any change later requires a superseding entry.

### D-015 — System-catalog metadata reads are permitted · DECIDED (flag if you disagree)
Enumerating the visible view surface and column types requires reading `INFORMATION_SCHEMA.VIEWS` / `INFORMATION_SCHEMA.COLUMNS` / `sys.*` catalog views. These are metadata about the permitted views, not base-table data reads; constraint 1 is interpreted as permitting them. Every such statement is still audited (constraint 7). If you disagree, Phase 0 falls back to the static 56-name list from the export and `SELECT TOP 0` probes per view.

### D-016 — Audit-log sink: JSONL file from Phase 0; mirrored to app DB from Phase 3 · DECIDED
Constraint 7 requires auditing every source-DB statement from the first connection — before the app DB exists (Phase 3). Sink is an append-only JSONL artifact (`artifacts/audit/source-audit-YYYYMMDD.jsonl`) from day one; from Phase 3 the same events also land in the `source_audit_log` table. The JSONL trail remains permanently (file trail survives app-DB restores).

### D-017 — `schema_for_SQL_PROJ.txt` is unverified documentation, never an authority · DECIDED
The export shows signs of tool generation: entries labeled `"table"`, a trailing empty string in every `columns` array, prose summaries with empty metadata on 7 entries, UTF-8 read artifacts, no data types anywhere. It is used only to seed Phase 1 hypotheses (names, plausible relations, value domains). The **empirical semantic catalog built in Phase 1 from the live database is the sole authority** the check compiler accepts. Where export and live schema disagree, the live schema wins and the discrepancy is logged in the Phase 1 report.

---

## Part 3 — Blocked asks (source-DB team) {#blocked-asks}

Requests for view changes, indexes, or new columns. Never worked around against base tables (constraint 1).

| ID | Status | Ask | Motivation | Raised |
|----|--------|-----|------------|--------|
| — | — | *(none yet)* | | |

Template: `ASK-NNN · status · exact view/column change requested · which check/measurement needs it · cost evidence (from executor cost report)`.
