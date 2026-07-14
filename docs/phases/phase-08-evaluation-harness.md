# Phase 8 — Evaluation harness

## Objective

Measure the system against ground truth: synthetic anomaly injection into a mutable test copy paired with the checks expected to catch them (recall), precision from live reason-coded feedback (Phase 6 data), versioned gold finding sets, and a CI regression gate that fails on recall regression (F11).

## Preconditions

- Phases 2–7 closed (full pipeline: compile → execute → materialize → narrate → feedback).
- **D-009.2 ruled:** a restored copy/subset of `INDICI_BI_Full` that MAY be mutated, plus who provisions it and where. Harness development and the CI gate run on the fixture DB regardless; *realistic-volume recall numbers* wait on the copy.
- **D-011 confirmation due at this phase's gate** (its numbers parameterize the harness and the Phase 11 load test).

## Steps (one deliverable each; TDD throughout)

1. **Injection safety interlock (first, before any injector exists).** `cdss.eval.guard`: injection connections require BOTH (a) an explicit `CDSS_EVAL_TARGET` allowlist entry matching server+database, and (b) a sentinel marker table (`cdss_eval_target_marker`, created manually once by whoever provisions the copy) present in the target DB. Missing either ⇒ refusal. The production source can never carry the marker (we have no write access — the interlock is therefore structural).
   *Deliverable:* guard + tests: unlisted target refused; listed-but-unmarked target refused; marked fixture DB accepted.
2. **Mutation catalog.** `eval/mutations/*.yaml`: each entry = an injectable anomaly (set-based DML against the *test copy*), the check expected to catch it, entity selection rule (deterministic seed), and the reversal/cleanup statement. Seed coverage: delete an appointment's invoices ⇒ `appointment-completed-no-invoice`; orphan a `LabRad` order's result linkage ⇒ lab-result check; shift `Recalls.ReCallDate` past due ⇒ recall-overdue check; null a prerequisite field ⇒ expected **indeterminate** (F6 is evaluated too, not just fail). Target: ≥ 1 mutation per active workflow/care-gap check, ≥ 10 for profiling-derived classes.
   *Deliverable:* catalog schema + entries; validation that every referenced check exists and is active; every mutation has a cleanup.
3. **Injector + runner.** `cdss.eval.run`: snapshot baseline findings → apply a mutation batch (seeded, recorded) → full pipeline run → diff findings → score: caught (expected check fired on the injected entity), missed, mislabeled (different check fired), noise (findings on non-injected entities vs baseline) → clean up → verify restoration (row-level diff empty). Recall per check = caught / injected.
   *Deliverable:* runner + e2e on fixture DB: known mutations ⇒ expected catches; cleanup verified.
4. **Precision from feedback (F11's other half).** `cdss.eval.precision_report`: reads Phase 6 `precision_stats` (live or simulated feedback), joins with recall per check into one scorecard; checks with recall but no precision data (no feedback yet) are explicitly marked `unmeasured`, never assumed.
   *Deliverable:* combined scorecard artifact (`artifacts/eval/scorecard-<run>.json` + `.md`).
5. **Gold finding sets.** Versioned fixtures (`eval/gold/gold-v<N>.json`): the expected finding set for a pinned (fixture data version, check library version, mutation batch) triple. Regeneration is an explicit, human-approved command (`--bless`), never automatic — a gold change is reviewable in the diff.
   *Deliverable:* gold v1 blessed + comparison logic with typed diff output.
6. **CI regression gate.** CI job: fixture DB from scratch → inject → run → compare against gold → **fail on any missed injection that gold catches, or any new noise finding not in gold**; recall summary posted to the job log. Runtime budget enforced so the gate stays fast enough to keep.
   *Deliverable:* `scripts/eval_gate.ps1` wired into CI; a deliberately broken check demonstrably fails the gate (test of the test).
7. **Realistic-volume recall run (needs D-009.2 copy).** Full harness against the restored copy: recall at production-like volumes, per-check cost at those volumes (feeds F10 asks + D-011 load-target validation).
   *Deliverable:* `artifacts/eval/scorecard-live-v1` + cost findings recorded; any unaffordable check ⇒ `ASK-NNN`.

## Exit criteria

1. `scripts/check.ps1` exits 0; injection-interlock tests green (unlisted and unmarked targets refused).
2. **One command** (`scripts/eval_gate.ps1`) produces the harness report end-to-end on a clean checkout.
3. CI fails on recall regression — demonstrated by a deliberately disabled check turning the gate red, then green on restore.
4. Scorecard covers every active check: recall (or `not-injectable` with a written reason), precision (or `unmeasured`), cost.
5. Post-injection cleanup verified by row-level diff (test copy restored exactly).
6. Gold set v1 blessed and versioned; `--bless` requires an explicit flag + note.
7. **D-011 confirmed (or amended) in `DECISIONS.md`** — its numbers are now load-bearing for Phase 11.
8. Step 7 (realistic-volume run) complete **if** the D-009.2 copy exists; otherwise explicitly deferred in `DECISIONS.md` with the copy as a named blocker — the CI gate on fixture data stands either way.

## Verification (gatekeeper commands)

```powershell
.\scripts\check.ps1
.\scripts\eval_gate.ps1
python -m pytest tests/eval -v            # interlock, catalog validation, runner e2e
Get-Content artifacts/eval/scorecard-*.md
git log --oneline -- eval/gold/           # gold changes are visible, blessed commits only
```

## Risks / dependencies / open questions

- **D-009.2 is the realistic-recall blocker.** The phase is structured so everything except step 7 closes without it; step 7's deferral (if needed) is explicit, never silent.
- **Injection realism:** synthetic mutations approximate real failure modes; a check can score perfect recall on injections yet miss real-world variants. Mitigated by growing the mutation catalog from Phase 6 feedback and Phase 7 discovery patterns over time — the catalog is versioned and reviewable.
- **Gold-set brittleness:** intentional library growth changes expected findings; `--bless` keeps that a reviewed, diffable act rather than CI friction.
- **Noise scoring on the live copy:** the restored copy contains *real* anomalies (that's the product working); noise-vs-baseline diffing only counts *new* findings caused by injection, so pre-existing real findings don't pollute recall.
- **Depends on:** Phases 2–7; D-009.2; D-011 confirmation at gate.
- No new `DECISIONS.md` entries required beyond the D-011 confirmation and (if needed) the step-7 deferral record.
