"""Phase 2 step 2: cdss.dsl parses a YAML check document into a typed model,
then validates it against the semantic catalog (F2 -- every view/column/join
referenced must exist) and the stub action registry. Structural (JSON Schema)
validation is step 1's schema, re-used here, never re-implemented.

All catalog fixtures below are entirely synthetic -- fabricated view/column
names -- and must never be mistaken for real INDICI_BI_Full data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from cdss.dsl import (
    STUB_ACTION_LIBRARY,
    AllNode,
    AnyNode,
    CatalogIndex,
    CheckReferenceError,
    CheckValidationError,
    ExistsNode,
    NotExistsNode,
    NotNode,
    check_doc_from_dict,
    parse_check_document,
    validate_check_against_catalog,
)

EXAMPLES_DIR = Path(__file__).parent.parent / "examples" / "checks"


def _column(name: str, column_class: str = "measure") -> dict[str, Any]:
    return {
        "column_name": name,
        "data_type": "nvarchar",
        "is_free_text": False,
        "column_class": column_class,
        "sampling": {"sampled": False, "method": "full"},
        "null_count": 0,
        "null_rate": 0.0,
        "distinct_count": 10,
        "min_value": None,
        "max_value": None,
        "top_values": [],
        "string_length_stats": None,
        "reference_samples": None,
        "value_pattern_stats": None,
    }


def _view(qualified_name: str, column_names: list[str]) -> dict[str, Any]:
    return {
        "qualified_name": qualified_name,
        "row_count": 10,
        "row_count_status": "exact",
        "archetype": "fact",
        "columns": [_column(name) for name in column_names],
        "candidate_keys": [],
        "watermark_classification": {"status": "fallback_needed", "columns": []},
        "sentinels": [],
        "test_record_indicators": [],
    }


SYNTHETIC_CATALOG: dict[str, Any] = {
    "catalog_version": 1,
    "produced_at": "2026-07-16T00:00:00+00:00",
    "source_database": "SyntheticDB",
    "views": [
        _view(
            "dbo.SyntheticAppointment",
            [
                "AppointmentID",
                "PatientID",
                "ScheduleDate",
                "AppointmentCompleted",
                "IsDeleted",
                "IsDummy",
                "PracticeID",
                "AppointmentType",
                "Provider",
            ],
        ),
        _view(
            "dbo.SyntheticInvoice",
            ["InvoiceID", "AppointmentID", "IsActive"],
        ),
    ],
    "relationships": [],
    "profiling_costs": [],
    "pruning_report": {
        "pairs_considered": 0,
        "pairs_pruned": 0,
        "pairs_evaluated": 0,
        "pairs_skipped_cost": 0,
    },
}


SYNTHETIC_CHECK = """
id: synthetic-completed-no-invoice
title: Synthetic completed appointment has no invoice
category: revenue-integrity
default_severity: medium
entity:
  view: dbo.SyntheticAppointment
  key: [AppointmentID]
  practice_column: PracticeID
  base_filters:
    - "IsDeleted = 0"
    - "IsDummy = 0"
params:
  invoice_lag_days:
    type: integer
    default: { strategy: percentile, measure: appointment_to_invoice_lag, p: 95, fallback: 7 }
prerequisites:
  - "AppointmentCompleted IS NOT NULL"
  - "ScheduleDate IS NOT NULL"
predicate:
  all:
    - "AppointmentCompleted = 1"
    - "ScheduleDate <= DATEADD(day, -{invoice_lag_days}, sysdatetime())"
    - not_exists:
        view: dbo.SyntheticInvoice
        "on": "dbo.SyntheticInvoice.AppointmentID = dbo.SyntheticAppointment.AppointmentID"
        where: "dbo.SyntheticInvoice.IsActive = 1"
