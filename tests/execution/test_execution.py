"""Phase 2 step 5: compiled SQL for the checked-in example checks runs live
against the disposable fixture SQL Server (scripts/fixture_db.ps1); result
tri-states are asserted row-by-row against hand-computed expectations,
covering pass/fail/indeterminate, missing-prerequisite rows, and base-filter
exclusion for every construct the fixture data below was built to exercise.

**Scoping decision, flagged, not silently worked around.** `compile_check()`
emits named T-SQL parameters (`@stale_days`, ...) meant to be bound by the
Phase 3 executor (per `cdss.compiler`'s own docstring) -- that executor
doesn't exist yet. This suite binds them itself with a `DECLARE ... = <literal
value>;` preamble in front of the compiled SELECT. That's test-harness
plumbing only, not a preview of the real (Phase 3) binding mechanism.

**Array params.** `array`-typed params (`valid_status_codes` on
`appointment-invalid-status-code`) compile to one named T-SQL parameter per
element (`cdss.compiler`'s `_expand_array_params`) -- this suite reads the
element values straight from the check's own `fixed` default (the DSL's own
design: an `in`-against-catalog-domain array is static, human-reviewed at
authoring time, never overridden at run time) rather than requiring the
caller to repeat them.
"""

from __future__ import annotations

from pathlib import Path

import pyodbc

from cdss.compiler import CompiledCheck, compile_check
from cdss.dsl import CheckDoc, parse_check_document

EXAMPLES_DIR = Path(__file__).parent.parent.parent / "examples" / "checks"

_SQL_DECLARE_TYPE = {
    "integer": "INT",
    "number": "FLOAT",
    "boolean": "BIT",
    "string": "NVARCHAR(100)",
}


def _sql_literal(value: object, dsl_type: str) -> str:
    if dsl_type == "string":
        escaped = str(value).replace("'", "''")
        return f"N'{escaped}'"
    if dsl_type == "boolean":
        return "1" if value else "0"
    return str(value)


