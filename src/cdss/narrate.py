"""Phase 5 steps 1-4: the deterministic narration floor (F8), the typed
placeholder renderer, the validator (F8's actual enforcement point, built
and tested before any narrator/LLM code existed, per the phase spec's own
step ordering), and the Tier S narration pipeline (`compose`) that ties
them together with a real LLM call. A finding is never delayed or lost to
narration: any LLM-side failure (garbage output, a smuggled value, an
outage) falls back to the check's own deterministic `fallback_template`
(step 1) rather than propagating. The template cache and the
redaction-boundary proof are step 5+'s job
(`docs/phases/phase-05-explanation-layer.md`).
"""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import json
import re
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from os import environ
from typing import Any, Literal

import sqlalchemy as sa

from cdss.action_library import CURATED_ACTIONS
from cdss.authoring.llm_draft import LLMClient

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")

_MONTH_NAMES = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
    "jan",
    "feb",
    "mar",
    "apr",
    "jun",
    "jul",
    "aug",
    "sep",
    "sept",
    "oct",
    "nov",
    "dec",
)
_MONTH_ALTERNATION = "|".join(_MONTH_NAMES)

# Multi-word date paraphrases ("March 3rd", "3rd of March", "1 March 2026")
# plus literal ISO/slash dates typed directly into static prose -- scanned
# first and their spans marked "consumed" so the plain word-token pass below
# doesn't also flag their component digits separately.
_DATE_LIKE = re.compile(
    rf"\b(?:{_MONTH_ALTERNATION})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s*\d{{4}})?\b"
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{_MONTH_ALTERNATION})\.?(?:,?\s*\d{{4}})?\b"
    rf"|\b\d{{4}}-\d{{2}}-\d{{2}}(?:[T ]\d{{2}}:\d{{2}}(?::\d{{2}})?)?\b"
    rf"|\b\d{{1,2}}/\d{{1,2}}/\d{{2,4}}\b",
    re.IGNORECASE,
)

# A "word" for numeric/code classification: an alnum run that may contain
# internal hyphens/dots/commas (so "INV-42", "E11.9", "1,234.56" are each one
# token, not several). Classified afterward by digit/letter content --
# uniformly broad on purpose (docs/phases/phase-05-explanation-layer.md's own
# risk note: the classifier errs broad, blocking more; the static vocabulary
# is the pressure valve, never a silent widening of the classifier itself).
_WORD_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[-.,][A-Za-z0-9]+)*")

TokenKind = Literal["date_like", "numeric", "code_like"]


def _classify_word_token(token: str) -> TokenKind | None:
    has_digit = any(c.isdigit() for c in token)
    has_alpha = any(c.isalpha() for c in token)
    if not has_digit:
        return None
    return "code_like" if has_alpha else "numeric"


def _find_suspect_tokens(text: str) -> list[tuple[int, int, TokenKind, str]]:
    spans: list[tuple[int, int, TokenKind, str]] = []
    consumed: list[tuple[int, int]] = []
    for match in _DATE_LIKE.finditer(text):
        spans.append((match.start(), match.end(), "date_like", match.group()))
        consumed.append((match.start(), match.end()))

    def _already_consumed(start: int, end: int) -> bool:
        return any(cs <= start and end <= ce for cs, ce in consumed)

    for match in _WORD_TOKEN.finditer(text):
        if _already_consumed(match.start(), match.end()):
            continue
        kind = _classify_word_token(match.group())
        if kind is not None:
            spans.append((match.start(), match.end(), kind, match.group()))

    spans.sort(key=lambda s: s[0])
    return spans


class UnknownPlaceholderError(ValueError):
    """Raised when a template references a field that is in neither the
    finding's evidence nor the check's params -- a broken template must fail
    loudly here, never render as blank or literal `{{field}}` text."""

    def __init__(self, field: str) -> None:
        super().__init__(f"template references unknown field '{field}' (not in evidence or params)")
        self.field = field


