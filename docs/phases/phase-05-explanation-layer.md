# Phase 5 — Explanation & recommendation layer

## Objective

Turn findings into staff-facing narratives and recommended actions under F8's constraint: the LLM writes placeholder templates and selects actions from the check's allowlist; code interpolates real values; a validator blocks any number/date/code not traceable to the evidence allowlist. PHI boundary per D-003 Tier S: zero actual values reach the LLM at runtime.

## Preconditions

- Phase 4 closed: active checks with action allowlists exist; findings with typed evidence materialize on fixture runs.
- **D-004 ruled** (narration LLM). **D-003 sign-off is required to CLOSE this phase** (brief constraint 5) — build proceeds on the proposed Tier S/M policy; the gate waits on you.

## Steps (one deliverable each; TDD — the validator is built before the narrator)

1. **Fallback templates (deterministic floor).** Add `fallback_template` as a **mandatory** field on `check_versions` (migration + backfill for Phase 4 checks, re-reviewed at this phase's review touchpoint): human-authored placeholder text rendered purely by code. Narration can now never block findings — the floor exists before any LLM code.
   *Deliverable:* migration + renderer + tests; every active check renders its fallback against fixture evidence.
2. **Placeholder renderer.** `cdss.narrate.render`: interpolates `{{field}}` placeholders from evidence ∪ params with typed, locale-fixed formatting (dates ISO-8601, decimals fixed notation); unknown placeholder ⇒ hard error; every interpolation recorded in a provenance map (placeholder → source field → rendered span offsets).
   *Deliverable:* renderer + property-style tests (round-trip: every rendered span traces back to a source).
3. **Validator (built and tested before the narrator exists).** `cdss.narrate.validate` takes (template, rendered text, evidence, params, check action-allowlist, static vocabulary) and enforces: (a) every placeholder ∈ evidence ∪ params; (b) every numeric / date-like / code-like token in the **rendered** text lies inside an interpolated span, the action library's fixed copy, or the approved static vocabulary (checked-in, human-reviewed list of phrase constants); (c) selected actions ⊆ the check's allowlist; (d) no evidence field outside the check's declared set appears. Any violation ⇒ `blocked`, with a machine-readable violation report.
   *Deliverable:* adversarial test suite — smuggled digits ("within 14 days" not in params), date paraphrases ("March 3rd" for 2026-03-03), invented codes, actions off-allowlist, evidence exfiltration — every one blocked. This suite is the phase's core evidence.
4. **Tier S narration pipeline.** `cdss.narrate.compose`: prompt = check rationale, category, resolution guidance, evidence **field names + types only**, param names, allowed action codes — through the redaction layer (`CDSS_REDACTION_MODE`, no "off" in production builds); LLM returns template + action selection; validate → render → validate rendered → store `narratives` row (template, rendered, model_id, prompt_hash, validation_status, actions). Any failure ⇒ deterministic fallback renders, `validation_status='blocked_fell_back'`. LLM outage ⇒ `fallback_static`. Findings are never delayed or lost to narration.
   *Deliverable:* pipeline + failure-injection tests (LLM returns garbage / smuggles values / times out ⇒ fallback path, finding unaffected).
5. **Template cache.** Cache key `(check_version_id, evidence-shape hash)`: LLM calls are O(active checks), not O(findings) (F10). Cache invalidates on check version change.
   *Deliverable:* cache + test: N findings of one check ⇒ 1 LLM call; version bump ⇒ exactly 1 more.
6. **Redaction-boundary proof.** Record every outbound LLM payload (dev/test mode); assert none contains: any evidence *value*, any entity key, any string matching live identifier patterns (NHI format, names from fixture data). Combined with step 3 this is the constraint-5 evidence package for your D-003 sign-off.
   *Deliverable:* boundary test suite green; a short `docs/phi-boundary.md` summarizing what leaves the process in each tier, for sign-off.
7. **End-to-end narration run.** `scripts/run.ps1` extended: new findings from a fixture run get narratives inline (§4.5 of ARCHITECTURE.md); run report gains narration stats (composed, cached, blocked, fallback).
   *Deliverable:* fixture run where every new finding ends with a rendered, validated narrative or an explicit fallback.

## Exit criteria

1. `scripts/check.ps1` exits 0, including the full adversarial validator suite (step 3) and failure-injection suite (step 4).
2. **Structural proof:** test demonstrating no un-allowlisted number/date/code can reach output — the adversarial suite covers digit smuggling, date paraphrase, code invention, action escape, evidence exfiltration; all blocked.
3. Redaction verified: recorded LLM payloads contain zero evidence values/identifiers (step 6 suite green).
4. Every active check has a fallback template; kill-the-LLM test (env var forces outage) still yields narrated findings via `fallback_static`.
5. Cache proof: fixture run with N>10 findings on one check makes exactly 1 LLM call (asserted on recorded calls).
6. **D-003 signed off in `DECISIONS.md` (status → DECIDED)** with `docs/phi-boundary.md` as its evidence. *Hard gate — the phase cannot close without it.*

## Verification (gatekeeper commands)

```powershell
.\scripts\check.ps1
python -m pytest tests/narration -v          # adversarial + failure-injection + boundary suites
$env:CDSS_LLM_FORCE_OUTAGE='1'; .\scripts\run.ps1   # findings still narrated via fallback
Get-Content docs/phi-boundary.md
```

## Risks / dependencies / open questions

- **D-003 is the gate** (brief constraint 5). If you rule stricter than Tier S, note Tier S already sends zero values — a stricter ruling likely constrains Tier M (Phases 4/7 prompts), which would be a documented amendment there, not here.
- **Validator false positives** (legit static phrasing blocked): acceptable failure direction — it falls back to the deterministic template; blocked-rate appears in run reports so over-blocking is visible and the static vocabulary can be extended through review, never widened silently.
- **Token-classification gaps** (what counts as "code-like"): classifier errs broad (blocks more), with the static vocabulary as the pressure valve; classifier rules are versioned and tested.
- **Evidence-shape drift** breaking cached templates: cache key includes evidence-shape hash, so drift ⇒ recompose, not stale render.
- **Depends on:** Phases 3–4; D-003 (gate), D-004 (LLM).
- No new `DECISIONS.md` entries required by this spec.