evidence: [AppointmentID, PatientID, ScheduleDate, AppointmentType, Provider, PracticeID]
actions: [verify-invoice, raise-billing-task]
resolution: "An active invoice exists for the appointment, or the finding is dismissed."
"""


def _valid_doc_text() -> str:
    return SYNTHETIC_CHECK


# --- catalog fixture hygiene -------------------------------------------------


def test_synthetic_catalog_fixture_is_schema_valid() -> None:
    from cdss.catalog import load_schema

    jsonschema.validate(instance=SYNTHETIC_CATALOG, schema=load_schema())


# --- parsing: structural validation delegates to the step 1 schema ----------


def test_parse_check_document_returns_typed_model() -> None:
    doc = parse_check_document(_valid_doc_text())
    assert doc.id == "synthetic-completed-no-invoice"
    assert doc.entity.view == "dbo.SyntheticAppointment"
    assert doc.entity.key == ("AppointmentID",)
    assert doc.params["invoice_lag_days"].type == "integer"
    assert doc.params["invoice_lag_days"].default.strategy == "percentile"
    assert doc.params["invoice_lag_days"].default.measure == "appointment_to_invoice_lag"
    assert doc.actions == ("verify-invoice", "raise-billing-task")
    assert isinstance(doc.predicate, AllNode)
    assert isinstance(doc.predicate.all[2], NotExistsNode)
    assert doc.predicate.all[2].not_exists.view == "dbo.SyntheticInvoice"


def test_parse_check_document_parses_all_six_checked_in_examples() -> None:
    for path in sorted(EXAMPLES_DIR.glob("*.yaml")):
        doc = parse_check_document(path.read_text(encoding="utf-8"))
        assert doc.id


def test_check_doc_from_dict_matches_parse_check_document() -> None:
    text = _valid_doc_text()
    assert check_doc_from_dict(yaml.safe_load(text)) == parse_check_document(text)


def test_check_doc_from_dict_rejects_structurally_invalid_dict() -> None:
    with pytest.raises(CheckValidationError):
        check_doc_from_dict({"id": "missing-everything-else"})


def test_parse_check_document_rejects_missing_required_field() -> None:
    text = _valid_doc_text().replace("predicate:", "not_predicate:")
    with pytest.raises(CheckValidationError):
        parse_check_document(text)


def test_parse_check_document_rejects_unknown_category() -> None:
    text = _valid_doc_text().replace("category: revenue-integrity", "category: not-a-category")
    with pytest.raises(CheckValidationError):
        parse_check_document(text)


def test_predicate_not_node_parses() -> None:
    text = _valid_doc_text().replace(
        'predicate:\n  all:\n    - "AppointmentCompleted = 1"',
        'predicate:\n  all:\n    - not: "AppointmentCompleted = 0"',
    )
    doc = parse_check_document(text)
    assert isinstance(doc.predicate, AllNode)
    assert isinstance(doc.predicate.all[0], NotNode)


def test_predicate_any_and_exists_node_parse() -> None:
    text = (
        _valid_doc_text()
        .replace(
            "not_exists:",
            "exists:",
        )
        .replace(
            'predicate:\n  all:\n    - "AppointmentCompleted = 1"',
            'predicate:\n  any:\n    - "AppointmentCompleted = 1"',
        )
    )
    doc = parse_check_document(text)
    assert isinstance(doc.predicate, AnyNode)
    assert isinstance(doc.predicate.any[2], ExistsNode)


# --- semantic validation against the catalog (F2) ---------------------------


def test_valid_check_validates_against_matching_catalog() -> None:
    doc = parse_check_document(_valid_doc_text())
    catalog = CatalogIndex(SYNTHETIC_CATALOG)
    validate_check_against_catalog(doc, catalog)  # must not raise


def test_all_six_checked_in_examples_are_internally_consistent_stub_actions() -> None:
    # Every action any example uses must be in the stub registry -- proves the
    # registry wasn't seeded with an incomplete or drifted list.
    for path in sorted(EXAMPLES_DIR.glob("*.yaml")):
        doc = parse_check_document(path.read_text(encoding="utf-8"))
        for action in doc.actions:
            assert action in STUB_ACTION_LIBRARY, f"{path.name} uses undeclared action {action}"


def test_unknown_entity_view_is_refused_naming_it() -> None:
    doc = parse_check_document(_valid_doc_text())
    catalog = CatalogIndex(
        {**SYNTHETIC_CATALOG, "views": [SYNTHETIC_CATALOG["views"][1]]}
    )  # drop the driving view
    with pytest.raises(CheckReferenceError, match="dbo.SyntheticAppointment"):
        validate_check_against_catalog(doc, catalog)


def test_unknown_entity_key_column_is_refused_naming_it() -> None:
    text = _valid_doc_text().replace("key: [AppointmentID]", "key: [NotARealColumn]")
    doc = parse_check_document(text)
    catalog = CatalogIndex(SYNTHETIC_CATALOG)
    with pytest.raises(CheckReferenceError, match="NotARealColumn"):
        validate_check_against_catalog(doc, catalog)


def test_unknown_practice_column_is_refused_naming_it() -> None:
    text = _valid_doc_text().replace(
        "practice_column: PracticeID", "practice_column: NotARealPracticeColumn"
    )
    doc = parse_check_document(text)
    catalog = CatalogIndex(SYNTHETIC_CATALOG)
    with pytest.raises(CheckReferenceError, match="NotARealPracticeColumn"):
        validate_check_against_catalog(doc, catalog)


def test_unknown_column_in_base_filter_is_refused_naming_it() -> None:
    text = _valid_doc_text().replace('"IsDummy = 0"', '"NotARealFlag = 0"')
    doc = parse_check_document(text)
    catalog = CatalogIndex(SYNTHETIC_CATALOG)
    with pytest.raises(CheckReferenceError, match="NotARealFlag"):
        validate_check_against_catalog(doc, catalog)


def test_unknown_column_in_predicate_leaf_is_refused_naming_it() -> None:
    text = _valid_doc_text().replace('"AppointmentCompleted = 1"', '"NotARealPredicateColumn = 1"')
    doc = parse_check_document(text)
    catalog = CatalogIndex(SYNTHETIC_CATALOG)
    with pytest.raises(CheckReferenceError, match="NotARealPredicateColumn"):
        validate_check_against_catalog(doc, catalog)


def test_unknown_view_in_not_exists_clause_is_refused_naming_it() -> None:
    text = _valid_doc_text().replace("dbo.SyntheticInvoice", "dbo.NotARealView")
    doc = parse_check_document(text)
    catalog = CatalogIndex(SYNTHETIC_CATALOG)
    with pytest.raises(CheckReferenceError, match="dbo.NotARealView"):
        validate_check_against_catalog(doc, catalog)


def test_unknown_qualified_column_in_join_condition_is_refused_naming_it() -> None:
    text = _valid_doc_text().replace(
        "dbo.SyntheticInvoice.AppointmentID = dbo.SyntheticAppointment.AppointmentID",
        "dbo.SyntheticInvoice.NotARealJoinColumn = dbo.SyntheticAppointment.AppointmentID",
    )
    doc = parse_check_document(text)
    catalog = CatalogIndex(SYNTHETIC_CATALOG)
    with pytest.raises(CheckReferenceError, match="NotARealJoinColumn"):
        validate_check_against_catalog(doc, catalog)


def test_unknown_evidence_column_is_refused_naming_it() -> None:
    text = _valid_doc_text().replace(
        "evidence: [AppointmentID, PatientID, ScheduleDate, AppointmentType, Provider, PracticeID]",
        "evidence: [AppointmentID, NotARealEvidenceColumn]",
    )
    doc = parse_check_document(text)
    catalog = CatalogIndex(SYNTHETIC_CATALOG)
    with pytest.raises(CheckReferenceError, match="NotARealEvidenceColumn"):
        validate_check_against_catalog(doc, catalog)


def test_evidence_column_only_on_joined_view_still_validates() -> None:
    # Evidence may come from the driving view OR any declared join (spec text).
    text = _valid_doc_text().replace(
        "evidence: [AppointmentID, PatientID, ScheduleDate, AppointmentType, Provider, PracticeID]",
        "evidence: [AppointmentID, IsActive]",
    )
    doc = parse_check_document(text)
    catalog = CatalogIndex(SYNTHETIC_CATALOG)
    validate_check_against_catalog(doc, catalog)  # IsActive lives only on dbo.SyntheticInvoice


def test_unknown_action_is_refused_naming_it() -> None:
    text = _valid_doc_text().replace(
        "actions: [verify-invoice, raise-billing-task]", "actions: [not-a-real-action]"
    )
    doc = parse_check_document(text)
    catalog = CatalogIndex(SYNTHETIC_CATALOG)
    with pytest.raises(CheckReferenceError, match="not-a-real-action"):
        validate_check_against_catalog(doc, catalog)


def test_unknown_param_reference_is_refused_naming_it() -> None:
    text = _valid_doc_text().replace(
        "DATEADD(day, -{invoice_lag_days}, sysdatetime())",
        "DATEADD(day, -{undeclared_param}, sysdatetime())",
    )
    doc = parse_check_document(text)
    catalog = CatalogIndex(SYNTHETIC_CATALOG)
    with pytest.raises(CheckReferenceError, match="undeclared_param"):
        validate_check_against_catalog(doc, catalog)