@dataclass(frozen=True)
class ProvenanceEntry:
    """One resolved placeholder: which field it came from, whether that
    field was found in the finding's evidence or the check's params, and the
    `[start, end)` character span it occupies in the *rendered* text -- so a
    validator can walk the rendered text back to the source that produced
    each span."""

    placeholder: str
    source: Literal["evidence", "params"]
    start: int
    end: int


@dataclass(frozen=True)
class RenderResult:
    text: str
    provenance: list[ProvenanceEntry]


def _format_value(value: Any) -> str:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return format(value, "f")
    if isinstance(value, float):
        # Route through Decimal(str(...)) rather than format(value, "f")
        # directly: format() on a float defaults to 6 fractional digits
        # (padding "5.0" to "5.000000"), which isn't this value's own
        # precision. str() already gives the shortest round-tripping
        # decimal digits; Decimal(...) + "f" only strips the scientific
        # notation str() falls back to at extreme magnitudes.
        return format(decimal.Decimal(str(value)), "f")
    return str(value)


def render_fallback(
    template: str, *, evidence: Mapping[str, Any], params: Mapping[str, Any]
) -> str:
    """Interpolate every `{{field}}` in `template` from evidence, falling back
    to params on a miss (evidence wins on a name collision -- it's the
    finding's own actual data; params are just the check's configured
    thresholds)."""

    return render(template, evidence=evidence, params=params).text


def render(
    template: str, *, evidence: Mapping[str, Any], params: Mapping[str, Any]
) -> RenderResult:
    """Like `render_fallback`, but with typed, locale-fixed formatting
    (dates ISO-8601, decimals fixed notation -- never scientific) and a
    provenance map recording, for every resolved placeholder, which field
    it came from and the exact span it rendered into -- so every span in
    the output text traces back to a named source field, never invented
    text."""

    parts: list[str] = []
    provenance: list[ProvenanceEntry] = []
    template_pos = 0
    output_pos = 0
    for match in _PLACEHOLDER.finditer(template):
        literal_span = template[template_pos : match.start()]
        parts.append(literal_span)
        output_pos += len(literal_span)

        field = match.group(1)
        if field in evidence:
            source: Literal["evidence", "params"] = "evidence"
            formatted = _format_value(evidence[field])
        elif field in params:
            source = "params"
            formatted = _format_value(params[field])
        else:
            raise UnknownPlaceholderError(field)

        parts.append(formatted)
        start = output_pos
        output_pos += len(formatted)
        provenance.append(
            ProvenanceEntry(placeholder=field, source=source, start=start, end=output_pos)
        )
        template_pos = match.end()

    parts.append(template[template_pos:])
    return RenderResult(text="".join(parts), provenance=provenance)


ViolationRule = Literal[
    "unresolvable_placeholder",
    "rendered_text_mismatch",
    "undeclared_evidence_field",
    "action_not_allowlisted",
    "unallowlisted_token",
]


@dataclass(frozen=True)
class Violation:
    """One machine-readable enforcement failure -- `rule` names which of
    F8's four checks tripped, `detail` is a human-readable explanation for
    a reviewer looking at a blocked narrative."""

    rule: ViolationRule
    detail: str


@dataclass(frozen=True)
class ValidationResult:
    status: Literal["ok", "blocked"]
    violations: tuple[Violation, ...]


def _within_any_span(start: int, end: int, provenance: Sequence[ProvenanceEntry]) -> bool:
    return any(entry.start <= start and end <= entry.end for entry in provenance)


def _action_library_copy_text(action_allowlist: Set[str]) -> str:
    return " ".join(
        f"{action.title} {action.description}"
        for action in CURATED_ACTIONS
        if action.code in action_allowlist
    )


