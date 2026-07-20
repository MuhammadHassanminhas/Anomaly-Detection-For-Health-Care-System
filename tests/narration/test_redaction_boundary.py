"""Phase 5 step 6: the redaction-boundary proof. Combined with step 3's own
adversarial validator suite, this is the constraint-5 evidence package for
D-003 sign-off (still OPEN in DECISIONS.md -- this suite is evidence for
that decision, not the decision itself). Records every real outbound LLM
payload and proves, empirically against realistic-looking sensitive data,
that none of it -- no evidence value, no entity key, no live identifier
pattern -- ever left the process under Tier S.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import re
from pathlib import Path

from cdss.narrate import (
    ComposeResult,
    JsonlPromptRecorder,
    RecordedPrompt,
    TemplateCache,
    compose,
)

# A real-shaped NZ NHI is 3 letters + 4 digits (older format). Synthetic --
# not a real patient's identifier.
_NHI_PATTERN = re.compile(r"\b[A-Z]{3}\d{4}\b")

# Synthetic names, clearly not real people (CLAUDE.md: synthetic data is
# always labeled synthetic) -- stand-ins for "names from fixture data".
_FIXTURE_NAMES = ("Hine Ngata", "Wiremu Tane", "Aroha Ngata")

_DEFINITION = {
    "id": "patient-overdue-review",
    "category": "care-gap",
    "evidence": ["AppointmentID", "PatientNHI", "PatientName", "TotalAmount", "InvoiceDate"],
    "actions": ["flag-for-clinician-review"],
    "resolution": "The patient is reviewed, or the finding is dismissed with a reason.",
    "params": {},
}
_FALLBACK_TEMPLATE = "This check has flagged a record for manual review."

_ADVERSARIAL_EVIDENCE = {
    "AppointmentID": 918273,  # the finding's own entity key
    "PatientNHI": "ABC1234",
    "PatientName": _FIXTURE_NAMES[0],
    "TotalAmount": decimal.Decimal("-12.50"),
    "InvoiceDate": dt.date(2026, 3, 3),
}

_VALID_RESPONSE = json.dumps(
    {
        "template": (
            "Appointment {{AppointmentID}} for {{PatientName}} (NHI {{PatientNHI}}) has an "
            "outstanding balance of {{TotalAmount}} as of {{InvoiceDate}}."
        ),
        "actions": ["flag-for-clinician-review"],
    }
)


class FakeLLMClient:
    def __init__(self, response: str) -> None:
        self._response = response
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._response


class FakePromptRecorder:
    def __init__(self) -> None:
        self.recorded: list[RecordedPrompt] = []

    def record(self, recorded: RecordedPrompt) -> None:
        self.recorded.append(recorded)


def _compose_with_recorder(
    client: FakeLLMClient, recorder: FakePromptRecorder, **overrides: object
) -> ComposeResult:
    kwargs: dict[str, object] = {
        "model_id": "gpt-4o-mini",
        "check_version_id": "check-version-boundary-test",
        "definition": _DEFINITION,
        "rationale": "Overdue patients should be reviewed promptly.",
        "fallback_template": _FALLBACK_TEMPLATE,
        "evidence": _ADVERSARIAL_EVIDENCE,
        "params": {},
        "env": {"CDSS_ENV": "test"},
        "recorder": recorder,
    }
    kwargs.update(overrides)
    return compose(client, **kwargs)  # type: ignore[arg-type]


def test_recorded_prompt_never_contains_an_evidence_value() -> None:
    client = FakeLLMClient(_VALID_RESPONSE)
    recorder = FakePromptRecorder()
    result = _compose_with_recorder(client, recorder)
    assert result.validation_status == "valid"
    assert len(recorder.recorded) == 1
    prompt = recorder.recorded[0].prompt

    assert "918273" not in prompt  # the entity key
    assert "ABC1234" not in prompt  # the NHI value
    assert "-12.50" not in prompt  # the dollar amount
    assert "2026-03-03" not in prompt  # the ISO date
    for name in _FIXTURE_NAMES:
        assert name not in prompt


def test_recorded_prompt_never_matches_a_live_nhi_pattern() -> None:
    client = FakeLLMClient(_VALID_RESPONSE)
    recorder = FakePromptRecorder()
    _compose_with_recorder(client, recorder)
    prompt = recorder.recorded[0].prompt
    assert _NHI_PATTERN.search(prompt) is None


def test_recorded_prompt_carries_field_names_and_types_only() -> None:
    client = FakeLLMClient(_VALID_RESPONSE)
    recorder = FakePromptRecorder()
    _compose_with_recorder(client, recorder)
    prompt = recorder.recorded[0].prompt
    for field_name in _ADVERSARIAL_EVIDENCE:
        assert field_name in prompt  # names are metadata, not PHI (D-004)
    assert '"type": "integer"' in prompt
    assert '"type": "string"' in prompt
    assert '"type": "decimal"' in prompt
    assert '"type": "date"' in prompt


def test_recorder_is_never_called_again_on_a_cache_hit() -> None:
    client = FakeLLMClient(_VALID_RESPONSE)
    recorder = FakePromptRecorder()
    cache = TemplateCache()
    _compose_with_recorder(client, recorder, cache=cache)
    assert len(recorder.recorded) == 1

    # a second finding, same check version + evidence shape -> cache hit,
    # no new prompt is ever built or sent, so nothing new is recorded
    second_evidence = {**_ADVERSARIAL_EVIDENCE, "AppointmentID": 111111, "PatientNHI": "XYZ9999"}
    result = _compose_with_recorder(client, recorder, cache=cache, evidence=second_evidence)
    assert result.validation_status == "valid"
    assert client.prompts == [recorder.recorded[0].prompt]  # only ever sent once
    assert len(recorder.recorded) == 1


def test_recorder_is_not_invoked_when_none_is_supplied() -> None:
    # the default (no recorder) -- proves recording is opt-in, never a
    # requirement to compose a narrative at all
    client = FakeLLMClient(_VALID_RESPONSE)
    result = compose(
        client,
        model_id="gpt-4o-mini",
        check_version_id="check-version-boundary-test",
        definition=_DEFINITION,
        rationale="Overdue patients should be reviewed promptly.",
        fallback_template=_FALLBACK_TEMPLATE,
        evidence=_ADVERSARIAL_EVIDENCE,
        params={},
        env={"CDSS_ENV": "test"},
    )
    assert result.validation_status == "valid"


# --- JsonlPromptRecorder: the real dev/test-mode sink ------------------------


def test_jsonl_prompt_recorder_writes_one_line_per_call(tmp_path: Path) -> None:
    fixed_time = dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.UTC)
    recorder = JsonlPromptRecorder(audit_dir=tmp_path, clock=lambda: fixed_time)
    recorder.record(
        RecordedPrompt(
            check_version_id="v1",
            model_id="gpt-4o-mini",
            prompt="redacted prompt text",
            prompt_hash="abc123",
            recorded_at=fixed_time.isoformat(),
        )
    )
    recorder.record(
        RecordedPrompt(
            check_version_id="v2",
            model_id="gpt-4o-mini",
            prompt="another redacted prompt",
            prompt_hash="def456",
            recorded_at=fixed_time.isoformat(),
        )
    )

    path = tmp_path / "prompt-audit-2026-07-20.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["check_version_id"] == "v1"
    assert first["prompt_hash"] == "abc123"
    second = json.loads(lines[1])
    assert second["check_version_id"] == "v2"


def test_jsonl_prompt_recorder_composed_end_to_end_writes_a_redacted_line(
    tmp_path: Path,
) -> None:
    client = FakeLLMClient(_VALID_RESPONSE)
    recorder = JsonlPromptRecorder(audit_dir=tmp_path)
    result = compose(
        client,
        model_id="gpt-4o-mini",
        check_version_id="check-version-boundary-test",
        definition=_DEFINITION,
        rationale="Overdue patients should be reviewed promptly.",
        fallback_template=_FALLBACK_TEMPLATE,
        evidence=_ADVERSARIAL_EVIDENCE,
        params={},
        env={"CDSS_ENV": "test"},
        recorder=recorder,
    )
    assert result.validation_status == "valid"

    files = list(tmp_path.glob("prompt-audit-*.jsonl"))
    assert len(files) == 1
    line = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
    assert "918273" not in line["prompt"]
    assert "ABC1234" not in line["prompt"]
