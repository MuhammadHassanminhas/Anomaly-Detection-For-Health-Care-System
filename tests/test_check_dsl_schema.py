"""Phase 2 step 1: the check-DSL schema is the structural gate every YAML
check document must pass before the Phase 2 step 2 parser attempts semantic
(catalog) validation. This test only exercises schema shape -- it does not
touch the semantic catalog or emit any SQL.
"""

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

SCHEMA_PATH = Path(__file__).parent.parent / "src" / "cdss" / "schemas" / "check-dsl.schema.json"
EXAMPLES_DIR = Path(__file__).parent.parent / "examples" / "checks"


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def load_example(name: str) -> dict[str, Any]:
    text = (EXAMPLES_DIR / name).read_text(encoding="utf-8")
    doc: dict[str, Any] = yaml.safe_load(text)
    return doc


# --- schema well-formedness -------------------------------------------------


def test_schema_file_is_valid_json_schema() -> None:
    schema = load_schema()
    jsonschema.Draft202012Validator.check_schema(schema)


# --- every checked-in example validates (>=6, incl. the ARCHITECTURE.md sketch) --


EXAMPLE_FILES = [
    "appointment-completed-no-invoice.yaml",
    "patient-active-missing-nhi.yaml",
    "invoice-stale-unpaid-balance.yaml",
    "appointment-invalid-status-code.yaml",
    "patient-no-recent-appointment.yaml",
    "invoice-negative-total-amount.yaml",
]


def test_at_least_six_example_checks_are_checked_in() -> None:
    assert len(EXAMPLE_FILES) >= 6
    for name in EXAMPLE_FILES:
        assert (EXAMPLES_DIR / name).exists(), f"missing example {name}"


@pytest.mark.parametrize("name", EXAMPLE_FILES)
def test_example_check_validates_against_schema(name: str) -> None:
    jsonschema.validate(instance=load_example(name), schema=load_schema())


def test_architecture_sketch_matches_checked_in_example_exactly() -> None:
    # ARCHITECTURE.md 2.3's YAML sketch is the normative illustrative example --
    # confirm the checked-in file has not silently drifted from it.
    sketch = {
        "id": "appointment-completed-no-invoice",
        "title": "Completed appointment has no invoice",
        "category": "revenue-integrity",
        "default_severity": "medium",
        "entity": {
            "view": "dbo.Appointments",
            "key": ["AppointmentID"],
            "practice_column": "PracticeID",
            "base_filters": ["IsDeleted = 0", "IsDummy = 0"],
        },
        "params": {
            "invoice_lag_days": {
                "type": "integer",
                "default": {
                    "strategy": "percentile",
                    "measure": "appointment_to_invoice_lag",
                    "p": 95,
                    "fallback": 7,
                },
            }
        },
        "prerequisites": ["AppointmentCompleted IS NOT NULL", "ScheduleDate IS NOT NULL"],
        "predicate": {
            "all": [
                "AppointmentCompleted = 1",
                "ScheduleDate <= DATEADD(day, -{invoice_lag_days}, sysdatetime())",
                {
                    "not_exists": {
                        "view": "dbo.Invoices",
                        "on": "dbo.Invoices.AppointmentID = dbo.Appointments.AppointmentID",
                        "where": "dbo.Invoices.IsActive = 1",
                    }
                },
            ]
        },
        "evidence": [
            "AppointmentID",
            "PatientID",
            "ScheduleDate",
            "AppointmentType",
            "Provider",
            "PracticeID",
        ],
        "actions": ["verify-invoice", "raise-billing-task"],
        "resolution": (
            "An active invoice exists for the appointment, or the finding is dismissed "
            "with a reason."
        ),
    }
    assert load_example("appointment-completed-no-invoice.yaml") == sketch


# --- malformed examples are rejected, with an actionable error -------------


def _valid_doc() -> dict[str, Any]:
    return load_example("invoice-negative-total-amount.yaml")


def test_missing_required_top_level_field_is_rejected() -> None:
    doc = _valid_doc()
    del doc["predicate"]
    with pytest.raises(jsonschema.ValidationError, match="predicate"):
        jsonschema.validate(instance=doc, schema=load_schema())


def test_unknown_top_level_field_is_rejected() -> None:
    doc = _valid_doc()
    doc["not_a_real_field"] = "nope"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=doc, schema=load_schema())


def test_unknown_category_is_rejected() -> None:
    doc = _valid_doc()
    doc["category"] = "not-a-real-category"
    with pytest.raises(jsonschema.ValidationError, match="not-a-real-category"):
        jsonschema.validate(instance=doc, schema=load_schema())


def test_entity_missing_view_is_rejected() -> None:
    doc = _valid_doc()
    del doc["entity"]["view"]
    with pytest.raises(jsonschema.ValidationError, match="view"):
        jsonschema.validate(instance=doc, schema=load_schema())


def test_entity_key_must_be_non_empty_array() -> None:
    doc = _valid_doc()
    doc["entity"]["key"] = []
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=doc, schema=load_schema())


def test_param_missing_default_is_rejected() -> None:
    doc = _valid_doc()
    doc["params"] = {"threshold": {"type": "integer"}}
    with pytest.raises(jsonschema.ValidationError, match="default"):
        jsonschema.validate(instance=doc, schema=load_schema())


def test_param_default_with_unknown_strategy_is_rejected() -> None:
    doc = _valid_doc()
    doc["params"] = {"threshold": {"type": "integer", "default": {"strategy": "guess"}}}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=doc, schema=load_schema())


def test_percentile_default_missing_measure_is_rejected() -> None:
    doc = _valid_doc()
    doc["params"] = {
        "threshold": {
            "type": "integer",
            "default": {"strategy": "percentile", "p": 95, "fallback": 1},
        }
    }
    # jsonschema's oneOf doesn't guarantee which branch's error message surfaces --
    # asserting refusal, not exact wording, matches this project's existing precedent.
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=doc, schema=load_schema())


def test_predicate_node_with_both_all_and_any_is_rejected() -> None:
    doc = _valid_doc()
    doc["predicate"] = {"all": ["TotalAmount < 0"], "any": ["TotalAmount < 0"]}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=doc, schema=load_schema())


def test_predicate_node_with_no_recognized_key_is_rejected() -> None:
    doc = _valid_doc()
    doc["predicate"] = {"maybe": ["TotalAmount < 0"]}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=doc, schema=load_schema())


def test_not_exists_clause_missing_on_is_rejected() -> None:
    doc = _valid_doc()
    doc["predicate"] = {
        "all": [
            {"not_exists": {"view": "dbo.Invoices"}},
        ]
    }
    with pytest.raises(jsonschema.ValidationError, match="on"):
        jsonschema.validate(instance=doc, schema=load_schema())


def test_evidence_must_be_non_empty() -> None:
    doc = _valid_doc()
    doc["evidence"] = []
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=doc, schema=load_schema())


def test_actions_must_be_non_empty() -> None:
    doc = _valid_doc()
    doc["actions"] = []
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=doc, schema=load_schema())


def test_id_must_match_kebab_case_pattern() -> None:
    doc = _valid_doc()
    doc["id"] = "Not A Valid ID!"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=doc, schema=load_schema())