def validate(
    template: str,
    rendered: str,
    *,
    evidence: Mapping[str, Any],
    params: Mapping[str, Any],
    declared_evidence_fields: Set[str],
    action_allowlist: Set[str],
    selected_actions: Sequence[str] = (),
    static_vocabulary: Set[str] = frozenset(),
) -> ValidationResult:
    """F8's enforcement point: a rendered narrative is `blocked` unless (a)
    every template placeholder resolves against evidence/params, (b) every
    numeric/date-like/code-like token in the rendered text lies inside an
    interpolated span, the action library's own fixed copy, or the
    checked-in static vocabulary, (c) every selected action is on the
    check's own allowlist, and (d) no evidence field outside the check's
    declared set was interpolated -- even one that legitimately resolved
    against the `evidence` mapping passed in. Independently re-derives the
    expected render from `template`/`evidence`/`params` rather than trusting
    the caller's `rendered` text is what it claims to be; any mismatch is
    itself a violation, never silently accepted."""

    try:
        expected = render(template, evidence=evidence, params=params)
    except UnknownPlaceholderError as exc:
        return ValidationResult(
            status="blocked",
            violations=(Violation("unresolvable_placeholder", str(exc)),),
        )

    if rendered != expected.text:
        return ValidationResult(
            status="blocked",
            violations=(
                Violation(
                    "rendered_text_mismatch",
                    "the supplied rendered text does not match a deterministic "
                    "render of this template against this evidence and params",
                ),
            ),
        )

    violations: list[Violation] = []

    for entry in expected.provenance:
        if entry.source == "evidence" and entry.placeholder not in declared_evidence_fields:
            violations.append(
                Violation(
                    "undeclared_evidence_field",
                    f"placeholder '{{{{{entry.placeholder}}}}}' resolved from evidence, but "
                    f"'{entry.placeholder}' is not in the check's declared evidence fields",
                )
            )

    for action in selected_actions:
        if action not in action_allowlist:
            violations.append(
                Violation(
                    "action_not_allowlisted",
                    f"selected action '{action}' is not in the check's action allowlist",
                )
            )

    allowlisted_copy = _action_library_copy_text(action_allowlist)
    for start, end, kind, token in _find_suspect_tokens(rendered):
        if _within_any_span(start, end, expected.provenance):
            continue
        if token in static_vocabulary:
            continue
        if token in allowlisted_copy:
            continue
        violations.append(
            Violation(
                "unallowlisted_token",
                f"{kind} token '{token}' at [{start}, {end}) is not traceable to an "
                "interpolated evidence/param span, the action library's fixed copy, "
                "or the approved static vocabulary",
            )
        )

    if violations:
        return ValidationResult(status="blocked", violations=tuple(violations))
    return ValidationResult(status="ok", violations=())


# --- step 4: Tier S narration pipeline (F8/D-003/D-004) ---------------------

RedactionMode = Literal["tier_s", "off"]

_REDACTION_MODES: frozenset[str] = frozenset({"tier_s", "off"})


class RedactionOffInProductionError(RuntimeError):
    """`CDSS_REDACTION_MODE=off` was requested while `CDSS_ENV=production`
    -- or `CDSS_ENV` is unset at all, since production is the fail-closed
    default (a deployment that forgot to set it is treated as production,
    never as implicitly safe to de-redact)."""

    def __init__(self) -> None:
        super().__init__(
            "CDSS_REDACTION_MODE=off is not permitted when CDSS_ENV=production "
            "(or CDSS_ENV is unset -- production is the fail-closed default)"
        )


def resolve_redaction_mode(env: Mapping[str, str] | None = None) -> RedactionMode:
    """`CDSS_REDACTION_MODE` defaults to `tier_s` (the only mode that never
    lets a real evidence value leave the process). `CDSS_ENV` defaults to
    `production` -- fail closed, so an environment that never declared
    itself non-production cannot silently run with redaction off. Raising
    here is a deployment-configuration failure, not a per-finding one, so
    unlike `compose`'s own LLM-failure handling this is never caught by a
    fallback path -- it should stop startup, not narrate quietly wrong."""

    source = env if env is not None else environ
    mode_raw = source.get("CDSS_REDACTION_MODE", "tier_s").strip().lower()
    if mode_raw not in _REDACTION_MODES:
        raise ValueError(
            f"CDSS_REDACTION_MODE must be one of {sorted(_REDACTION_MODES)}, got {mode_raw!r}"
        )
    deployment_env = source.get("CDSS_ENV", "production").strip().lower()
    if mode_raw == "off" and deployment_env == "production":
        raise RedactionOffInProductionError()
    return "tier_s" if mode_raw == "tier_s" else "off"


