# Phase 10 — Triage UI

## Objective

React triage UI (React 18 + Vite + TypeScript, per D-014) over the Phase 9 API: findings queue, evidence/narrative view, dismiss-with-reason, check administration, review gate, and run dashboard — completing the brief's end-to-end exit criterion through the real UI (D-013 split).

## Preconditions

- Phase 9 closed: API serves everything the UI needs; `artifacts/openapi.json` committed.
- No new open decisions: auth uses the Phase 9 provider interface (stub in dev; real IdP is Phase 11 and swaps behind the same login flow).

## Steps (one deliverable each; component/E2E tests per step — Playwright against the real API + fixture DBs, no mocked API in E2E)

1. **Scaffold + generated client.** Vite + TS strict + ESLint/Prettier aligned with repo CI; API client **generated** from `openapi.json` (build step, checked-in output diffed in CI — drift ⇒ red build); auth context + login flow against the provider interface; practice selector bound to the token's permitted practices (D-008).
   *Deliverable:* `scripts/check_ui.ps1` (lint, typecheck, unit, build) green; client regeneration is a scripted step.
2. **Findings queue.** Practice-scoped queue: filters (status, category, severity, check), sort, keyset pagination, counts by status; snoozed hidden by default with an explicit toggle. States for loading/empty/error (no silent failure).
   *Deliverable:* queue page + component tests; Playwright: seeded fixture findings appear, filters/pagination behave.
3. **Finding detail — evidence view.** Deterministic evidence fields (typed rendering — dates, codes, counts exactly as stored), validated narrative with its `validation_status` visible (fallback renders are visibly marked as deterministic fallbacks), recommended actions from the check's allowlist, check rationale + resolution guidance, full lifecycle history.
   *Deliverable:* detail page + tests, incl. rendering of a `blocked_fell_back` narrative.
4. **Lifecycle actions + dismiss-with-reason.** Acknowledge / dismiss / snooze / reopen; dismissal modal **requires** a reason code (enum from the API) before submit enables; optional note; optimistic update with rollback on API error; 409 (already dismissed) surfaced honestly.
   *Deliverable:* Playwright: dismissal without reason impossible via the UI; with reason ⇒ queue updates and event history shows the actor.
5. **Check admin + review gate UI.** For `check_reviewer`/`admin`: drafts list, draft detail (definition, rationale, compiled SQL, fixture dry-run result), approve/reject/amend — same review service semantics as the CLI; active-check list with per-practice precision, demotion state (with the triggering stats), params (learned/manual provenance), enable/disable; discovery signal queue (admin only).
   *Deliverable:* review flow Playwright test: draft → approve in UI → check active; `triage_user` never sees admin/discovery routes (guard + API 403 both exercised).
6. **Run dashboard + feedback-blind-spot nudge.** Runs list + run report view (per-check cost, tri-state counts, narration stats, parameter shifts, demotions); the Phase 6 flag surfaces here: (practice, check) pairs with high open-finding volume and zero feedback rendered as an explicit "needs triage attention" banner on the queue.
   *Deliverable:* dashboard pages + the nudge banner driven by fixture data engineered to trigger it.
7. **Full-loop E2E (the brief's criterion, through the UI).** One Playwright scenario against real API + fixture DBs: seeded anomaly → pipeline run → finding visible in queue → detail shows validated narrative → dismiss with reason via UI → precision update visible in check admin; plus the demotion scenario surfacing in admin after the calibration job.
   *Deliverable:* `e2e/full-loop.spec.ts` green in CI (services orchestrated by script, no manual steps).

## Exit criteria

1. `scripts/check.ps1` and `scripts/check_ui.ps1` exit 0 (lint, typecheck strict, unit/component tests, production build).
2. **Full-loop Playwright E2E green in CI**: seeded anomaly → finding → narrative → dismissal-with-reason → precision update, all through the real API and UI (brief's exit criterion).
3. Dismissal without a reason code is impossible through the UI (Playwright-proven) — and still rejected at API/service/DB if attempted directly.
4. Role separation proven in the UI: `triage_user` session has no admin/review/discovery access (route guard + API enforcement both exercised).
5. Generated client matches `openapi.json` (CI diff check green).
6. Every user-facing value on queue/detail pages traces to API-served deterministic data — no client-side computation of numbers/dates beyond formatting (code-review checklist item + no-arithmetic lint rule on view components).
7. UI dev/build/test runs from a clean checkout via documented scripts only.

## Verification (gatekeeper commands)

```powershell
.\scripts\check.ps1
.\scripts\check_ui.ps1
npx playwright test e2e/ --reporter=list
python -m pytest tests/e2e -v
git diff --exit-code src/ui/src/api/       # generated client in sync
```

## Risks / dependencies / open questions

- **E2E flakiness** (multi-service orchestration): mitigated by scripted, health-checked startup and DB reset between scenarios; a flaky gate is a broken gate, so stability is an explicit acceptance concern for step 7.
- **Value-formatting drift** (UI formatting a date/number differently than stored): formatting utilities are unit-tested against the same typed rendering rules as the narration renderer (Phase 5) — one shared spec for how values display.
- **Accessibility/localization**: baseline a11y (keyboard nav, labels, contrast) included in component tests; full audit is Phase 11 checklist material. NZ date/number formats fixed project-wide (matches Phase 5 renderer).
- **Depends on:** Phase 9 API + openapi artifact; fixture pipeline.
- No new `DECISIONS.md` entries required by this spec.
