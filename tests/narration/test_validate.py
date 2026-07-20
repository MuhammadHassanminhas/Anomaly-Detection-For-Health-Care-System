"""Phase 5 step 3: cdss.narrate.validate -- F8's actual enforcement point,
built and tested before any narrator/LLM code exists. Pure logic, no DB
involved (same class as test_render.py/test_render_fallback.py). This is
the phase's core evidence suite (spec step 3's own deliverable text): every
adversarial smuggling attempt named in the spec must be blocked.
"""

from __future__ import annotations

import datetime as dt

import pytest

import cdss.narrate as narrate_module
from cdss.action_library import ActionDef
from cdss.narrate import (
    ValidationResult,
    Violation,
    _action_library_copy_text,
    render,
    validate,
)

_INVOICE_TEMPLATE = "Invoice {{InvoiceTransactionID}} has a negative total of {{TotalAmount}}."
_INVOICE_EVIDENCE = {"InvoiceTransactionID": "INV-1", "TotalAmount": "-12.50"}
_INVOICE_DECLARED_FIELDS = {"InvoiceTransactionID", "TotalAmount"}
_INVOICE_ACTIONS = {"flag-for-data-steward-review"}


def _rule_names(result: ValidationResult) -> set[str]:
    return {v.rule for v in result.violations}


# --- the happy path: nothing to block --------------------------------------


def test_ok_when_every_token_traces_to_evidence_params_or_is_plain_prose() -> None:
    result = validate(
        _INVOICE_TEMPLATE,
        render(_INVOICE_TEMPLATE, evidence=_INVOICE_EVIDENCE, params={}).text,
        evidence=_INVOICE_EVIDENCE,
        params={},
        declared_evidence_fields=_INVOICE_DECLARED_FIELDS,
        action_allowlist=_INVOICE_ACTIONS,
        selected_actions=["flag-for-data-steward-review"],
    )
    assert result == ValidationResult(status="ok", violations=())


# --- rule (a): every placeholder must resolve against evidence or params ---


def test_blocks_a_template_referencing_an_unknown_field() -> None:
    template = "{{not_a_real_field}} is wrong."
    result = validate(
        template,
        template,
        evidence={},
        params={},
        declared_evidence_fields=set(),
        action_allowlist=set(),
    )
    assert result.status == "blocked"
    assert _rule_names(result) == {"unresolvable_placeholder"}


def test_blocks_when_the_supplied_rendered_text_does_not_match_a_deterministic_render() -> None:
    result = validate(
        _INVOICE_TEMPLATE,
        "This text was never actually produced by rendering the template.",
        evidence=_INVOICE_EVIDENCE,
        params={},
        declared_evidence_fields=_INVOICE_DECLARED_FIELDS,
        action_allowlist=_INVOICE_ACTIONS,
    )
    assert result.status == "blocked"
    assert _rule_names(result) == {"rendered_text_mismatch"}


# --- rule (b): adversarial smuggling suite, the phase's core evidence ------


def test_blocks_a_smuggled_digit_not_present_in_evidence_or_params() -> None:
    template = (
        "Invoice {{InvoiceTransactionID}} is overdue, well past the usual 14 day grace period."
    )
    rendered = render(template, evidence=_INVOICE_EVIDENCE, params={}).text
    result = validate(
        template,
        rendered,
        evidence=_INVOICE_EVIDENCE,
        params={},
        declared_evidence_fields=_INVOICE_DECLARED_FIELDS,
        action_allowlist=set(),
    )
    assert result.status == "blocked"
    assert any(v.rule == "unallowlisted_token" and "14" in v.detail for v in result.violations)


def test_blocks_a_date_paraphrase_of_the_real_evidence_date() -> None:
    evidence = {**_INVOICE_EVIDENCE, "InvoiceDate": dt.date(2026, 3, 3)}
    template = "Invoice {{InvoiceTransactionID}} was dated March 3rd, matching {{InvoiceDate}}."
    rendered = render(template, evidence=evidence, params={}).text
    result = validate(
        template,
        rendered,
        evidence=evidence,
        params={},
        declared_evidence_fields=_INVOICE_DECLARED_FIELDS | {"InvoiceDate"},
        action_allowlist=set(),
    )
    assert result.status == "blocked"
    assert any(
        v.rule == "unallowlisted_token" and "March 3rd" in v.detail for v in result.violations
    )
    # the real, placeholder-sourced ISO date must NOT itself be flagged
    assert not any("2026-03-03" in v.detail for v in result.violations)


def test_blocks_an_invented_diagnosis_style_code() -> None:
    template = "Invoice {{InvoiceTransactionID}} is billed under code E11.9."
    rendered = render(template, evidence=_INVOICE_EVIDENCE, params={}).text
    result = validate(
        template,
        rendered,
        evidence=_INVOICE_EVIDENCE,
        params={},
        declared_evidence_fields=_INVOICE_DECLARED_FIELDS,
        action_allowlist=set(),
    )
    assert result.status == "blocked"
    assert any(v.rule == "unallowlisted_token" and "E11.9" in v.detail for v in result.violations)


# --- rule (c): selected actions must be on the check's own allowlist -------


