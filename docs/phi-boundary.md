# PHI boundary — what leaves the process, per tier

Phase 5 step 6 deliverable: the constraint-5 evidence package for D-003
sign-off (`DECISIONS.md`, still **OPEN**). This document is evidence for
that decision, not the decision itself — D-003 is signed off by the
product owner updating `DECISIONS.md`, not by this file existing.

Two tiers carry data to an LLM anywhere in CDSS (D-003's proposed policy,
`ARCHITECTURE.md` §2.6). Both sit behind `CDSS_REDACTION_MODE`
(`cdss.narrate.resolve_redaction_mode`); there is no `off` mode in a
production build — `CDSS_ENV` defaults to `production` (fail-closed), and
`off` is refused unless a non-production environment is explicitly
declared.

## Tier S — runtime narration (default, `cdss.narrate.compose`)

**Leaves the process:** the check's `rationale`, `category`, `resolution`
text; every evidence/param **field name and type** (e.g. `"AppointmentID"`
/ `"integer"`); the check's allowed action codes. All static, checked-in,
or structural metadata — never row data.

**Never leaves the process:** any evidence value, any param value, any
entity key, any free-text field content. `build_narration_context`'s Tier
S branch does not have a code path that can emit a value — there is no
flag or condition to disable; the branch that would include one
(`mode="off"`) is a separate function path that `resolve_redaction_mode`
refuses in production.

**How this is proven, not just asserted:**
- `tests/narration/test_compose.py::test_tier_s_context_carries_names_and_types_only_never_a_value` —
  every field built under Tier S is asserted to have no `"value"` key.
- `tests/narration/test_redaction_boundary.py` (step 6) — an adversarial,
  realistic-looking finding (an NHI-format string, a synthetic patient
  name, a numeric entity key, a currency amount, a date) is run through
  the real `compose` pipeline; the actual recorded outbound prompt
  (`cdss.narrate.RecordedPrompt`, captured via `cdss.narrate.PromptRecorder`)
  is scanned and asserted to contain **none** of the raw values, and to
  match no NHI-format pattern (`\b[A-Z]{3}\d{4}\b`).
- `tests/narration/test_validate.py` (step 3) — the adversarial validator
  suite: even if a template *did* smuggle a value-shaped token into its
  static prose (independent of the redaction boundary above — a
  belt-and-suspenders check, not a duplicate of it), `cdss.narrate.validate`
  blocks it before it reaches a stored narrative. Together, steps 3 and 6
  are the two independent layers constraint 5 asks for: nothing sent
  carries a value (6), and nothing rendered can smuggle one back in (3).

**Recording:** `cdss.narrate.JsonlPromptRecorder` writes one JSONL line
per real LLM call to `artifacts/prompt_audit/` when a caller opts in — off
by default, never required to compose a narrative (`recorder=None` is the
default and is fully supported).

## Tier M — offline authoring & discovery characterization (`cdss.authoring.llm_draft`)

**Leaves the process:** view/column names, types, classes; aggregate
statistics (null rate, distinct count, row count); for a **non-identifier**
column only — its captured `min_value`/`max_value`/`top_values`/
`reference_samples` (bounded-cardinality domain/vocabulary samples, per
D-020's tiered profiling-stage capture policy). Relationship containment
ratios and orphan counts (aggregate, not row-level).

**Never leaves the process:** anything from an `identifier_or_freetext`-classified
column (`cdss.authoring.llm_draft._redacted_column` re-checks
`column_class` itself rather than trusting the profiler's own invariant —
defense in depth against a future profiling-stage regression). No
individual patient/appointment/invoice row is ever assembled or sent —
Tier M's unit is a column's aggregate profile, not a record.

**How this is proven:** `tests/test_llm_draft.py`'s redaction-boundary
tests — an adversarially-populated identifier column's `min_value`/
`top_values`/`reference_samples` are asserted to never reach
`build_catalog_context`'s output or the assembled prompt, even when a
fixture deliberately populates them (i.e., the test does not merely trust
that the real catalog happens to leave them null).

## What this document does not cover

- The discovery lane's own Tier M characterization prompts (Phase 7, not
  yet built) — expected to reuse `build_catalog_context`'s same redaction
  boundary; a fresh boundary-proof suite is that phase's own job when it
  lands, not inherited automatically from this one.
- Whether practice/provider names count as PHI-equivalent for this
  deployment — an open question D-003 itself names, for the product owner
  to answer, not assumed here either way.