def _evidence_type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dt.datetime):
        return "datetime"
    if isinstance(value, dt.date):
        return "date"
    if isinstance(value, (decimal.Decimal, float)):
        return "decimal"
    if isinstance(value, int):
        return "integer"
    if value is None:
        return "null"
    return "string"


@dataclass(frozen=True)
class NarrationContext:
    rationale: str
    category: str
    resolution: str
    evidence_fields: list[dict[str, str]]
    param_fields: list[dict[str, str]]
    allowed_actions: list[str]


def build_narration_context(
    *,
    rationale: str,
    category: str,
    resolution: str,
    evidence: Mapping[str, Any],
    params: Mapping[str, Any],
    param_types: Mapping[str, str],
    action_allowlist: Set[str],
    mode: RedactionMode,
) -> NarrationContext:
    """Tier S (`mode="tier_s"`, the only mode a production build can select
    -- `resolve_redaction_mode` enforces that): every evidence/param field
    is reduced to its **name and type only**. Fabrication is structurally
    impossible from this alone -- there is no value here for the LLM to
    smuggle back. `mode="off"` additionally includes each field's own
    rendered value, for local debugging only, never in production."""

    def _field(name: str, type_name: str, value: Any) -> dict[str, str]:
        field = {"name": name, "type": type_name}
        if mode == "off":
            field["value"] = _format_value(value)
        return field

    evidence_fields = [
        _field(name, _evidence_type_name(value), value) for name, value in evidence.items()
    ]
    param_fields = [
        _field(name, param_types.get(name, _evidence_type_name(value)), value)
        for name, value in params.items()
    ]
    return NarrationContext(
        rationale=rationale,
        category=category,
        resolution=resolution,
        evidence_fields=evidence_fields,
        param_fields=param_fields,
        allowed_actions=sorted(action_allowlist),
    )


def build_narration_prompt(context: NarrationContext) -> str:
    return (
        "You are writing a short staff-facing narrative template for a data anomaly "
        "finding.\n\n"
        f"Category: {context.category}\n"
        f"Why this check exists: {context.rationale}\n"
        f"Resolution guidance: {context.resolution}\n\n"
        "You do NOT know the real values of this finding -- only these field names and "
        "types (never invent a value; every fact must come from a placeholder):\n"
        f"Evidence fields: {json.dumps(context.evidence_fields)}\n"
        f"Check parameters: {json.dumps(context.param_fields)}\n\n"
        "Write a template using `{{field_name}}` placeholders (only names listed above) "
        "for every fact. Never write a literal number, date, or code in the template text "
        "-- if it is a fact, it must be a placeholder.\n\n"
        f"Select zero or more actions from exactly this allowlist: {context.allowed_actions}\n\n"
        'Return a single JSON object, nothing else: {"template": "...", "actions": ["..."]}'
    )


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if stripped.endswith("```"):
            stripped = stripped[: -len("```")]
    return stripped.strip()


def parse_narration_response(text: str) -> tuple[str, list[str]]:
    parsed = json.loads(_strip_code_fences(text))
    if not isinstance(parsed, dict):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    template = parsed.get("template")
    if not isinstance(template, str):
        raise ValueError("response missing a string 'template' field")
    actions = parsed.get("actions", [])
    if not isinstance(actions, list) or not all(isinstance(a, str) for a in actions):
        raise ValueError("response 'actions' must be a list of strings")
    return template, actions


@dataclass(frozen=True)
class ComposeResult:
    template: str
    rendered: str
    model_id: str | None
    prompt_hash: str | None
    validation_status: Literal["valid", "blocked_fallback", "fallback_static"]
    actions: list[str]