def _compile_example(name: str) -> tuple[CheckDoc, CompiledCheck]:
    doc = parse_check_document((EXAMPLES_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
    return doc, compile_check(doc)


def _run(
    conn: pyodbc.Connection,
    doc: CheckDoc,
    compiled: CompiledCheck,
    param_values: dict[str, object],
) -> dict[object, str]:
    """Execute `compiled` with `param_values` (scalar params, by name) and
    every array param's own declared default (by element) bound as literal
    DECLAREs; return `{entity_key: tri_state}` (every example here has a
    single-column key, so `row[0]` is unambiguous)."""
    declares: list[str] = []
    for name, param in doc.params.items():
        if param.type == "array":
            for i, value in enumerate(param.default.value):
                element_name = f"{name}_{i}"
                dsl_type = compiled.params_schema[element_name]
                declares.append(
                    f"DECLARE @{element_name} {_SQL_DECLARE_TYPE[dsl_type]} "
                    f"= {_sql_literal(value, dsl_type)};"
                )
        else:
            dsl_type = compiled.params_schema[name]
            declares.append(
                f"DECLARE @{name} {_SQL_DECLARE_TYPE[dsl_type]} "
                f"= {_sql_literal(param_values[name], dsl_type)};"
            )
    cursor = conn.cursor()
    cursor.execute("\n".join(declares) + "\n" + compiled.sql_text)
    return {row[0]: row[2] for row in cursor.fetchall()}


# --- appointment-completed-no-invoice (all, comparisons, date arithmetic, ---
# --- not_exists+join, null-test prerequisites) ------------------------------


def test_appointment_completed_no_invoice(fixture_conn: pyodbc.Connection) -> None:
    doc, compiled = _compile_example("appointment-completed-no-invoice")
    results = _run(fixture_conn, doc, compiled, {"invoice_lag_days": 7})
    assert results[1] == "fail"  # completed, stale, no invoice at all
    assert results[2] == "pass"  # completed, stale, but has an active invoice
    assert results[3] == "pass"  # not completed
    assert results[4] == "pass"  # completed, but not stale yet
    assert results[5] == "indeterminate"  # AppointmentCompleted IS NULL
    assert results[6] == "indeterminate"  # ScheduleDate IS NULL
    assert 7 not in results  # base_filters: IsDeleted = 1
    assert 8 not in results  # base_filters: IsDummy = 1
    assert 9 not in results
    assert 10 not in results
    assert 11 not in results


# --- appointment-invalid-status-code (not, in-against-catalog-domain array) --


def test_appointment_invalid_status_code(fixture_conn: pyodbc.Connection) -> None:
    doc, compiled = _compile_example("appointment-invalid-status-code")
    results = _run(fixture_conn, doc, compiled, {})
    assert results[1] == "pass"  # AppointmentStatus = 'Completed' (in the reviewed domain)
    assert results[2] == "pass"
    assert results[3] == "pass"
    assert results[4] == "pass"
    assert results[5] == "pass"
    assert results[6] == "pass"
    assert 7 not in results  # base_filters: IsDeleted = 1
    assert results[8] == "indeterminate"  # AppointmentStatus IS NULL
    assert results[9] == "pass"  # AppointmentStatus = 'Booked' (in the reviewed domain)
    assert results[10] == "fail"  # AppointmentStatus = 'Bogus' (not in the reviewed domain)
    assert results[11] == "pass"


# --- invoice-negative-total-amount (bare comparison predicate, no params) ---


def test_invoice_negative_total_amount(fixture_conn: pyodbc.Connection) -> None:
    doc, compiled = _compile_example("invoice-negative-total-amount")
    results = _run(fixture_conn, doc, compiled, {})
    assert results[1] == "fail"  # TotalAmount = -50
    assert results[2] == "pass"  # TotalAmount = 100
    assert results[3] == "indeterminate"  # TotalAmount IS NULL
    assert results[4] == "pass"
    assert results[6] == "pass"
    assert results[7] == "pass"
    assert 5 not in results  # base_filters: IsDeleted = 1


# --- invoice-stale-unpaid-balance (all, comparisons, date arithmetic, ------
# --- fixed-strategy param default) ------------------------------------------


def test_invoice_stale_unpaid_balance(fixture_conn: pyodbc.Connection) -> None:
    doc, compiled = _compile_example("invoice-stale-unpaid-balance")
    results = _run(fixture_conn, doc, compiled, {"stale_days": 60})
    assert results[1] == "fail"  # unpaid, invoiced 90 days ago
    assert results[2] == "pass"  # UnpaidAmount = 0
    assert results[3] == "fail"  # unpaid, invoiced 90 days ago (TotalAmount irrelevant here)
    assert results[4] == "indeterminate"  # InvoiceDate IS NULL
    assert results[7] == "pass"  # unpaid, but only invoiced 5 days ago
    assert 5 not in results  # base_filters: IsDeleted = 1
    assert 6 not in results  # base_filters: IsActive = 0


# --- patient-active-missing-nhi (all, comparisons, null test in predicate --
# --- position, not just a prerequisite) --------------------------------------


def test_patient_active_missing_nhi(fixture_conn: pyodbc.Connection) -> None:
    doc, compiled = _compile_example("patient-active-missing-nhi")
    results = _run(fixture_conn, doc, compiled, {})
    assert results[1] == "fail"  # active, not a test record, no NHI
    assert results[2] == "pass"  # has an NHI number
    assert results[3] == "pass"  # IsTestRecord = 1
    assert results[4] == "indeterminate"  # IsActive IS NULL
    assert results[5] == "fail"
    assert 6 not in results  # base_filters: IsDeleted = 1


# --- patient-no-recent-appointment (any, not_exists+join, param-driven -----
# --- window lookback) ---------------------------------------------------------


def test_patient_no_recent_appointment(fixture_conn: pyodbc.Connection) -> None:
    doc, compiled = _compile_example("patient-no-recent-appointment")
    results = _run(fixture_conn, doc, compiled, {"recall_window_days": 365})
    assert results[1] == "fail"  # high-care, no appointment at all
    assert results[2] == "pass"  # high-care, has a recent appointment
    assert results[3] == "pass"  # neither IsHighCare nor IsCarePlus
    assert results[5] == "indeterminate"  # EnrollmentDate IS NULL
    assert 4 not in results  # base_filters: IsActive IS NULL
    assert 6 not in results  # base_filters: IsDeleted = 1
