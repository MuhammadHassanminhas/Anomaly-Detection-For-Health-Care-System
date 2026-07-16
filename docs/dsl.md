# docs/dsl.md — CDSS check DSL (normative)

Phase 2 step 1. This document and `src/cdss/schemas/check-dsl.schema.json` are kept
consistent by construction: every construct named here has a corresponding shape in
the schema, and the schema introduces nothing this document doesn't describe.

A check is one YAML document. The JSON Schema validates **structure only** — that
the document has the right shape. It does not know whether `dbo.Appointments` exists
or whether `AppointmentCompleted` is a real column on it: that is the step 2 parser's
job, checked against the semantic catalog (F2 — a check can never reference what the
catalog doesn't know). The schema also never parses the text inside a predicate or
`base_filters` expression; step 2/3 own that.

## Top-level shape

```yaml
id: kebab-case-stable-slug          # becomes checks.slug (Phase 3)
title: Human-readable title
category: revenue-integrity         # referential | data-quality | workflow | care-gap | revenue-integrity | policy
default_severity: medium            # low | medium | high | critical
entity: { ... }                     # see below
params: { ... }                     # typed params, may be {}
prerequisites: [ ... ]              # list of leaf expressions, may be []
predicate: ...                      # a predicate node (see below)
evidence: [ ... ]                   # non-empty list of column/field names
actions: [ ... ]                    # non-empty list of action-library codes
resolution: "..."                   # human-readable resolution condition
```

`entity`:

```yaml
entity:
  view: dbo.Appointments             # schema-qualified, verified against the catalog at parse time
  key: [AppointmentID]               # non-empty; the entity's identifying column(s)
  practice_column: PracticeID
  base_filters:                      # standard exclusions, applied in the driving query
    - "IsDeleted = 0"
    - "IsDummy = 0"
```

## Leaf expressions

Every predicate/prerequisite/`base_filters`/`on`/`where` value is an **opaque
string** — a SQL-ish boolean expression. The DSL doesn't formalize an expression
grammar at the schema level; the compiler (step 3) parses it for referenced
columns/params and emits the corresponding T-SQL. A leaf expression may reference a
param by `{param_name}`, substituted at compile time as a named placeholder bound by
the Phase 3 executor — never a literal baked into the golden SQL.

Four expression shapes are supported, all as plain strings:

- **Comparisons** — `"TotalAmount < 0"`, `"AppointmentCompleted = 1"`.
- **Null tests** — `"NHINumber IS NULL"`, `"ScheduleDate IS NOT NULL"`.
- **Date arithmetic** — `"InvoiceDate <= DATEADD(day, -{stale_days}, sysdatetime())"`.
- **`in` against a catalog-derived domain** — `"AppointmentStatus IN ({valid_status_codes})"`,
  where `valid_status_codes` is an `array`-typed param. The domain is *seeded* from
  the semantic catalog's captured `top_values` for that column at authoring time
  (human-reviewed) and then stored as a static, versioned param default — never
  re-read from the catalog at compile time. Re-deriving the "valid" set from the same
  population being checked would be circular (every value that occurred would
  trivially be "in domain"); F4 already requires predicates to stay fixed logic with
  params carrying the tunable/learned parts.

## Window lookbacks

A "window lookback" is not a separate schema construct — it is a date-arithmetic
comparison (usually inside an `exists`/`not_exists` clause's `where`) bounded by an
integer-typed param, e.g.:

```yaml
where: "dbo.Appointments.ScheduleDate >= DATEADD(day, -{recall_window_days}, sysdatetime())"
```

Named explicitly here because determinism (F10/§2.3) requires the window's length
to always be a bound param, never a literal date computed once and baked into the
compiled SQL — the same compiled statement must mean "as of now" on every run.

## Predicate nodes

A `predicate` (and each `prerequisites` entry) is one of:

| Shape | Meaning |
|---|---|
| a leaf expression string | evaluated directly |
| `all: [node, ...]` | true iff every child is true (three-valued AND, see below) |
| `any: [node, ...]` | true iff at least one child is true (three-valued OR) |
| `not: node` | negation |
| `exists: {view, on, where?}` | true iff a matching row exists in the joined view |
| `not_exists: {view, on, where?}` | true iff no matching row exists |

Nodes nest arbitrarily. An `exists`/`not_exists` clause's `on` is the join
condition; `where` is an optional additional filter scoped to the joined view only.
A node may declare exactly one of `all`/`any`/`not`/`exists`/`not_exists` — mixing
keys, or using a key not in this list, is a schema violation.

## Typed params

```yaml
params:
  invoice_lag_days:
    type: integer                    # integer | number | string | boolean | array
    default:
      strategy: percentile           # learned per-practice at calibration time (Phase 6)
      measure: appointment_to_invoice_lag
      p: 95
      fallback: 7                    # used until the practice has enough data
  stale_days:
    type: integer
    default: { strategy: fixed, value: 60 }   # static, never learned
```

F4: predicates are fixed logic; only param *values* vary per practice, and only
through one of these two default strategies. A param without a `default` is
rejected — every check must be evaluable the moment it's approved, before any
calibration run has ever executed.

## Evidence, actions, resolution

- `evidence`: the exact field allowlist the narration validator (F8, Phase 5) will
  enforce — the LLM can reference these fields' *names and types* when drafting a
  narrative template, but the renderer alone fills in values. Non-empty; may include
  PHI-bearing fields (e.g. `FirstName`) since evidence is for human triage, not the
  LLM boundary — Tier S means the LLM never sees the *value*, not that the value is
  never stored.
- `actions`: non-empty list of codes that must exist in the curated action library
  (a stub registry until Phase 4 builds the real one).
- `resolution`: free text describing the human-readable condition under which the
  finding would no longer apply.

## Three-valued evaluation (normative, F6)

Per entity row, in order:

1. `base_filters` are applied in the driving query — rows that don't match never
   enter evaluation at all (not even as indeterminate).
2. If any `prerequisites` entry evaluates `NULL` or `FALSE` → **indeterminate**,
   and the predicate is never evaluated for that row.
3. Otherwise, the `predicate` evaluates: `TRUE` → **fail**, `FALSE` → **pass**,
   `NULL` → **indeterminate**. SQL `NULL` propagation through comparisons and
   `AND`/`OR`/`NOT` is caught explicitly by the compiler's CASE-based tri-state
   logic (step 3) — never coerced to `FALSE`/`TRUE`.

`all`/`any` follow standard three-valued logic: `all` is indeterminate if no child
is `FALSE` but at least one is indeterminate; `any` is indeterminate if no child is
`TRUE` but at least one is indeterminate.

## Determinism

`(DSL doc, params, catalog version, dialect)` → byte-identical compiled SQL,
hashed as `sql_hash`. Golden-SQL snapshot tests (step 4) pin this in CI; a
double-compile test proves the same inputs never drift.

## Example checks

Six checked-in examples under `examples/checks/` collectively exercise every
construct above:

| File | Constructs exercised |
|---|---|
| `appointment-completed-no-invoice.yaml` | `all`, comparison, date arithmetic, `not_exists`, null-test prerequisites, percentile param — the ARCHITECTURE.md §2.3 sketch, checked in verbatim |
| `patient-active-missing-nhi.yaml` | `all`, comparisons, null test as a predicate condition (not just a prerequisite) |
| `invoice-stale-unpaid-balance.yaml` | `all`, comparison, date arithmetic, fixed-strategy param |
| `appointment-invalid-status-code.yaml` | `not`, `in`-against-catalog-domain, array param |
| `patient-no-recent-appointment.yaml` | `any`, `not_exists`, window lookback |
| `invoice-negative-total-amount.yaml` | bare leaf-expression predicate (no `all`/`any` wrapper), empty `params` |

## Deliberately out of scope for step 1

- Whether a referenced view/column/join actually exists (step 2, against the
  semantic catalog).
- Parsing a leaf expression's internal grammar to find referenced columns/params
  (step 2/3).
- Compiling to T-SQL (step 3) and golden-SQL snapshots (step 4).
- The action library's real contents (Phase 4 stub only for now).
