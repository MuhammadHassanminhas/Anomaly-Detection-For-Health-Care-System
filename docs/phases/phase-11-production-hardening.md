# Phase 11 — Production hardening

## Objective

Make the working system operable and deployable: real authN/Z, rate limits, observability, scheduling, deployment artifacts, backup/restore, runbooks and docs, load test against the D-011 targets, and a completed security checklist — exiting with a documented clean-environment deployment.

## Preconditions

- Phases 0–10 closed. **D-006 ruled** (deployment target; spec assumes Docker Compose on an on-network Linux VM + GitHub Actions/`scripts/ci.ps1`); **D-007 ruled** (IdP; spec assumes OIDC/Entra ID); **D-011 confirmed** at the Phase 8 gate (targets below quote its defaults — amended numbers substitute mechanically).

## Steps (one deliverable each; TDD where testable, checklist-driven where not)

1. **Real authentication.** OIDC provider implementation behind the Phase 9 interface (Entra ID per D-007): token validation, role claims → `triage_user`/`check_reviewer`/`admin`, permitted-practices claim mapping; stub provider refused outside `CDSS_ENV=dev|test` (already enforced — now the production path exists). Fallback local-accounts provider (argon2id) implemented only if D-007 ruled it.
   *Deliverable:* provider + tests (expired/forged/wrong-audience tokens rejected); login E2E against a test IdP tenant.
2. **API hardening.** Rate limiting (per-user + per-IP, config-driven), security headers, CORS locked to the deployed UI origin, request-size limits, audit of authz on every route (automated route-inventory test: every route declares its role requirement or CI fails).
   *Deliverable:* hardening middleware + tests; route-inventory assertion green.
3. **Observability.** Prometheus metrics endpoint (run duration, per-check cost, findings by status, narration validation blocks, LLM failures, API latencies); structlog JSON everywhere with run/request ids; run dashboard (Phase 10) fed by real metrics; alert rules shipped as config (run failed, run overdue, narration block-rate spike, source connection failures).
   *Deliverable:* metrics + log schema documented; alert rules in repo; smoke test scrapes the endpoint.
4. **Scheduler.** Nightly run at the D-011 cadence (02:00 local) + calibration/precision/discovery/aggregate job schedules — in-container scheduler (cron/APScheduler per D-006 runtime), overlap-protected (a running run blocks the next; skip is logged + alertable), manual trigger via API (admin role).
   *Deliverable:* schedule config + overlap tests; manual-trigger E2E.
5. **Deployment artifacts.** Containerfiles (API+scheduler, UI static bundle behind a reverse proxy, app DB) + Compose stack; config solely via env vars/secret files (documented in `docs/configuration.md`, validated at boot with actionable errors); Alembic migrations run as an explicit, gated deploy step; TLS termination documented.
   *Deliverable:* `docker compose up` from a clean clone + documented env ⇒ healthy stack (healthchecks green).
6. **Backup/restore (app DB).** Scheduled dumps + retention; **restore drill scripted and rehearsed**: `scripts/restore_drill.ps1` restores a backup into a fresh instance and runs an integrity check (row counts, latest run present, migrations at head). The JSONL audit trail's independence from app-DB restores (D-016) verified in the drill.
   *Deliverable:* drill script passing against a real backup of the fixture-loaded DB.
7. **Load test against D-011 targets.** Realistic volumes (Phase 8's live-copy cost data sizes the dataset): nightly run < 30 min at current source volumes; API p95 < 500 ms for queue queries at 20 concurrent users; UI FMP < 3 s on the practice network (measured against the deployed stack). Misses ⇒ measured deltas + targeted fixes (one change at a time) or a documented `DECISIONS.md` re-negotiation of the target — never a quiet shrug.
   *Deliverable:* `artifacts/load/report.md` with measured numbers vs targets.
8. **Security checklist + PHI review.** Checklist executed and recorded (`docs/security-checklist.md`): secrets never in logs/images (scrub tests re-verified), app-DB access roles minimal, discovery role grants verified (F9), PHI-at-rest review (evidence JSONB minimality re-audited against active checks), dependency audit (pip/npm), TLS everywhere, audit-log completeness sampled against source statements.
   *Deliverable:* completed checklist with evidence links, dated.
9. **Runbooks + docs.** Operator runbook (deploy, upgrade+migrate, backup/restore, run failed, source schema drift event, LLM outage, demotion review, feedback-blind-spot follow-up — the Phase 6 handoff); user guide (triage workflow, reason codes and what they drive, review gate for check reviewers); `README.md` from-zero setup.
   *Deliverable:* docs complete; a person who is not the author deploys from docs alone (the step-10 rehearsal).
10. **Clean-environment deployment rehearsal (the exit).** On a fresh VM/environment per D-006: follow `docs/runbook-deploy.md` verbatim — clone, configure, deploy, migrate, connect to source (read-only), run the pipeline, triage a finding in the UI. Deviations found ⇒ docs fixed, rehearsal repeated until clean.
    *Deliverable:* rehearsal log (commands + outcomes) committed as `artifacts/deploy-rehearsal.md`.

## Exit criteria

1. `scripts/check.ps1` + `scripts/check_ui.ps1` + full E2E suites exit 0 on the final revision.
2. **Clean-environment deployment via documented steps succeeds** — rehearsal log committed, second run clean (brief's criterion).
3. **Load test meets the D-011 targets** (or a DECIDED re-negotiation entry exists) — report committed with real measurements.
4. **Security checklist complete** with evidence, including F9 grant verification and PHI-at-rest re-audit (brief's criterion).
5. Restore drill passes; scheduled runs execute unattended for ≥ 3 consecutive nights in the deployed environment (run reports as evidence).
6. Stub auth provably impossible in the production build; all routes role-inventoried.
7. All `DECISIONS.md` items status DECIDED (or explicitly deferred-with-owner); no open `ASK-NNN` without a recorded disposition.

## Verification (gatekeeper commands)

```powershell
.\scripts\check.ps1 ; .\scripts\check_ui.ps1
docker compose up -d ; docker compose ps            # all healthy
.\scripts\restore_drill.ps1
Get-Content artifacts/load/report.md
Get-Content artifacts/deploy-rehearsal.md
Get-Content docs/security-checklist.md
```

## Risks / dependencies / open questions

- **D-006/D-007 block start** of steps 1 and 5–10 in their final form; steps 2–4 proceed on any ruling.
- **Load-test misses are plausible** (views may hide expensive joins — F10): the path is measured deltas, per-check optimization asks (`ASK-NNN`), or target re-negotiation — all documented, none silent.
- **Test IdP tenant availability** (step 1) needs an Entra test app registration from your side if D-007 confirms OIDC.
- **Three-night soak (criterion 5) sets the minimum calendar length** of this phase — flagged so the gate isn't mistaken for slippage.
- **Depends on:** everything; this phase closes the project to production.
- No new `DECISIONS.md` entries required beyond those already scheduled to be DECIDED here.
