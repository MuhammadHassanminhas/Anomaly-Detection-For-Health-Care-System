"""Phase 1 step 1: the semantic-catalog schema is the sole shape the check
compiler will accept (D-017). This test only exercises the schema itself —
profiling logic arrives in later steps. The fixture below is entirely
synthetic (fabricated view/column names) and must never be mistaken for a
real INDICI_BI_Full profiling result.
"""

import json
from pathlib import Path

import jsonschema
import pytest

SCHEMA_PATH = (
    Path(__file__).parent.parent / "src" / "cdss" / "schemas" / "semantic-catalog.schema.json"
)

# Synthetic fixture only -- not real INDICI_BI_Full data.
MINIMAL_CATALOG = {
    "catalog_version": 1,
    "produced_at": "2026-07-15T00:00:00+00:00",
    "source_database": "INDICI_BI_Full",
    "views": [
        {
            "qualified_name": "dbo.SyntheticView",
            "row_count": 100,
            "row_count_status": "exact",
            "columns": [
                {
                    "column_name": "SyntheticID",
                    "data_type": "int",
                    "is_free_text": False,
                    "column_class": "key",
                    "sampling": {"sampled": False, "method": "full"},
                    "null_count": 0,
                    "null_rate": 0.0,
                    "distinct_count": 100,
                    "min_value": "1",
                    "max_value": "100",
                    "top_values": [],
                    "string_length_stats": None,
                    "reference_samples": None,
                    "value_pattern_stats": None,
                }
            ],
            "archetype": "fact",
            "candidate_keys": [
                {
                    "columns": ["SyntheticID"],
                    "distinct_count": 100,
                    "row_count": 100,
                    "evidence_method": "exact",
                }
            ],
            "watermark_classification": {
                "status": "fallback_needed",
                "columns": [],
            },
            "sentinels": [],
            "test_record_indicators": [],
        }
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


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_schema_file_is_valid_json_schema() -> None:
    schema = load_schema()
    jsonschema.Draft202012Validator.check_schema(schema)


def test_minimal_fixture_validates_against_schema() -> None:
    jsonschema.validate(instance=MINIMAL_CATALOG, schema=load_schema())


def test_missing_required_top_level_field_is_rejected() -> None:
    invalid = {k: v for k, v in MINIMAL_CATALOG.items() if k != "views"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=invalid, schema=load_schema())


def test_unknown_top_level_field_is_rejected() -> None:
    invalid = dict(MINIMAL_CATALOG, unexpected_field="nope")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=invalid, schema=load_schema())


def test_sampled_column_must_still_declare_free_text_flag() -> None:
    # Free-text columns must skip top_values entirely (PHI-at-rest guard) --
    # the schema enforces top_values absent/empty when is_free_text is true
    # only via profiler discipline, not schema constraint; this test just
    # confirms a free-text column profile with no top_values still validates.
    catalog = json.loads(json.dumps(MINIMAL_CATALOG))
    catalog["views"][0]["columns"][0]["is_free_text"] = True
    catalog["views"][0]["columns"][0]["top_values"] = []
    jsonschema.validate(instance=catalog, schema=load_schema())


def test_sampled_column_records_strategy_and_predicate_in_method_string() -> None:
    # D-018: sampling estimates are per-column (`ColumnProfile.sampling`), and
    # reproducibility is encoded in the free-text `method` string rather than
    # a separate schema field -- e.g. modulo, watermark-range, or random.
    catalog = json.loads(json.dumps(MINIMAL_CATALOG))
    catalog["views"][0]["columns"][0]["sampling"] = {
        "sampled": True,
        "method": "modulo:SyntheticID%10=0",
    }
    jsonschema.validate(instance=catalog, schema=load_schema())


def test_column_profile_without_sampling_is_rejected() -> None:
    catalog = json.loads(json.dumps(MINIMAL_CATALOG))
    del catalog["views"][0]["columns"][0]["sampling"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=catalog, schema=load_schema())


def test_column_profile_without_column_class_is_rejected() -> None:
    # D-020: every column profile must declare its PHI capture tier.
    catalog = json.loads(json.dumps(MINIMAL_CATALOG))
    del catalog["views"][0]["columns"][0]["column_class"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=catalog, schema=load_schema())


def test_column_profile_rejects_unknown_column_class() -> None:
    catalog = json.loads(json.dumps(MINIMAL_CATALOG))
    catalog["views"][0]["columns"][0]["column_class"] = "not_a_real_class"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=catalog, schema=load_schema())


# --- D-023: table archetype + reference-vocabulary capture ----------------------


def test_view_profile_without_archetype_is_rejected() -> None:
    catalog = json.loads(json.dumps(MINIMAL_CATALOG))
    del catalog["views"][0]["archetype"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=catalog, schema=load_schema())


def test_view_profile_rejects_unknown_archetype() -> None:
    catalog = json.loads(json.dumps(MINIMAL_CATALOG))
    catalog["views"][0]["archetype"] = "not_a_real_archetype"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=catalog, schema=load_schema())


def test_column_profile_without_reference_samples_is_rejected() -> None:
    catalog = json.loads(json.dumps(MINIMAL_CATALOG))
    del catalog["views"][0]["columns"][0]["reference_samples"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=catalog, schema=load_schema())


def test_column_profile_without_value_pattern_stats_is_rejected() -> None:
    catalog = json.loads(json.dumps(MINIMAL_CATALOG))
    del catalog["views"][0]["columns"][0]["value_pattern_stats"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=catalog, schema=load_schema())


def test_reference_vocabulary_column_validates_with_samples_and_tag_stats() -> None:
    catalog = json.loads(json.dumps(MINIMAL_CATALOG))
    catalog["views"][0]["archetype"] = "reference"
    column = catalog["views"][0]["columns"][0]
    column["column_class"] = "reference_vocabulary"
    column["reference_samples"] = {"values": ["Alpha", "Beta"], "sample_only": True}
    column["value_pattern_stats"] = {"trailing_tag_counts": {"(disorder)": 12, "(procedure)": 5}}
    jsonschema.validate(instance=catalog, schema=load_schema())


def test_reference_samples_requires_sample_only_true() -> None:
    catalog = json.loads(json.dumps(MINIMAL_CATALOG))
    catalog["views"][0]["columns"][0]["reference_samples"] = {
        "values": ["Alpha"],
        "sample_only": False,
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=catalog, schema=load_schema())


# --- D-021: pair/containment pruning report --------------------------------------


def test_catalog_without_pruning_report_is_rejected() -> None:
    catalog = json.loads(json.dumps(MINIMAL_CATALOG))
    del catalog["pruning_report"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=catalog, schema=load_schema())


def test_pruning_report_missing_field_is_rejected() -> None:
    catalog = json.loads(json.dumps(MINIMAL_CATALOG))
    del catalog["pruning_report"]["pairs_evaluated"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=catalog, schema=load_schema())
