"""Phase 2 step 3: cdss.compiler emits one deterministic, parameterized
T-SQL statement per check. Three-valued evaluation (F6) is implemented by
letting SQL Server's own AND/OR/NOT/CASE semantics do the work -- these
tests inspect the *shape* of the compiled text to prove that construction is
correct; they never execute SQL (step 5's fixture-DB deliverable does that).

All fixtures are synthetic (fabricated view/column names), matching this
project's existing schema/DSL-test convention.
"""

from __future__ import annotations

import re

import pytest

from cdss.compiler import compile_check
from cdss.dsl import (
    AllNode,
    AnyNode,
    CheckDoc,
    EntityDef,
    ExistsClause,
    ExistsNode,
    NotExistsNode,
    NotNode,
    ParamDef,
    ParamDefault,
    parse_check_document,
)

EXAMPLES_DIR = __import__("pathlib").Path(__file__).parent.parent / "examples" / "checks"


def _doc(
    *,
    key: tuple[str, ...] = ("AppointmentID",),
    practice_column: str = "PracticeID",
    base_filters: tuple[str, ...] = ("IsDeleted = 0", "IsDummy = 0"),
    params: dict[str, ParamDef] | None = None,
    prerequisites: tuple[str, ...] = ("AppointmentCompleted IS NOT NULL",),
    predicate: object = "AppointmentCompleted = 1",
    evidence: tuple[str, ...] = ("AppointmentID", "PatientID", "PracticeID"),
) -> CheckDoc:
    return CheckDoc(
        id="synthetic-check",
        title="Synthetic check",
        category="data-quality",
        default_severity="medium",
        entity=EntityDef(
            view="dbo.SyntheticAppointment",
            key=key,
            practice_column=practice_column,
            base_filters=base_filters,
        ),
        params=params or {},
        prerequisites=prerequisites,
        predicate=predicate,  # type: ignore[arg-type]
        evidence=evidence,
        actions=("verify-invoice",),
        resolution="Resolved.",
    )


# --- basic shape --------------------------------------------------------------


def test_compile_check_projects_key_practice_tristate_evidence_in_order() -> None:
    doc = _doc(evidence=("AppointmentID", "PatientID", "ScheduleDate", "PracticeID"))
    compiled = compile_check(doc)
    select_clause = re.search(r"SELECT\s+(.*?)\s+FROM", compiled.sql_text, re.DOTALL)
    assert select_clause is not None
    projected = select_clause.group(1)
    # entity key, practice_column, tri_state, then evidence-only additions (deduped)
    assert projected.index("[AppointmentID]") < projected.index("[PracticeID]")
    assert projected.index("[PracticeID]") < projected.index("tri_state")
    assert projected.index("AS tri_state") < projected.index("[PatientID]")
    assert projected.index("[PatientID]") < projected.index("[ScheduleDate]")
    # AppointmentID/PracticeID (already in key/practice_column) are not repeated
    assert projected.count("[AppointmentID]") == 1
    assert projected.count("[PracticeID]") == 1


def test_compile_check_from_clause_references_entity_view() -> None:
    doc = _doc()
    compiled = compile_check(doc)
    assert "FROM dbo.SyntheticAppointment" in compiled.sql_text


def test_compile_check_where_clause_includes_base_filters() -> None:
    doc = _doc()
    compiled = compile_check(doc)
    assert "(IsDeleted = 0)" in compiled.sql_text
    assert "(IsDummy = 0)" in compiled.sql_text


# --- tri-state CASE structure (F6) -------------------------------------------


def test_tri_state_outer_case_falls_through_null_or_false_prerequisite() -> None:
    doc = _doc(prerequisites=("AppointmentCompleted IS NOT NULL", "ScheduleDate IS NOT NULL"))
    compiled = compile_check(doc)
    # A single fall-through ELSE handles both a FALSE and a NULL prerequisite
    # gate identically (native CASE WHEN semantics) -- exactly one such ELSE
    # closes the outer CASE.
    assert compiled.sql_text.count("ELSE 'indeterminate'") == 2  # outer + inner
    assert "WHEN (AppointmentCompleted IS NOT NULL) AND (ScheduleDate IS NOT NULL) THEN" in (
        compiled.sql_text
    )