def test_blocks_an_action_off_the_checks_allowlist() -> None:
    rendered = render(_INVOICE_TEMPLATE, evidence=_INVOICE_EVIDENCE, params={}).text
    result = validate(
        _INVOICE_TEMPLATE,
        rendered,
        evidence=_INVOICE_EVIDENCE,
        params={},
        declared_evidence_fields=_INVOICE_DECLARED_FIELDS,
        action_allowlist=_INVOICE_ACTIONS,
        selected_actions=["flag-for-data-steward-review", "prescribe-medication"],
    )
    assert result.status == "blocked"
    assert any(
        v.rule == "action_not_allowlisted" and "prescribe-medication" in v.detail
        for v in result.violations
    )


# --- rule (d): evidence exfiltration ----------------------------------------


def test_blocks_an_evidence_field_outside_the_checks_declared_set() -> None:
    evidence = {**_INVOICE_EVIDENCE, "PatientNHI": "ABC1234"}
    template = _INVOICE_TEMPLATE + " Patient NHI on file: {{PatientNHI}}."
    rendered = render(template, evidence=evidence, params={}).text
    result = validate(
        template,
        rendered,
        evidence=evidence,
        params={},
        declared_evidence_fields=_INVOICE_DECLARED_FIELDS,  # PatientNHI deliberately absent
        action_allowlist=set(),
    )
    assert result.status == "blocked"
    assert any(
        v.rule == "undeclared_evidence_field" and "PatientNHI" in v.detail
        for v in result.violations
    )


# --- the pressure valve: a reviewed static phrase is exempt -----------------


def test_static_vocabulary_exempts_a_reviewed_phrase() -> None:
    template = "Invoice {{InvoiceTransactionID}} relates to the COVID-19 response fund."
    rendered = render(template, evidence=_INVOICE_EVIDENCE, params={}).text
    result = validate(
        template,
        rendered,
        evidence=_INVOICE_EVIDENCE,
        params={},
        declared_evidence_fields=_INVOICE_DECLARED_FIELDS,
        action_allowlist=set(),
        static_vocabulary={"COVID-19"},
    )
    assert result.status == "ok"


def test_without_the_static_vocabulary_entry_the_same_phrase_is_blocked() -> None:
    template = "Invoice {{InvoiceTransactionID}} relates to the COVID-19 response fund."
    rendered = render(template, evidence=_INVOICE_EVIDENCE, params={}).text
    result = validate(
        template,
        rendered,
        evidence=_INVOICE_EVIDENCE,
        params={},
        declared_evidence_fields=_INVOICE_DECLARED_FIELDS,
        action_allowlist=set(),
    )
    assert result.status == "blocked"
    assert any(
        v.rule == "unallowlisted_token" and "COVID-19" in v.detail for v in result.violations
    )


# --- the action-library-copy exemption's data source, tested directly ------
# (CURATED_ACTIONS' own title/description text has no digit/code-like tokens
# today, so the exemption's live containment path has nothing to exercise
# end-to-end without fabricating a fake action definition; this proves the
# lookup itself is correct and scoped to only the allowlisted codes.)


def test_action_library_copy_text_is_scoped_to_the_given_allowlist() -> None:
    copy_text = _action_library_copy_text({"book-recall"})
    assert copy_text == "Book a recall Schedule a follow-up appointment for the patient."
    assert "invoice" not in copy_text.lower()


def test_a_token_appearing_in_the_allowlisted_actions_own_fixed_copy_is_exempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # None of the real CURATED_ACTIONS carry a digit/code-like token today,
    # so this path has nothing to exercise against the real registry.
    # Monkeypatching a synthetic entry (restored automatically after the
    # test) proves the exemption itself works, without claiming any
    # fabricated data as real.
    fake_action = ActionDef(
        code="raise-priority-1-task", title="Raise a P1 task", description="Escalate immediately."
    )
    monkeypatch.setattr(narrate_module, "CURATED_ACTIONS", (fake_action,))

    template = "Invoice {{InvoiceTransactionID}} needs a P1 response."
    rendered = render(template, evidence=_INVOICE_EVIDENCE, params={}).text
    result = validate(
        template,
        rendered,
        evidence=_INVOICE_EVIDENCE,
        params={},
        declared_evidence_fields=_INVOICE_DECLARED_FIELDS,
        action_allowlist={"raise-priority-1-task"},
    )
    assert result.status == "ok"


# --- multiple independent violations are all reported, not just the first --


def test_reports_every_violation_not_just_the_first() -> None:
    evidence = {**_INVOICE_EVIDENCE, "PatientNHI": "ABC1234"}
    template = (
        "Invoice {{InvoiceTransactionID}} is 14 days overdue. Patient NHI on file: {{PatientNHI}}."
    )
    rendered = render(template, evidence=evidence, params={}).text
    result = validate(
        template,
        rendered,
        evidence=evidence,
        params={},
        declared_evidence_fields=_INVOICE_DECLARED_FIELDS,
        action_allowlist={"flag-for-data-steward-review"},
        selected_actions=["prescribe-medication"],
    )
    assert result.status == "blocked"
    assert _rule_names(result) == {
        "unallowlisted_token",
        "undeclared_evidence_field",
        "action_not_allowlisted",
    }


def test_violation_is_a_plain_comparable_dataclass() -> None:
    v1 = Violation(rule="action_not_allowlisted", detail="x")
    v2 = Violation(rule="action_not_allowlisted", detail="x")
    assert v1 == v2
