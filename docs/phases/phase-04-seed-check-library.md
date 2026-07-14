# Phase 4 — Seed check library

## Objective

Populate the check library through all four F3 sources' front half — auto-generate profiling-derived checks from the semantic catalog, LLM-draft workflow/care-gap checks, build the human review gate — and learn per-practice parameter defaults from each practice's own distributions (F4).

**Active-check target (set here, per brief):** ≥ 30 approved active checks — ≥ 18 profiling-derived (referential/data-quality), ≥ 10 LLM-drafted workflow/care-gap, ≥ 2 hand-authored policy examples. Discovery promotions (F3c) arrive in Phase 7.

## Preconditions

- Phase 3 closed: library tables, executor, fixture DB pipeline all live.
- **D-004 ruled** for step 4 only (LLM drafting); steps 1–3, 5–7 are LLM-free and do not wait.
- **D-003 Tier M** confirmed sufficient for what leaves for the LLM: catalog metadata + aggregates, no row-level data, no identifiers.

## Steps (one deliverable each; TDD throughout)

1. **Action library seed.** Curated, human-written `action_library` rows (e.g., `book-recall`, `verify-invoice`, `chase-result`, `flag-for-clinician-review`, `correct-record`, `raise-billing-task`) — operational actions only, no clinical judgments; loaded by migration/seed script; every future check references only these.
   *Deliverable:* seeded table + test that an unknown action code fails check validation.
2. **Profiling-derived generator (F3a).** `cdss.authoring.derive` reads the semantic catalog and emits draft DSL checks from evidence: relationship edges with orphans ⇒ referential-integrity checks; sentinel prevalence ⇒ placeholder-value checks; domain violations ⇒ enum checks; impossible ranges (from typed min/max, e.g., DOB in the future) ⇒ range checks; candidate-key duplicates ⇒ duplicate checks. Each draft carries machine-generated rationale citing the exact catalog evidence (containment ratio, orphan count, prevalence). Deterministic: same catalog ⇒ same drafts.
   *Deliverable:* generator + unit tests on the fixture catalog; drafts land as `checks(source=profiling, status=draft)`.
3. **Review gate CLI (F3 — the one gate).** `python -m cdss.review`: list drafts, show definition + rationale + compiled SQL + **fixture dry-run result**, approve (⇒ `active`, records reviewer + note) / reject (⇒ `rejected`, reason mandatory) / amend (new `check_version`). No path from `draft` to `active` outside this command; approval writes `reviewed_by/at/note` on the version (immutability preserved — amendments create versions).
   *Deliverable:* review CLI with tests proving no bypass path (direct SQL is the only alternative, and CI greps the codebase for any other status-transition write).
4. **LLM drafting harness (F3b).** `cdss.authoring.llm_draft`: prompt = Tier M catalog context (names, types, domains, relationship edges, aggregate stats — redaction-layer enforced) + DSL spec + category briefs (workflow-integrity, care-gap). Output must parse and catalog-validate (step 2 of Phase 2) or it is auto-rejected with the error fed back once for repair; surviving drafts land as `checks(source=llm, status=draft)` with the LLM's rationale attached. Target drafts: appointment-completed-no-invoice, lab-ordered-no-result, chronic-condition-recall-overdue, immunisation-schedule-gap, inbox-unactioned-aging, claim-no-payment, and peers.
   *Deliverable:* harness + redaction-boundary test (prompt content provably contains no row-level values); ≥ 12 valid drafts in the gate.
5. **Fixture test per check.** Every draft intended for approval gets a fixture scenario: synthetic rows that must fail, pass, and be indeterminate; wired into CI. A check without a passing fixture test cannot be approved (review CLI enforces).
   *Deliverable:* per-check fixture suites; CI job running all of them.
6. **Parameter learning (F4).** `cdss.calibration.learn_defaults`: for every `strategy: percentile` param, compute the practice's empirical distribution via set-based SQL over the views (e.g., invoice-lag P95, lab-turnaround P90), write `practice_check_config(params_source=learned)` + `calibration_runs` snapshot (before/after, distribution percentiles). Practices with insufficient data (below a minimum-n recorded in config) fall back to the DSL's declared fallback, explicitly marked.
   *Deliverable:* learning job + tests on fixture distributions with hand-computed percentiles; live run populates real per-practice params.
7. **Human review session (the gate, exercised).** You (or your delegate) review every draft through the CLI to reach the target counts. This is the explicit human approval step — allowed manual action per the brief.
   *Deliverable:* ≥ 30 active checks, each with rationale, category, severity, params, reviewer identity, and a green fixture test.

## Exit criteria

1. `scripts/check.ps1` exits 0, including every approved check's fixture suite.
2. `SELECT count(*) FROM checks WHERE status='active'` ≥ 30 with the source split above; **zero** active checks lacking rationale, category, severity, params schema, or reviewer identity (SQL assertion in CI).
3. Review-gate bypass test green: no code path writes `status='active'` outside `cdss.review`.
4. Redaction-boundary test green: captured LLM prompts contain no row-level values or identifiers (asserted against recorded prompt payloads).
5. `calibration_runs` holds a learned-default snapshot per (practice, percentile-param) or an explicit insufficient-data fallback record.
6. Executor run on fixture DB with the seeded library completes; cost report shows every active check.

## Verification (gatekeeper commands)

```powershell
.\scripts\check.ps1
python -m cdss.review list --status active --counts
python -m pytest tests/checks -v
python -m pytest tests/redaction -v
.\scripts\run.ps1
```

## Risks / dependencies / open questions

- **D-004 blocks step 4 only.** If it stays open, the phase can still close by raising the profiling-derived + hand-authored targets — that would be a documented amendment to this spec's target split, gated by you.
- **LLM drafts may be plausible-but-wrong** (D-017 risk): defenses are catalog validation, mandatory fixture tests, live dry-run shown at review, and your human gate. Precision tracking (Phase 6) is the backstop.
- **Learned defaults from skewed live data** (e.g., a practice with backlogged invoicing learns a lax P95): visible in the `calibration_runs` snapshot at review time; reviewer can set `params_source=manual`. F5 recalibration revisits continuously.
- **Review workload:** ~40+ drafts to review is real work for you — the CLI shows compiled SQL + dry-run counts to make each decision fast.
- **Depends on:** Phases 1–3 artifacts; D-003 (Tier M), D-004.
- No new `DECISIONS.md` entries required; the ≥30 target is set by this spec and amendable at its gate.