def compose(
    client: LLMClient,
    *,
    model_id: str,
    definition: Mapping[str, Any],
    rationale: str,
    fallback_template: str,
    evidence: Mapping[str, Any],
    params: Mapping[str, Any],
    static_vocabulary: Set[str] = frozenset(),
    env: Mapping[str, str] | None = None,
) -> ComposeResult:
    """F8's runtime pipeline: compose a prompt from redacted context, call
    the LLM, render its template against the real evidence/params, then
    validate the render (step 3) -- with a deterministic fallback (step 1)
    on any failure, so a finding is never delayed or lost to narration.
    `validation_status` distinguishes *why* a fallback happened:
    `fallback_static` for an LLM-side failure (garbage output, malformed
    JSON, a timeout/outage -- nothing to validate), `blocked_fallback` for
    a real response that the renderer or validator rejected."""

    declared_evidence_fields = set(definition["evidence"])
    action_allowlist = set(definition["actions"])
    param_types = {name: spec["type"] for name, spec in definition.get("params", {}).items()}

    def _fallback(status: Literal["blocked_fallback", "fallback_static"]) -> ComposeResult:
        rendered = render_fallback(fallback_template, evidence=evidence, params=params)
        return ComposeResult(
            template=fallback_template,
            rendered=rendered,
            model_id=None,
            prompt_hash=None,
            validation_status=status,
            actions=[],
        )

    mode = resolve_redaction_mode(env)
    context = build_narration_context(
        rationale=rationale,
        category=definition["category"],
        resolution=definition.get("resolution", ""),
        evidence=evidence,
        params=params,
        param_types=param_types,
        action_allowlist=action_allowlist,
        mode=mode,
    )
    prompt = build_narration_prompt(context)
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    try:
        # The one deliberately broad catch in this module: garbage output,
        # malformed JSON, a network timeout, an outage -- every LLM-side
        # failure is handled identically (fall back), because a finding
        # must never be delayed or lost waiting on narration.
        raw_response = client.complete(prompt)
        template, selected_actions = parse_narration_response(raw_response)
    except Exception:
        return _fallback("fallback_static")

    try:
        rendered = render(template, evidence=evidence, params=params).text
    except UnknownPlaceholderError:
        return _fallback("blocked_fallback")

    result = validate(
        template,
        rendered,
        evidence=evidence,
        params=params,
        declared_evidence_fields=declared_evidence_fields,
        action_allowlist=action_allowlist,
        selected_actions=selected_actions,
        static_vocabulary=static_vocabulary,
    )
    if result.status == "blocked":
        return _fallback("blocked_fallback")

    return ComposeResult(
        template=template,
        rendered=rendered,
        model_id=model_id,
        prompt_hash=prompt_hash,
        validation_status="valid",
        actions=list(selected_actions),
    )


_INSERT_NARRATIVE = sa.text(
    "INSERT INTO narratives "
    "(finding_id, template_text, rendered_text, validation_status, model_id, prompt_hash, "
    "actions) "
    "VALUES (:finding_id, :template_text, :rendered_text, :validation_status, :model_id, "
    ":prompt_hash, CAST(:actions AS jsonb)) "
    "RETURNING id"
)


def persist_narrative(conn: sa.Connection, *, finding_id: str, result: ComposeResult) -> str:
    """Stores one `compose` result as a `narratives` row -- `model_id`/
    `prompt_hash` are `NULL` on any fallback path (no model was actually
    consulted to produce the rendered text)."""
    row = conn.execute(
        _INSERT_NARRATIVE,
        {
            "finding_id": finding_id,
            "template_text": result.template,
            "rendered_text": result.rendered,
            "validation_status": result.validation_status,
            "model_id": result.model_id,
            "prompt_hash": result.prompt_hash,
            "actions": json.dumps(result.actions),
        },
    ).one()
    return str(row.id)


__all__ = [
    "ComposeResult",
    "NarrationContext",
    "ProvenanceEntry",
    "RedactionMode",
    "RedactionOffInProductionError",
    "RenderResult",
    "TokenKind",
    "UnknownPlaceholderError",
    "ValidationResult",
    "Violation",
    "ViolationRule",
    "build_narration_context",
    "build_narration_prompt",
    "compose",
    "parse_narration_response",
    "persist_narrative",
    "render",
    "render_fallback",
    "resolve_redaction_mode",
    "validate",
]
