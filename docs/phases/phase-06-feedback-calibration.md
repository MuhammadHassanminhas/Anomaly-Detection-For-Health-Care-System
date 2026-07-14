# Phase 6 — Feedback & calibration

## Objective

Close the F5 loop: reason-coded dismissal handling, per-(practice, check) precision tracking, auto-demotion below the floor, and scheduled parameter recalibration — the alert-fatigue defense, built as a first-class subsystem.

## Preconditions

- Phase 5 closed. Phase 3 rails in place: reason-code CHECK constraint, append-only `finding_events`, `practice_check_config`, `precision_stats`, `calibration_runs`.
- D-011 defaults (floor 0.30, trailing window 50, min 10 events; all config, not constants) stand as proposed until you amend them — final confirmation due by Phase 8 gate.

## Steps (one deliverable each; TDD throughout)

1. **Feedback service.** `cdss.feedback`: dismiss(finding, reason_code, actor, note?) / acknowledge / snooze(until) / reopen — each writes the lifecycle transition + append-only event; dismissal without a valid reason code is rejected at the service layer (the DB constraint from Phase 3 is the backstop, this is the front door the API will call in Phase 9).
   *Deliverable:* service + tests for every transition, invalid-reason rejection, and event ordering.
2. **Precision computation job.** `cdss.calibration.precision`: per (practice, check), over the trailing window of reason-coded feedback events (window size + min-n from config): precision = `genuine_issue / all reason-coded feedback`; writes `precision_stats` rows (window bounds, n, precision, computed_at). Deterministic and idempotent — recomputing the same window rewrites identical rows.
   *Deliverable:* job + tests against hand-computed fixture feedback histories, incl. window-boundary and below-min-n cases (no stat row ⇒ no demotion possible).
3. **Auto-demotion (F5).** Below-floor precision with ≥ min-n events ⇒ `practice_check_config.demoted=true` (+ demoted_at, reason snapshot: the stats that triggered it), **for that practice only** — check stays live elsewhere; executor (Phase 3 loader) already skips demoted (practice, check) pairs — add the test proving it. Demotion emits a run-report line and an admin-visible record; **re-promotion is human-only** (via config/admin action, never automatic), so a flapping check cannot oscillate.
   *Deliverable:* demotion job + executor-skip test + no-auto-repromotion test.
4. **Recalibration job (F4).** `cdss.calibration.recalibrate`: scheduled re-run of the Phase 4 learning logic per (practice, percentile-param); writes `calibration_runs` (params_before/after, distribution snapshot); applies the new value only if `params_source != manual` (human-set params are never overwritten silently); every applied shift appears in the run report — thresholds never widen silently.
   *Deliverable:* job + tests: shifted fixture distribution ⇒ recorded before/after ⇒ new param used by the next compiled execution; manual params untouched.
5. **Indeterminacy + feedback interplay.** Dismissals with reason `data_entry_lag` feed a per-check aging hint (report-only in this phase — a candidate input to future lag-param recalibration, not an automatic one); reason-code distribution per check surfaces in the run report (a check dismissed mostly as `policy_difference` is a calibration candidate, not a demotion candidate).
   *Deliverable:* reason-distribution section in the run report.
6. **End-to-end loop demonstration.** Scripted scenario on the fixture DB: seeded findings → simulated reason-coded feedback stream (clearly synthetic, labeled) → precision drops below floor for practice A only → demotion observed for A, check still firing for practice B → separately, drifted fixture distribution → recalibration shifts a param → next run's compiled SQL uses the new value (asserted via `sql_hash` change + bound-param log).
   *Deliverable:* `tests/e2e/test_feedback_loop.py` — the brief's exit evidence ("simulated feedback drives an observable demotion and a parameter shift").

## Exit criteria

1. `scripts/check.ps1` exits 0, including the full loop e2e test (step 6).
2. Demotion demonstrated: below-floor practice demoted, other practice unaffected, executor provably skips the demoted pair, no auto-re-promotion.
3. Parameter shift demonstrated: recalibration records before/after and the next execution binds the new value; manual params proven untouched.
4. Precision job idempotency: recompute of an identical window produces identical stats (test-enforced).
5. Run report shows precision, reason-code distribution, demotions, and parameter shifts for the fixture scenario.
6. All jobs runnable by one command each (`python -m cdss.calibration.*`), schedulable without manual steps.

## Verification (gatekeeper commands)

```powershell
.\scripts\check.ps1
python -m pytest tests/e2e/test_feedback_loop.py -v
python -m cdss.calibration.precision --dry-run
python -m cdss.calibration.recalibrate --dry-run
```

## Risks / dependencies / open questions

- **Precision denominator gaming/starvation:** a practice that never leaves feedback yields no precision stats ⇒ no demotion — alert fatigue can persist invisibly. Mitigation: run report flags (practice, check) pairs with high open-finding volume and zero feedback; UI (Phase 10) will nudge. Not solvable by code alone — noted for operator runbooks (Phase 11).
- **Small-n instability:** min-n=10 guards the floor decision; the window is event-count-based (trailing 50), not time-based, so slow practices are judged on the same evidence mass.
- **Demotion masking real regressions:** a data-quality regression could crater precision and demote a *correct* check. Demotion reason snapshot + admin surfacing keeps the human in the loop; re-promotion is deliberately human-only.
- **D-011 numbers are proposals** — everything is config; your Phase 8-gate confirmation (or amendment) changes values, not code.
- **Depends on:** Phases 3–5.
- No new `DECISIONS.md` entries required by this spec.
