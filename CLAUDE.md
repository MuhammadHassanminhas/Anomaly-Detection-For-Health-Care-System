# CLAUDE.md — CDSS standing instructions (every session, every phase)

## 1. AI instructions

CDSS detects operational/clinical-workflow anomalies in the healthcare BI database `INDICI_BI_Full` (MS SQL Server) and produces explainable, actionable recommendations. LLMs **author** checks (offline, human-reviewed) and **narrate** findings (constrained, validated); deterministic SQL **decides** what is anomalous.

Document map — read before acting, update them as work progresses:

| File | Holds |
|------|-------|
| `CLAUDE.md` | AI instructions, coding rules, workflow, guardrails (this file; **< 300 lines** at all times) |
| `PROJECT_STATE.md` | Current phase, last completed work, current branch, current milestone, current blockers, next task |
| `ARCHITECTURE.md` | Diagrams, components, APIs, dependencies |
| `DECISIONS.md` | Decision log: `D-NNN` decisions, `ASK-NNN` blocked asks against the source-DB team |
| `SESSION.md` | Running session log: what happened, evidence, where to resume |
| `PLAN.md` | Thin phase index only — exactly one `CURRENT` phase; never inline phase detail |
| `docs/phases/phase-NN-<name>.md` | One spec per phase: objective, steps, exit criteria, verification commands, risks |

## 2. Coding rules
### Stop-and-wait discipline 
 - Finish a unit of work → present evidence (command outputs, file paths, test results) → **WAIT for explicit approval**.
 - Never start the next phase or mark a later phase begun; PLAN.md shows exactly one CURRENT.
 - Never start a new step withouth my approval ask my explicit persmission.  
 - Within a phase: one step at a time — complete it, show its deliverable and measured delta, then proceed. 
 - One change at a time; every optimization/refactor justified by a measured delta. 
### TDD and change discipline
- **TDD**: failing test first for every behavior, minimal code to green, then refactor.
- One change at a time; every optimization/refactor justified by a measured delta.
- Anything that runs repeatedly is fully scripted; manual actions only at explicit human approval gates.
- Gate before any step is called done: `scripts/check.ps1` (ruff + mypy strict + pytest) exits 0.

### Views-only rule (hard constraint)
- All source-DB reads are single `SELECT` statements against the provided views. **Never** query base tables; **never** issue DDL/DML against the source, even if visible. System-catalog metadata reads are permitted per D-015, and are audited.
- If a needed field is not exposed by any view: record a blocker `ASK-NNN` in `DECISIONS.md` and propose the view change — never work around it.
- All system-owned state lives in the separate app database. The source database is never written to.
- Every source statement is audited (statement, params, timestamp, duration, rows, component) — JSONL from day one, mirrored to `source_audit_log` from Phase 3 (D-016).

### Accuracy rules (absolute)
- Every number, date, code, count, or factual claim shown to a user is computed deterministically by code and reproducible by re-running a recorded query.
- Zero fabricated or estimated values anywhere — including tests, seed data labeled as real, logs, and documentation. Synthetic fixture data is always clearly labeled synthetic.
- The LLM never decides at runtime whether a record is anomalous, and never emits a number/date/code/count into user-facing output — the narration validator blocks any token not traceable to the evidence allowlist (F8).
- `schema_for_SQL_PROJ.txt` is unverified documentation (D-017): the Phase 1 empirical catalog is the sole authority the compiler accepts.
- Three-valued evaluation everywhere (F6): missing prerequisite data ⇒ indeterminate, never a flag.

### Secrets and PHI
- Secrets via environment variables (`CDSS_SOURCE_*`, `CDSS_APP_DB_URL`, `CDSS_LLM_API_KEY`); `.env` is gitignored; never log a secret value — only variable names.
- PHI: Tier S (narration — no values reach the LLM) / Tier M (offline authoring — aggregates only) per D-003; do not pass the Phase 5 gate without product-owner sign-off on the policy.

## 3. Workflow

### Stop-and-wait discipline (applies to every step and every phase)
- Finish a unit of work → present evidence (command outputs, file paths, test results) → **WAIT for explicit approval**. This applies at **every step boundary within a phase**, not only at phase boundaries (product-owner directive, 2026-07-14): do not start the next step's task, code, or commands until told to proceed.
- Never start the next phase or mark a later phase begun; `PLAN.md` shows exactly one `CURRENT`.
- If reality contradicts the plan or findings F1–F12: stop, write the `DECISIONS.md` entry, update `PLAN.md` + the affected phase spec, and ask. Never improvise silently.
- Keep `PROJECT_STATE.md` current: update it when a step completes, a blocker appears/clears, or the current task changes.
- Append to `SESSION.md` at each approval gate: what was done, evidence location, what comes next.

### Gated planning (Stage A rules — apply to any future spec revision)
- Plan **one phase per response**. Never draft a phase spec before the previous one is explicitly approved. Never skip the re-evaluation step (re-read skeleton, schema inventory, approved specs, `DECISIONS.md`; state in 3–5 lines whether anything changes this phase).
- Each phase lives in its own file `docs/phases/phase-NN-<name>.md`: objective, small ordered steps (one deliverable each), objectively verifiable exit criteria, exact verification commands, risks/dependencies/open questions.
- `PLAN.md` stays a thin index — never inline phase detail into it or merge specs into one big file.

## 4. Guardrails

### Prohibitions (from the brief — never violate)
1. No planning multiple phases in one response; no spec before prior approval; no skipping re-evaluation.
2. No merging phase specs; `CLAUDE.md` < 300 lines.
3. No base-table queries; no DDL/DML on the source database.
4. No per-row or per-record LLM calls, ever. LLM cost is O(checks + findings), never O(rows).
5. No LLM-generated numbers, dates, codes, or counts in user-facing output — validator-enforced.
6. No staff-facing alerts from the discovery lane (F9): no write path from discovery to `findings`.
7. No fabricated metrics, benchmarks, or test data presented as real.
8. No skipping the human review gate for any check, regardless of source.
9. No advancing a phase or a step — planning or execution — without explicit approval.
10. No clinical diagnoses or treatment advice in recommendations — operational actions only, human in the loop.

### Recording deviations
Any revision of a brief default or finding F1–F12 requires a `DECISIONS.md` entry (`D-NNN`): what was observed, why the default fails, the replacement. Asks against the source-DB team are `ASK-NNN` entries in Part 3 of `DECISIONS.md`. Silent deviation is a violation.
