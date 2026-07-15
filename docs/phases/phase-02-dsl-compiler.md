# Phase 2 — Check DSL + compiler

## Objective

Define the YAML check DSL (JSON-Schema-validated) and the deterministic compiler that turns `(DSL doc, params, catalog version, dialect)` into byte-identical, set-based T-SQL over the views, with normative three-valued evaluation semantics (F6).

## Preconditions

- Phase 1 closed: `semantic-catalog-v1.json` exists and validates (compiler development uses a **synthetic fixture catalog** for unit tests plus the real catalog for integration checks).
- D-009.1 ruled: fixture SQL Server available (Docker `mssql/server:2022` or Express/LocalDB fallback) — required for exit, not start.

## Steps (one deliverable each; TDD — every construct gets its failing test first)

1. **DSL specification.** `docs/dsl.md` (normative) + `src/cdss/schemas/check-dsl.schema.json`. Constructs (per ARCHITECTURE.md §2.3): `all/any/not`, comparisons, null tests, date arithmetic, `exists/not_exists` with declared join, `in` against catalog domains, window lookbacks; `entity` (view, key, practice_column, base_filters), typed `params` with default strategies (`fixed` | `percentile`), `prerequisites`, `predicate`, `evidence`, `actions`, `resolution`. Tri-state semantics stated normatively: base_filters scope the driving query; any prerequisite NULL/FALSE ⇒ indeterminate; predicate TRUE ⇒ fail, FALSE ⇒ pass, NULL ⇒ indeterminate — SQL NULL propagation caught, never coerced.
   *Deliverable:* spec doc + schema; ≥6 example checks (incl. the §2.3 sketch) validate; malformed examples rejected with actionable errors.
2. **Parser + semantic validation.** `cdss.dsl` parses YAML → typed model; validates against the JSON Schema, then against the **semantic catalog**: every view/column/join/domain referenced must exist (F2); evidence columns must exist on the driving view or declared joins; actions must be declared (action library integration lands in Phase 4 — for now a stub registry). Unknown reference ⇒ refusal with the exact missing name.
   *Deliverable:* parser + validator with unit tests for every refusal path.
3. **Compiler core.** `cdss.compiler` emits one set-based, parameterized T-SQL statement per check computing `(entity key columns, practice_id, tri_state, evidence columns)` for the whole increment — CASE-based tri-state exactly per the normative semantics; deterministic output (stable column order, canonical whitespace, sorted predicates where order is semantically free); `sql_hash` computed. Watermark/increment clauses are emitted as named placeholder parameters (bound by the Phase 3 executor).
   *Deliverable:* compiler with unit tests per construct, incl. NULL-propagation edge cases (e.g., comparison with NULL operand lands in indeterminate, not pass).
4. **Golden-SQL snapshot suite.** Every example check compiles to a checked-in `.sql` golden file; CI fails on any diff. Same inputs ⇒ byte-identical output proven by a double-compile test.
   *Deliverable:* `tests/golden/*.sql` + snapshot test harness.
5. **Fixture database + execution semantics tests.** Scripted creation of a fixture SQL Server DB (clearly synthetic data): tiny tables shaped like a subset of catalog views, **exposed to the code under test as views**, covering pass/fail/indeterminate for every construct — including missing-prerequisite rows and NULL-in-predicate rows. Compiled SQL for each example check runs live; result tri-states asserted row-by-row against hand-computed expectations.
   *Deliverable:* `scripts/fixture_db.ps1` (create/teardown, fully scripted) + execution test suite green against real SQL Server.
6. **Compiler guard integration.** Compiled SQL passes through the Phase 0 SQL guard before any execution (single SELECT, view allowlist) — proving the guard accepts everything the compiler emits and still refuses hand-crafted violations.
   *Deliverable:* integration tests guard×compiler.

## Exit criteria

1. `scripts/check.ps1` exits 0 — including: unit tests for **every DSL construct's** pass, fail, and indeterminate paths; every refusal path; double-compile determinism test.
2. Golden-SQL snapshots exist for all example checks; snapshot test green.
3. `scripts/fixture_db.ps1` provisions the fixture DB from nothing; execution test suite runs compiled SQL against real SQL Server and all tri-state assertions pass.
4. A check referencing a view/column absent from the catalog fails compilation with an error naming the missing reference (test-proven).
5. `docs/dsl.md` and `check-dsl.schema.json` are consistent (each construct in one appears in the other — verified by a doc-coverage test, not by eye).

## Verification (gatekeeper commands)

```powershell
.\scripts\check.ps1
.\scripts\fixture_db.ps1 -Recreate
python -m pytest tests/execution -v
python -m pytest tests/golden -v
```

## Risks / dependencies / open questions

- **D-009.1 blocks exit:** no fixture SQL Server ⇒ criteria 3 unmeetable. Golden-snapshot work (criteria 1–2) proceeds regardless.
- **Dialect drift:** fixture server (2022) may differ from the live server's version (captured in Phase 0). Compiler targets the **live** version's feature set; any construct needing a newer feature than live supports is rejected at compile time. Recorded per-dialect in golden files if versions differ.
- **Expressiveness ceiling:** some future checks may not fit the DSL (e.g., multi-hop temporal sequences). Deliberate: the DSL grows by versioned extension in later phases (new constructs ⇒ new golden tests), never by escape-hatch raw SQL, which would bypass catalog validation and the guard. If Phase 4 hits a wall, that's a `DECISIONS.md` entry, not an improvisation.
- **Depends on:** Phase 1 catalog schema (fixture catalog for unit tests mirrors it); Phase 0 SQL guard.
- No new `DECISIONS.md` entries required by this spec.
