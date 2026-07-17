"""Phase 4 step 4: cdss.authoring.llm_draft -- the LLM-drafted check harness
(F3b). Pure tests use a `FakeLLMClient` (no network, no real API cost) and an
entirely synthetic fixture catalog (fabricated view/column names, same
convention as tests/test_dsl.py and tests/test_derive.py). The
redaction-boundary test is the step's own named deliverable: it proves the
prompt sent to any client never contains an identifier-classified column's
value, even when one is adversarially present in the fixture catalog.
DB-gated persistence tests require CDSS_APP_DB_URL and skip (never fail)
otherwise -- D-009.1.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import sqlalchemy as sa

from cdss.authoring.llm_draft import (
    LLMConfig,
    MissingLLMConfigError,
    build_catalog_context,
    build_prompt,
    draft_checks_for_category,
    load_llm_config,
    parse_llm_response,
    persist_llm_drafts,
)


class FakeLLMClient:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._responses.pop(0)


def _column(
    name: str,
    *,
    data_type: str = "int",
    column_class: str = "key",
    distinct_count: int | None = 10,
    min_value: str | None = None,
    max_value: str | None = None,
    top_values: list[dict[str, Any]] | None = None,
    reference_samples: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "column_name": name,
        "data_type": data_type,
        "is_free_text": False,
        "column_class": column_class,
        "sampling": {"sampled": False, "method": "none"},
        "null_count": 0,
        "null_rate": 0.0,
        "distinct_count": distinct_count,
        "min_value": min_value,
        "max_value": max_value,
        "top_values": top_values or [],
        "string_length_stats": None,
        "reference_samples": reference_samples,
        "value_pattern_stats": None,
    }


def _fixture_catalog() -> dict[str, Any]:
    """One synthetic view carrying every column class this harness redacts
    differently, including an *adversarial* identifier column that (unlike
    the real profiler's own guarantee) has non-null value fields -- proving
    the redaction is this module's own re-check, not blind trust in
    upstream never regressing."""
    view = {
        "qualified_name": "dbo.FakePatient",
        "row_count": 500,
        "row_count_status": "exact",
        "archetype": "fact",
        "columns": [
            _column(
                "ProfileID", data_type="int", column_class="key", min_value="1", max_value="500"
            ),
            _column("PracticeID", data_type="int", column_class="key", distinct_count=3),
            _column(
                "FirstName",
                data_type="nvarchar",
                column_class="identifier_or_freetext",
                min_value="AARON",
                max_value="ZOE",
                top_values=[{"value": "John", "frequency": 5}],
            ),
            _column(
                "StatusCode",
                data_type="varchar",
                column_class="categorical_coded",
                top_values=[
                    {"value": "Active", "frequency": 400},
                    {"value": "Inactive", "frequency": 100},
                ],
            ),
            _column(
                "DiseaseName",
                data_type="nvarchar",
                column_class="reference_vocabulary",
                reference_samples={"values": ["Diphtheria", "Tetanus"], "sample_only": True},
            ),
            _column(
                "EnrollmentDate",
                data_type="datetime2",
                column_class="measure",
                min_value="2020-01-01",
                max_value="2026-01-01",
            ),
        ],
        "candidate_keys": [
            {
                "columns": ["ProfileID"],
                "distinct_count": 500,
                "row_count": 500,
                "evidence_method": "exact",
            }
        ],
        "watermark_classification": {"status": "fallback_needed", "columns": []},
        "sentinels": [],
        "test_record_indicators": [],
    }
    invoice_view = {
        "qualified_name": "dbo.FakeInvoice",
        "row_count": 100,
        "row_count_status": "exact",
        "archetype": "fact",
        "columns": [
            _column("InvoiceID", data_type="int", column_class="key"),
            _column("ProfileID", data_type="int", column_class="key"),
            _column("PracticeID", data_type="int", column_class="key", distinct_count=3),
        ],
        "candidate_keys": [],
        "watermark_classification": {"status": "fallback_needed", "columns": []},
        "sentinels": [],
        "test_record_indicators": [],
    }
    return {
        "catalog_version": 1,
        "produced_at": "2026-01-01T00:00:00+00:00",
        "source_database": "TEST_DB",
        "views": [view, invoice_view],
        "relationships": [],
        "profiling_costs": [],
        "pruning_report": {
            "pairs_considered": 0,
            "pairs_pruned": 0,
            "pairs_evaluated": 0,
            "pairs_skipped_cost": 0,
        },
    }


# --- LLM config ---------------------------------------------------------------


def test_load_llm_config_raises_naming_missing_api_key() -> None:
    with pytest.raises(MissingLLMConfigError, match="OPENAI_API_KEY"):
        load_llm_config({"OPENAI_MODEL": "gpt-4o-mini"})


def test_load_llm_config_raises_naming_missing_model() -> None:
    with pytest.raises(MissingLLMConfigError, match="OPENAI_MODEL"):
        load_llm_config({"OPENAI_API_KEY": "sk-test"})


def test_load_llm_config_reads_both_vars() -> None:
    config = load_llm_config({"OPENAI_API_KEY": "sk-test", "OPENAI_MODEL": "gpt-4o-mini"})
    assert config == LLMConfig(api_key="sk-test", model="gpt-4o-mini")


# --- redaction boundary (the step's own named deliverable) ------------------


def test_redacted_context_never_includes_identifier_column_values() -> None:
    context = build_catalog_context(_fixture_catalog())
    columns = context["views"][0]["columns"]
    first_name = next(c for c in columns if c["column_name"] == "FirstName")
    assert "min_value" not in first_name
    assert "max_value" not in first_name
    assert "top_values" not in first_name
    assert "reference_samples" not in first_name
    assert first_name["column_class"] == "identifier_or_freetext"


def test_redacted_context_includes_safe_aggregate_and_domain_data() -> None:
    context = build_catalog_context(_fixture_catalog())
    columns = {c["column_name"]: c for c in context["views"][0]["columns"]}
    assert columns["StatusCode"]["top_values"] == ["Active", "Inactive"]
    assert columns["DiseaseName"]["reference_samples"] == ["Diphtheria", "Tetanus"]
    assert columns["EnrollmentDate"]["min_value"] == "2020-01-01"
    assert columns["ProfileID"]["max_value"] == "500"


def test_prompt_never_contains_the_adversarial_identifier_values() -> None:
    context = build_catalog_context(_fixture_catalog())
    prompt = build_prompt(context, "workflow")
    assert "AARON" not in prompt
    assert "ZOE" not in prompt
    assert "John" not in prompt
    # sanity: the prompt is not vacuous -- safe data really is present
    assert "StatusCode" in prompt
    assert "Diphtheria" in prompt


# --- parse_llm_response ------------------------------------------------------


def test_parse_llm_response_accepts_a_json_array() -> None:
    assert parse_llm_response('[{"id": "a"}, {"id": "b"}]') == [{"id": "a"}, {"id": "b"}]


def test_parse_llm_response_wraps_a_bare_object() -> None:
    assert parse_llm_response('{"id": "a"}') == [{"id": "a"}]


def test_parse_llm_response_strips_markdown_code_fences() -> None:
    text = '```json\n[{"id": "a"}]\n```'
    assert parse_llm_response(text) == [{"id": "a"}]


def test_parse_llm_response_rejects_non_json() -> None:
    with pytest.raises(json.JSONDecodeError):
        parse_llm_response("not json at all")


# --- validate-or-repair loop --------------------------------------------------

_VALID_DRAFT_1 = {
    "id": "fake-patient-inactive-status",
    "title": "FakePatient has an inactive status",
    "category": "workflow",
    "default_severity": "medium",
    "entity": {
        "view": "dbo.FakePatient",
        "key": ["ProfileID"],
        "practice_column": "PracticeID",
        "base_filters": [],
    },
    "params": {},
    "prerequisites": ["StatusCode IS NOT NULL"],
    "predicate": "StatusCode = 'Inactive'",
    "evidence": ["ProfileID", "StatusCode", "PracticeID"],
    "actions": ["flag-for-data-steward-review"],
    "resolution": "StatusCode is corrected, or the finding is dismissed with a reason.",
}

_VALID_DRAFT_2 = {
    **_VALID_DRAFT_1,
    "id": "fake-patient-stale-enrollment",
    "predicate": "EnrollmentDate < '2020-06-01'",
    "prerequisites": ["EnrollmentDate IS NOT NULL"],
}

_DRAFT_WITH_BOGUS_COLUMN = {
    **_VALID_DRAFT_1,
    "id": "fake-patient-bogus-column",
    "predicate": "BogusColumn = 1",
    "prerequisites": [],
}

_REPAIRED_DRAFT = {
    **_VALID_DRAFT_1,
    "id": "fake-patient-bogus-column",
    "predicate": "StatusCode = 'Active'",
}

_DRAFT_WITH_BOGUS_VIEW = {
    **_VALID_DRAFT_1,
    "id": "fake-patient-bogus-view",
    "entity": {**_VALID_DRAFT_1["entity"], "view": "dbo.DoesNotExist"},
}


def test_draft_checks_for_category_accepts_valid_drafts_repairs_one_and_drops_the_other() -> None:
    initial_response = json.dumps(
        [_VALID_DRAFT_1, _VALID_DRAFT_2, _DRAFT_WITH_BOGUS_COLUMN, _DRAFT_WITH_BOGUS_VIEW]
    )
    repaired_response = json.dumps(_REPAIRED_DRAFT)
    still_broken_response = json.dumps(_DRAFT_WITH_BOGUS_VIEW)
    client = FakeLLMClient([initial_response, repaired_response, still_broken_response])

    drafts = draft_checks_for_category(client, _fixture_catalog(), "workflow")

    assert {d.slug for d in drafts} == {
        "fake-patient-inactive-status",
        "fake-patient-stale-enrollment",
        "fake-patient-bogus-column",
    }
    assert len(client.prompts) == 3  # 1 initial + 1 repair per invalid candidate


def test_affected_views_includes_joined_views_not_just_the_entity_view() -> None:
    draft_with_join = {
        **_VALID_DRAFT_1,
        "id": "fake-patient-no-invoice",
        "predicate": {
            "not_exists": {
                "view": "dbo.FakeInvoice",
                "on": "dbo.FakeInvoice.ProfileID = dbo.FakePatient.ProfileID",
            }
        },
        "prerequisites": [],
    }
    client = FakeLLMClient([json.dumps([draft_with_join])])

    drafts = draft_checks_for_category(client, _fixture_catalog(), "workflow")

    assert len(drafts) == 1
    assert set(drafts[0].affected_views) == {"dbo.FakePatient", "dbo.FakeInvoice"}


# --- persistence (DB-gated) --------------------------------------------------


def test_persist_llm_drafts_inserts_with_llm_source_and_draft_status(conn: sa.Connection) -> None:
    client = FakeLLMClient([json.dumps([_VALID_DRAFT_1])])
    drafts = draft_checks_for_category(client, _fixture_catalog(), "workflow")
    inserted_ids = persist_llm_drafts(conn, drafts)
    assert len(inserted_ids) == 1
    row = conn.execute(
        sa.text("SELECT source, status FROM checks WHERE id = :id"), {"id": inserted_ids[0]}
    ).one()
    assert row.source == "llm"
    assert row.status == "draft"


def test_persist_llm_drafts_is_idempotent_by_slug(conn: sa.Connection) -> None:
    client = FakeLLMClient([json.dumps([_VALID_DRAFT_1])])
    drafts = draft_checks_for_category(client, _fixture_catalog(), "workflow")
    first_ids = persist_llm_drafts(conn, drafts)
    second_ids = persist_llm_drafts(conn, drafts)
    assert second_ids == []
    count = conn.execute(
        sa.text("SELECT count(*) FROM checks WHERE id = ANY(:ids)"), {"ids": first_ids}
    ).scalar_one()
    assert count == 1
