"""Phase 5 step 4: cdss.narrate.compose -- the Tier S narration pipeline.
Failure-injection is this step's own named deliverable: LLM returns
garbage / smuggles a value / times out must all fall back, and the finding
(never modeled directly here -- `evidence`/`params` stand in for it) must
never be lost or delayed. Pure logic except the one persistence test.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json

import pytest
import sqlalchemy as sa

from cdss.narrate import (
    ComposeResult,
    RedactionOffInProductionError,
    _evidence_type_name,
    build_narration_context,
    compose,
    parse_narration_response,
    persist_narrative,
    resolve_redaction_mode,
)

_DEFINITION = {
    "id": "invoice-negative-total-amount",
    "category": "data-quality",
    "evidence": ["InvoiceTransactionID", "TotalAmount"],
    "actions": ["flag-for-data-steward-review"],
    "resolution": "TotalAmount is corrected, or the finding is dismissed with a reason.",
    "params": {},
}
_EVIDENCE = {"InvoiceTransactionID": "INV-1", "TotalAmount": "-12.50"}
_FALLBACK_TEMPLATE = "This check has flagged a record for manual review."


class FakeLLMClient:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._responses.pop(0)


class TimingOutLLMClient:
    def complete(self, prompt: str) -> str:
        raise TimeoutError("the LLM did not respond in time")


def _compose(client: object, **overrides: object) -> ComposeResult:
    kwargs: dict[str, object] = {
        "model_id": "gpt-4o-mini",
        "check_version_id": "check-version-1",
        "definition": _DEFINITION,
        "rationale": "TotalAmount should never be negative.",
        "fallback_template": _FALLBACK_TEMPLATE,
        "evidence": _EVIDENCE,
        "params": {},
        "env": {"CDSS_ENV": "test"},
    }
    kwargs.update(overrides)
    return compose(client, **kwargs)  # type: ignore[arg-type]


# --- the happy path ----------------------------------------------------------


def test_a_valid_llm_response_is_rendered_and_recorded_as_valid() -> None:
    response = json.dumps(
        {
            "template": "Invoice {{InvoiceTransactionID}} has a negative total of {{TotalAmount}}.",
            "actions": ["flag-for-data-steward-review"],
        }
    )
    result = _compose(FakeLLMClient([response]))
    assert result.validation_status == "valid"
    assert result.rendered == "Invoice INV-1 has a negative total of -12.50."
    assert result.model_id == "gpt-4o-mini"
    assert result.prompt_hash is not None
    assert result.actions == ["flag-for-data-steward-review"]


# --- failure injection: the step's own named deliverable ---------------------


def test_llm_returns_garbage_falls_back_and_the_finding_is_still_narrated() -> None:
    result = _compose(FakeLLMClient(["not json at all {{{"]))
    assert result.validation_status == "fallback_static"
    assert result.rendered == _FALLBACK_TEMPLATE
    assert result.model_id is None
    assert result.prompt_hash is None
    assert result.actions == []


def test_llm_response_missing_a_template_field_falls_back() -> None:
    result = _compose(FakeLLMClient([json.dumps({"actions": []})]))
    assert result.validation_status == "fallback_static"
    assert result.rendered == _FALLBACK_TEMPLATE


def test_llm_smuggles_a_value_not_in_evidence_or_params_falls_back() -> None:
    response = json.dumps(
        {
            "template": "Invoice {{InvoiceTransactionID}} is overdue by more than 14 days.",
            "actions": [],
        }
    )
    result = _compose(FakeLLMClient([response]))
    assert result.validation_status == "blocked_fallback"
    assert result.rendered == _FALLBACK_TEMPLATE
    assert result.model_id is None
    assert result.prompt_hash is None


def test_llm_references_an_unresolvable_placeholder_falls_back() -> None:
    response = json.dumps({"template": "{{not_a_real_field}}", "actions": []})
    result = _compose(FakeLLMClient([response]))
    assert result.validation_status == "blocked_fallback"
    assert result.rendered == _FALLBACK_TEMPLATE


def test_llm_selects_an_action_off_the_allowlist_falls_back() -> None:
    response = json.dumps(
        {
            "template": "Invoice {{InvoiceTransactionID}} has a negative total.",
            "actions": ["prescribe-medication"],
        }
    )
    result = _compose(FakeLLMClient([response]))
    assert result.validation_status == "blocked_fallback"


def test_llm_times_out_falls_back_and_the_finding_is_still_narrated() -> None:
    result = _compose(TimingOutLLMClient())
    assert result.validation_status == "fallback_static"
    assert result.rendered == _FALLBACK_TEMPLATE


# --- redaction-mode resolution -------------------------------------------------


def test_redaction_mode_defaults_to_tier_s() -> None:
    assert resolve_redaction_mode({}) == "tier_s"


def test_redaction_mode_off_is_refused_when_env_unset_fail_closed() -> None:
    with pytest.raises(RedactionOffInProductionError):
        resolve_redaction_mode({"CDSS_REDACTION_MODE": "off"})


def test_redaction_mode_off_is_refused_in_production() -> None:
    with pytest.raises(RedactionOffInProductionError):
        resolve_redaction_mode({"CDSS_REDACTION_MODE": "off", "CDSS_ENV": "production"})


def test_redaction_mode_off_is_permitted_outside_production() -> None:
    assert resolve_redaction_mode({"CDSS_REDACTION_MODE": "off", "CDSS_ENV": "development"}) == (
        "off"
    )


def test_unknown_redaction_mode_raises() -> None:
    with pytest.raises(ValueError, match="CDSS_REDACTION_MODE"):
        resolve_redaction_mode({"CDSS_REDACTION_MODE": "bogus"})


# --- Tier S context never carries a value; off mode does ----------------------


def test_tier_s_context_carries_names_and_types_only_never_a_value() -> None:
    context = build_narration_context(
        rationale="r",
        category="data-quality",
        resolution="res",
        evidence=_EVIDENCE,
        params={"threshold": 5},
        param_types={"threshold": "integer"},
        action_allowlist={"flag-for-data-steward-review"},
        mode="tier_s",
    )
    for field in context.evidence_fields + context.param_fields:
        assert "value" not in field
    assert {f["name"] for f in context.evidence_fields} == set(_EVIDENCE)


def test_off_mode_context_carries_the_rendered_value_too() -> None:
    context = build_narration_context(
        rationale="r",
        category="data-quality",
        resolution="res",
        evidence=_EVIDENCE,
        params={},
        param_types={},
        action_allowlist=set(),
        mode="off",
    )
    by_name = {f["name"]: f for f in context.evidence_fields}
    assert by_name["InvoiceTransactionID"]["value"] == "INV-1"


# --- evidence type classification (drives the Tier S "type" field) ----------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, "boolean"),
        (dt.datetime(2026, 3, 3, 10, 30), "datetime"),
        (dt.date(2026, 3, 3), "date"),
        (decimal.Decimal("1.50"), "decimal"),
        (1.5, "decimal"),
        (5, "integer"),
        (None, "null"),
        ("INV-1", "string"),
    ],
)
def test_evidence_type_name_classifies_every_value_shape(value: object, expected: str) -> None:
    assert _evidence_type_name(value) == expected


# --- response parsing ----------------------------------------------------------


def test_parse_narration_response_reads_template_and_actions() -> None:
    template, actions = parse_narration_response(
        json.dumps({"template": "{{x}}", "actions": ["a", "b"]})
    )
    assert template == "{{x}}"
    assert actions == ["a", "b"]


def test_parse_narration_response_defaults_actions_to_empty() -> None:
    template, actions = parse_narration_response(json.dumps({"template": "{{x}}"}))
    assert template == "{{x}}"
    assert actions == []


def test_parse_narration_response_strips_markdown_fences() -> None:
    template, _ = parse_narration_response('```json\n{"template": "{{x}}", "actions": []}\n```')
    assert template == "{{x}}"


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        json.dumps({"actions": []}),
        json.dumps({"template": 5}),
        json.dumps({"template": "{{x}}", "actions": "not-a-list"}),
        json.dumps({"template": "{{x}}", "actions": [1, 2]}),
    ],
)
def test_parse_narration_response_rejects_malformed_shapes(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_narration_response(raw)


# --- persistence (app DB only) -----------------------------------------------


def _seed_minimal_finding(conn: sa.Connection) -> str:
    check_id = conn.execute(
        sa.text(
            "INSERT INTO checks (slug, title, category, default_severity, source, status) "
            "VALUES ('narration-compose-check', 'x', 'data-quality', 'high', 'manual', "
            "'active') RETURNING id"
        )
    ).scalar_one()
    version_id = conn.execute(
        sa.text(
            "INSERT INTO check_versions "
            "(check_id, version_number, definition, definition_hash, affected_views, "
            "params_schema, fallback_template) "
            "VALUES (:check_id, 1, CAST('{}' AS jsonb), 'hash', ARRAY[]::text[], "
            "CAST('{}' AS jsonb), :fallback) RETURNING id"
        ),
        {"check_id": check_id, "fallback": _FALLBACK_TEMPLATE},
    ).scalar_one()
    conn.execute(
        sa.text(
            "INSERT INTO practices (practice_id, name) VALUES ('practice-compose-test', 'x') "
            "ON CONFLICT DO NOTHING"
        )
    )
    run_id = conn.execute(sa.text("INSERT INTO runs DEFAULT VALUES RETURNING id")).scalar_one()
    finding_id = conn.execute(
        sa.text(
            "INSERT INTO findings "
            "(check_id, check_version_id, practice_id, dedupe_key, entity_key, severity, "
            "evidence, first_seen_run_id, last_seen_run_id) "
            "VALUES (:check_id, :version_id, 'practice-compose-test', 'dedupe-1', "
            "CAST('{}' AS jsonb), 'high', CAST('{}' AS jsonb), :run_id, :run_id) "
            "RETURNING id"
        ),
        {"check_id": check_id, "version_id": version_id, "run_id": run_id},
    ).scalar_one()
    return str(finding_id)


def test_persist_narrative_stores_a_valid_compose_result(conn: sa.Connection) -> None:
    finding_id = _seed_minimal_finding(conn)
    result = ComposeResult(
        template="Invoice {{InvoiceTransactionID}} has a negative total.",
        rendered="Invoice INV-1 has a negative total.",
        model_id="gpt-4o-mini",
        prompt_hash="abc123",
        validation_status="valid",
        actions=["flag-for-data-steward-review"],
    )
    narrative_id = persist_narrative(conn, finding_id=finding_id, result=result)
    stored = conn.execute(
        sa.text(
            "SELECT template_text, rendered_text, validation_status, model_id, prompt_hash, "
            "actions FROM narratives WHERE id = :id"
        ),
        {"id": narrative_id},
    ).one()
    assert stored.template_text == result.template
    assert stored.rendered_text == result.rendered
    assert stored.validation_status == "valid"
    assert stored.model_id == "gpt-4o-mini"
    assert stored.prompt_hash == "abc123"
    assert stored.actions == ["flag-for-data-steward-review"]


def test_persist_narrative_stores_a_fallback_result_with_null_model_fields(
    conn: sa.Connection,
) -> None:
    finding_id = _seed_minimal_finding(conn)
    result = ComposeResult(
        template=_FALLBACK_TEMPLATE,
        rendered=_FALLBACK_TEMPLATE,
        model_id=None,
        prompt_hash=None,
        validation_status="fallback_static",
        actions=[],
    )
    narrative_id = persist_narrative(conn, finding_id=finding_id, result=result)
    stored = conn.execute(
        sa.text(
            "SELECT validation_status, model_id, prompt_hash, actions FROM narratives "
            "WHERE id = :id"
        ),
        {"id": narrative_id},
    ).one()
    assert stored.validation_status == "fallback_static"
    assert stored.model_id is None
    assert stored.prompt_hash is None
    assert stored.actions == []


def test_narratives_validation_status_rejects_an_unknown_value(conn: sa.Connection) -> None:
    finding_id = _seed_minimal_finding(conn)
    with pytest.raises(sa.exc.IntegrityError):
        conn.execute(
            sa.text(
                "INSERT INTO narratives (finding_id, template_text, rendered_text, "
                "validation_status) VALUES (:finding_id, 't', 'r', 'not_a_real_status')"
            ),
            {"finding_id": finding_id},
        )
