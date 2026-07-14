# PLAN.md — CDSS (thin index; detail lives in docs/phases/, one file per phase)

## Open questions (full text in DECISIONS.md)

- **D-001** — Are the 56 exported objects the provided views? *(blocks Phase 0 exit)*
- **D-002** — Source connection details + SQL Server version *(blocks Phase 0 start)*
- **D-003** — PHI redaction policy sign-off *(blocks Phase 5 gate)*
- **D-004** — LLM provider/model/hosting boundary *(blocks LLM work in Phases 4/5/7)*
- **D-005** — App database engine *(blocks Phase 3 start)*
- **D-006** — Deployment target + CI runner *(blocks Phase 11)*
- **D-007** — UI authentication model *(blocks Phase 11; Phase 9 stubs)*
- **D-008** — Practice/tenant model confirmation *(soft-blocks Phase 3 schema)*
- **D-009** — Test SQL Server (fixtures) + mutable test copy *(blocks Phase 2 exit, Phase 8)*
- **D-010** — Scope exclusions (`AIFinanceAssistant.tblSalary`) *(input to Phase 1)*
- **D-011** — Operating parameters: cadence, retention, precision floor, load targets *(confirm by Phase 8 gate)*

## Phases

Numbering per D-013 (brief's Phase 9 split into API + UI; hardening becomes 11).
Spec files land under `docs/phases/` one at a time in Stage A1, each behind its own approval gate.

| # | Phase | Objective | Status | Spec |
|---|-------|-----------|--------|------|
| 0 | Environment & access verification | Scripted read-only connection; enumerate visible views and reconcile against the 56 export names; capture SQL Server version, row counts, watermark candidates; emit environment report. | **CURRENT** | *(pending A1)* |
| 1 | Schema intelligence | Automated profiling of every in-scope view → machine-readable semantic catalog + human-readable report, re-runnable by one command. | pending | — |
| 2 | Check DSL + compiler | YAML predicate DSL, deterministic compiler to T-SQL over views, three-valued evaluation semantics, golden-SQL tests. | pending | — |
| 3 | App database + executor | App-DB migrations (checks, findings, feedback, params, runs, audit); incremental watermark execution; dedup/snooze; per-check cost capture. | pending | — |
| 4 | Seed check library | Profiling-derived + LLM-drafted checks through the human review gate; per-practice parameter defaults learned from data. | pending | — |
| 5 | Explanation & recommendation layer | Deterministic evidence extraction; action library; constrained placeholder-template narration; exact-match validator; PHI redaction (Tier S/M). | pending | — |
| 6 | Feedback & calibration | Reason-coded dismissals; per-check-per-practice precision; auto-demotion below floor; parameter recalibration job. | pending | — |
| 7 | Discovery layer | Drift/outlier lane over engineered aggregates; LLM characterization; candidate-check drafting into the review gate; never alerts staff. | pending | — |
| 8 | Evaluation harness | Synthetic anomaly injection on the test copy; recall per check; precision from feedback; versioned gold sets; CI regression gate. | pending | — |
| 9 | API | FastAPI backend: findings queue, lifecycle actions, explanations, check management, review gate; pluggable auth stub. | pending | — |
| 10 | Triage UI | React UI: queue, evidence view, dismiss-with-reason, check admin, review gate, run dashboard; E2E test through real API. | pending | — |
| 11 | Production hardening | Real authN/Z, rate limits, observability, deployment artifacts, backup/restore, runbooks, docs, load test against D-011 targets. | pending | — |

## Protocol

Stage A1 next: one phase spec per response, each behind its own explicit approval gate; re-evaluate before each spec. Stage B (execution) begins only after every spec is approved and permission to enter is granted. `CURRENT` moves only at approved phase closes.