def test_tri_state_inner_case_maps_predicate_true_false_null() -> None:
    doc = _doc(predicate="AppointmentCompleted = 1")
    compiled = compile_check(doc)
    assert "WHEN (AppointmentCompleted = 1) THEN 'fail'" in compiled.sql_text
    assert "WHEN NOT (AppointmentCompleted = 1) THEN 'pass'" in compiled.sql_text


def test_empty_prerequisites_defaults_to_always_true_gate() -> None:
    doc = _doc(prerequisites=())
    compiled = compile_check(doc)
    assert "WHEN 1 = 1 THEN" in compiled.sql_text


# --- predicate-node compilation ----------------------------------------------


def test_all_node_compiles_to_conjunction() -> None:
    doc = _doc(predicate=AllNode(all=("A = 1", "B = 2")))
    compiled = compile_check(doc)
    assert "(A = 1) AND (B = 2)" in compiled.sql_text


def test_any_node_compiles_to_disjunction() -> None:
    doc = _doc(predicate=AnyNode(any=("A = 1", "B = 2")))
    compiled = compile_check(doc)
    assert "(A = 1) OR (B = 2)" in compiled.sql_text


def test_not_node_compiles_to_negation() -> None:
    doc = _doc(predicate=NotNode(not_="A = 1"))
    compiled = compile_check(doc)
    assert "NOT (A = 1)" in compiled.sql_text


def test_exists_node_compiles_to_exists_clause() -> None:
    doc = _doc(
        predicate=ExistsNode(
            exists=ExistsClause(
                view="dbo.SyntheticInvoice", on="dbo.SyntheticInvoice.AppointmentID = 1", where=None
            )
        )
    )
    compiled = compile_check(doc)
    # The 'fail' branch tests the bare predicate; only the tri-state
    # wrapper's 'pass' branch (NOT <predicate>) legitimately adds "NOT".
    assert "WHEN EXISTS (SELECT 1 FROM dbo.SyntheticInvoice WHERE" in compiled.sql_text
    assert "WHEN NOT EXISTS (SELECT 1 FROM dbo.SyntheticInvoice WHERE" in compiled.sql_text


def test_not_exists_node_compiles_to_not_exists_clause_with_where() -> None:
    doc = _doc(
        predicate=NotExistsNode(
            not_exists=ExistsClause(
                view="dbo.SyntheticInvoice",
                on="dbo.SyntheticInvoice.AppointmentID = dbo.SyntheticAppointment.AppointmentID",
                where="dbo.SyntheticInvoice.IsActive = 1",
            )
        )
    )
    compiled = compile_check(doc)
    assert "NOT EXISTS (SELECT 1 FROM dbo.SyntheticInvoice WHERE" in compiled.sql_text
    assert "AppointmentID = dbo.SyntheticAppointment.AppointmentID" in compiled.sql_text
    assert "dbo.SyntheticInvoice.IsActive = 1" in compiled.sql_text


def test_bare_leaf_predicate_compiles_without_wrapper() -> None:
    doc = _doc(predicate="TotalAmount < 0")
    compiled = compile_check(doc)
    assert "WHEN (TotalAmount < 0) THEN 'fail'" in compiled.sql_text


# --- params --------------------------------------------------------------------


def test_param_placeholder_substituted_with_named_sql_param() -> None:
    doc = _doc(
        predicate="ScheduleDate <= DATEADD(day, -{invoice_lag_days}, sysdatetime())",
        params={
            "invoice_lag_days": ParamDef(
                type="integer",
                default=ParamDefault(strategy="percentile", measure="m", p=95.0, fallback=7),
            )
        },
    )
    compiled = compile_check(doc)
    assert "@invoice_lag_days" in compiled.sql_text
    assert "{invoice_lag_days}" not in compiled.sql_text


def test_params_schema_reflects_declared_param_types() -> None:
    doc = _doc(
        params={
            "invoice_lag_days": ParamDef(
                type="integer",
                default=ParamDefault(strategy="fixed", value=7),
            )
        }
    )
    compiled = compile_check(doc)
    assert compiled.params_schema["invoice_lag_days"] == "integer"


# --- array params (IN-clause expansion, D-026 follow-up compiler amendment) --


def test_array_param_expands_to_one_named_param_per_element() -> None:
    doc = _doc(
        predicate="Status IN ({allowed_statuses})",
        params={
            "allowed_statuses": ParamDef(
                type="array",
                default=ParamDefault(strategy="fixed", value=["A", "B", "C"]),
            )
        },
    )
    compiled = compile_check(doc)
    assert "IN (@allowed_statuses_0, @allowed_statuses_1, @allowed_statuses_2)" in compiled.sql_text
    assert "{allowed_statuses}" not in compiled.sql_text
    assert compiled.params_schema["allowed_statuses_0"] == "string"
    assert compiled.params_schema["allowed_statuses_1"] == "string"
    assert compiled.params_schema["allowed_statuses_2"] == "string"
    assert "allowed_statuses" not in compiled.params_schema  # no bindable target for the bare name


def test_array_param_infers_element_type_from_value() -> None:
    doc = _doc(
        predicate="Code IN ({allowed_codes})",
        params={
            "allowed_codes": ParamDef(
                type="array",
                default=ParamDefault(strategy="fixed", value=[1, 2, 3]),
            )
        },
    )
    compiled = compile_check(doc)
    assert compiled.params_schema["allowed_codes_0"] == "integer"


def test_array_param_with_non_fixed_default_is_rejected() -> None:
    doc = _doc(
        predicate="Status IN ({allowed_statuses})",
        params={
            "allowed_statuses": ParamDef(
                type="array",
                default=ParamDefault(strategy="percentile", measure="m", p=95.0, fallback=["A"]),
            )
        },
    )
    with pytest.raises(ValueError, match="fixed"):
        compile_check(doc)


def test_array_param_with_empty_default_is_rejected() -> None:
    doc = _doc(
        predicate="Status IN ({allowed_statuses})",
        params={
            "allowed_statuses": ParamDef(
                type="array",
                default=ParamDefault(strategy="fixed", value=[]),
            )
        },
    )
    with pytest.raises(ValueError, match="non-empty"):
        compile_check(doc)


# --- watermark / increment scoping -------------------------------------------


def test_watermark_column_adds_increment_clause_with_named_params() -> None:
    doc = _doc()
    compiled = compile_check(doc, watermark_column="UpdatedAt")
    assert "(@watermark_from IS NULL OR UpdatedAt > @watermark_from)" in compiled.sql_text
    assert "(UpdatedAt <= @watermark_to)" in compiled.sql_text
    assert compiled.params_schema["watermark_from"] == "datetime"
    assert compiled.params_schema["watermark_to"] == "datetime"


def test_no_watermark_column_omits_increment_clause() -> None:
    doc = _doc()
    compiled = compile_check(doc, watermark_column=None)
    assert "@watermark_from" not in compiled.sql_text
    assert "watermark_from" not in compiled.params_schema


# --- determinism ---------------------------------------------------------------


def test_double_compile_is_byte_identical() -> None:
    doc = _doc()
    first = compile_check(doc, watermark_column="UpdatedAt")
    second = compile_check(doc, watermark_column="UpdatedAt")
    assert first.sql_text == second.sql_text
    assert first.sql_hash == second.sql_hash


def test_sql_hash_is_sha256_of_sql_text() -> None:
    import hashlib

    doc = _doc()
    compiled = compile_check(doc)
    assert compiled.sql_hash == hashlib.sha256(compiled.sql_text.encode("utf-8")).hexdigest()


def test_different_docs_produce_different_hashes() -> None:
    doc_a = _doc(predicate="A = 1")
    doc_b = _doc(predicate="B = 2")
    assert compile_check(doc_a).sql_hash != compile_check(doc_b).sql_hash


# --- end-to-end: real checked-in examples compile without error -------------


def test_all_six_checked_in_examples_compile() -> None:
    for path in sorted(EXAMPLES_DIR.glob("*.yaml")):
        doc = parse_check_document(path.read_text(encoding="utf-8"))
        compiled = compile_check(doc)
        assert len(compiled.sql_hash) == 64
        assert "SELECT" in compiled.sql_text
        assert f"FROM {doc.entity.view}" in compiled.sql_text


def test_unrecognized_predicate_node_type_is_rejected() -> None:
    doc = _doc(predicate=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        compile_check(doc)
