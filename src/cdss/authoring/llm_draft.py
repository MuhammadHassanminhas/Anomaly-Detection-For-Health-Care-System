"""Phase 4 step 4: cdss.authoring.llm_draft -- the LLM-drafted check harness
(F3b). D-004 (2026-07-17): OpenAI, model from `OPENAI_MODEL` (configured
`.env` value: `gpt-4o-mini`), superseding the original Anthropic
recommendation -- provider choice does not change the Tier M redaction
boundary this module enforces.

**Tier M boundary, enforced by construction, not by trust**: `build_catalog_context`
only ever emits `min_value`/`max_value`/`top_values`/`reference_samples`/
`value_pattern_stats` for a column when its own recorded `column_class` is
NOT `identifier_or_freetext` -- the same class D-020/D-022 already gate real
value capture behind at profiling time. In the real catalog this is already
a no-op (an identifier-classified column's value fields are already null/
empty by construction -- confirmed against `semantic-catalog-v3.json`'s own
`dbo.Patient` columns), but this module re-checks `column_class` itself
rather than trusting that invariant blindly, so a future profiling-stage
regression can't silently leak a value into an LLM prompt. Column *names*
and aggregate stats (null_rate, distinct_count, row_count, relationship
containment/orphan counts) are not PHI and are always included, matching
D-004's own "column names/statistics, no patient rows" framing.

**Validate-or-repair, exactly once per draft (spec text)**: every candidate
document from the LLM is run through the same `check_doc_from_dict` +
`validate_check_against_catalog` gate `cdss.authoring.derive` uses --
Phase 2's real F2 validator, not a separate hand-rolled check. A document
that fails gets exactly one repair round-trip (the validation error fed
back to the same client); a document that still fails is dropped, never
silently coerced into something schema-valid but wrong.

**D-025 scope note**: the phase spec's own "target drafts" list (lab-ordered-
no-result, immunisation-schedule-gap, inbox-unactioned-aging, claim-no-
payment) names views D-025 already dropped from scope (Immunisation, labs,
inbox, claims -- only `dbo.Disease`/`dbo.Patient`/`dbo.Appointments`/
`fqb.Invoices` remain). Those names were illustrative ("...and peers"), not
literal requirements; this harness drafts whatever workflow-integrity/
care-gap checks are actually derivable from the real 4-view catalog, and
does not fabricate checks against views that no longer exist.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from os import environ
from pathlib import Path
from typing import Any, Protocol

import sqlalchemy as sa
from openai import OpenAI

from cdss.app_db import load_app_db_url
from cdss.catalog import next_catalog_version
from cdss.dsl import (
    SCHEMA_PATH,
    CatalogIndex,
    CheckReferenceError,
    CheckValidationError,
    check_doc_from_dict,
    collect_joined_views,
    validate_check_against_catalog,
)

CATEGORY_BRIEFS: dict[str, str] = {
    # Keys are the DSL's own `category` enum values (check-dsl.schema.json),
    # not the phase spec's prose labels -- "workflow-integrity" in the spec
    # text means the DSL's plain "workflow" category, not a literal string.
    "workflow": (
        "A workflow check flags a record stuck at an intermediate step of an operational "
        "process it should have moved past -- e.g. an appointment marked completed with no "
        "corresponding invoice, or an activity left in a state that implies unfinished staff "
        "follow-up."
    ),
    "care-gap": (
        "A care-gap check flags a patient who is overdue for something clinical "
        "operations should have already triggered -- e.g. no recent appointment for a "
        "high-needs patient, or a recall window that has lapsed."
    ),
}

_MAX_REFERENCE_SAMPLES = 20
_MAX_TOP_VALUES = 20


class MissingLLMConfigError(RuntimeError):
    """OPENAI_API_KEY or OPENAI_MODEL is not set."""


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    model: str


def load_llm_config(env: Mapping[str, str] | None = None) -> LLMConfig:
    """Reads `OPENAI_API_KEY`/`OPENAI_MODEL` (D-004). Raises
    MissingLLMConfigError naming the variable, never a value -- mirrors
    `cdss.app_db.load_app_db_url`'s existing pattern."""
    source = env if env is not None else environ
    api_key = source.get("OPENAI_API_KEY")
    if not api_key:
        raise MissingLLMConfigError("Missing required environment variable: OPENAI_API_KEY")
    model = source.get("OPENAI_MODEL")
    if not model:
        raise MissingLLMConfigError("Missing required environment variable: OPENAI_MODEL")
    return LLMConfig(api_key=api_key, model=model)


# --- Tier M catalog context (redaction boundary) -----------------------------


def _redacted_column(column: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {
        "column_name": column["column_name"],
        "data_type": column["data_type"],
        "column_class": column["column_class"],
        "null_rate": column["null_rate"],
        "distinct_count": column["distinct_count"],
    }
    if column["column_class"] == "identifier_or_freetext":
        return redacted
    if column["min_value"] is not None:
        redacted["min_value"] = column["min_value"]
    if column["max_value"] is not None:
        redacted["max_value"] = column["max_value"]
    if column["top_values"]:
        redacted["top_values"] = [tv["value"] for tv in column["top_values"][:_MAX_TOP_VALUES]]
    if column["reference_samples"] is not None:
        redacted["reference_samples"] = column["reference_samples"]["values"][
            :_MAX_REFERENCE_SAMPLES
        ]
    return redacted


def build_catalog_context(catalog: dict[str, Any]) -> dict[str, Any]:
    """A Tier M-safe summary of the catalog: view/column names, types,
    classes, aggregate stats, and (only for a non-identifier column) its
    captured domain/range/vocabulary sample -- never a row-level value, and
    never anything at all for an `identifier_or_freetext` column beyond its
    name/type/null-rate/distinct-count."""
    return {
        "views": [
            {
                "qualified_name": view["qualified_name"],
                "archetype": view["archetype"],
                "row_count": view["row_count"],
                "columns": [_redacted_column(c) for c in view["columns"]],
                "candidate_keys": [{"columns": ck["columns"]} for ck in view["candidate_keys"]],
            }
            for view in catalog["views"]
        ],
        "relationships": [
            {
                "from_view": edge["from_view"],
                "from_column": edge["from_column"],
                "to_view": edge["to_view"],
                "to_column": edge["to_column"],
                "containment_a_to_b": edge["containment_a_to_b"],
                "containment_b_to_a": edge["containment_b_to_a"],
                "orphan_count_a": edge["orphan_count_a"],
                "orphan_count_b": edge["orphan_count_b"],
            }
            for edge in catalog["relationships"]
            if edge["status"] == "evaluated"
        ],
    }


_WORKED_EXAMPLE: dict[str, Any] = {
    "id": "appointment-completed-no-invoice-example",
    "title": "Appointment completed with no invoice",
    "category": "workflow",
    "default_severity": "high",
    "entity": {
        "view": "dbo.Appointments",
        "key": ["AppointmentID"],
        "practice_column": "PracticeID",
        "base_filters": ["IsDeleted = 0"],
    },
    "params": {},
    "prerequisites": ["AppointmentCompleted IS NOT NULL"],
    "predicate": {
        "all": [
            "AppointmentCompleted = 1",
            {
                "not_exists": {
                    "view": "fqb.Invoices",
                    "on": "fqb.Invoices.PracticeID = dbo.Appointments.PracticeID",
                }
            },
        ]
    },
    "evidence": ["AppointmentID", "AppointmentStatus", "PracticeID"],
    "actions": ["verify-invoice"],
    "resolution": "An invoice now exists, or the finding is dismissed with a reason.",
}


def build_prompt(catalog_context: dict[str, Any], category: str) -> str:
    dsl_schema_text = SCHEMA_PATH.read_text(encoding="utf-8")
    brief = CATEGORY_BRIEFS[category]
    return (
        f"You are drafting CDSS check definitions in category '{category}'.\n\n"
        f"Category brief: {brief}\n\n"
        "Every check you draft must be a JSON object matching this JSON Schema exactly "
        "(structure only -- it does not know which views/columns really exist):\n"
        f"{dsl_schema_text}\n\n"
        "Here is the only catalog you may reference views/columns/domains from -- a "
        "redacted summary (no patient rows, no identifiers, aggregate statistics and "
        f"vocabulary samples only):\n{json.dumps(catalog_context, indent=2)}\n\n"
        "`actions` must be one of: flag-for-data-steward-review, flag-for-clinician-review, "
        "book-recall, verify-invoice, chase-result, correct-record, raise-billing-task, "
        "request-nhi-lookup, raise-recall-task.\n\n"
        "IMPORTANT: `evidence` must be a list of real column names copied verbatim from the "
        "catalog above (the entity's own view or a joined view) -- never a sentence, "
        "description, or explanation. Every string in `evidence` must exactly match a "
        "`column_name` the catalog lists. Likewise, every column referenced inside "
        "`predicate`/`prerequisites`/`base_filters`/`on`/`where` must be a real `column_name` "
        "from the catalog, not a paraphrase.\n\n"
        "Here is one complete, correctly-shaped worked example (illustrative only -- do not "
        f"repeat it verbatim):\n{json.dumps(_WORKED_EXAMPLE, indent=2)}\n\n"
        "Return a JSON array of 6-10 check documents, and nothing else -- no prose, no "
        "markdown fences."
    )


def build_repair_prompt(broken_document: dict[str, Any], error: str) -> str:
    return (
        "The following check document failed validation with this error:\n"
        f"{error}\n\n"
        f"Document:\n{json.dumps(broken_document, indent=2)}\n\n"
        "Return one corrected JSON object fixing exactly this problem, and nothing else -- "
        "no prose, no markdown fences."
    )


# --- LLM client ---------------------------------------------------------------


class LLMClient(Protocol):
    def complete(self, prompt: str) -> str: ...


class OpenAIClient:
    def __init__(self, config: LLMConfig) -> None:
        self._client = OpenAI(api_key=config.api_key)
        self._model = config.model

    def complete(self, prompt: str) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
        )
        content = response.choices[0].message.content
        if content is None:
            raise ValueError("LLM returned an empty response")
        return content


# --- drafting + validate-or-repair --------------------------------------------


@dataclass(frozen=True)
class LLMDraftCheck:
    slug: str
    title: str
    category: str
    default_severity: str
    definition: dict[str, Any]
    rationale: str
    affected_views: list[str]


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if stripped.endswith("```"):
            stripped = stripped[: -len("```")]
    return stripped.strip()


def parse_llm_response(text: str) -> list[dict[str, Any]]:
    parsed = json.loads(_strip_code_fences(text))
    if isinstance(parsed, dict):
        return [parsed]
    if not isinstance(parsed, list):
        raise ValueError(f"expected a JSON array or object, got {type(parsed).__name__}")
    return parsed


def _validate(definition: dict[str, Any], catalog_index: CatalogIndex) -> str | None:
    """Returns None if valid, else the error message."""
    try:
        doc = check_doc_from_dict(definition)
        validate_check_against_catalog(doc, catalog_index)
    except (CheckValidationError, CheckReferenceError) as exc:
        return str(exc)
    return None


def _to_draft(definition: dict[str, Any], category: str, rationale: str) -> LLMDraftCheck:
    doc = check_doc_from_dict(definition)
    affected_views = {doc.entity.view}
    collect_joined_views(doc.predicate, affected_views)
    return LLMDraftCheck(
        slug=doc.id,
        title=doc.title,
        category=category,
        default_severity=doc.default_severity,
        definition=definition,
        rationale=rationale,
        affected_views=sorted(affected_views),
    )


def draft_checks_for_category(
    client: LLMClient, catalog: dict[str, Any], category: str
) -> list[LLMDraftCheck]:
    """One category brief -> zero or more valid drafts. Every candidate gets
    at most one repair round-trip (the phase spec's own "fed back once for
    repair"); a document still invalid after that is dropped, not raised."""
    catalog_index = CatalogIndex(catalog)
    prompt = build_prompt(build_catalog_context(catalog), category)
    raw_response = client.complete(prompt)
    candidates = parse_llm_response(raw_response)

    drafts: list[LLMDraftCheck] = []
    for candidate in candidates:
        error = _validate(candidate, catalog_index)
        if error is not None:
            repaired_text = client.complete(build_repair_prompt(candidate, error))
            try:
                candidate = parse_llm_response(repaired_text)[0]
            except (json.JSONDecodeError, IndexError, ValueError):
                continue
            error = _validate(candidate, catalog_index)
            if error is not None:
                continue
        rationale = f"LLM-drafted ({category}): {candidate.get('title', '')}"
        drafts.append(_to_draft(candidate, category, rationale))
    return drafts


def generate_llm_drafts(
    client: LLMClient, catalog: dict[str, Any], *, categories: list[str] | None = None
) -> list[LLMDraftCheck]:
    categories = categories if categories is not None else list(CATEGORY_BRIEFS)
    drafts: list[LLMDraftCheck] = []
    for category in categories:
        drafts.extend(draft_checks_for_category(client, catalog, category))
    return drafts


# --- persistence (drafts land as checks(source=llm, status=draft)) ----------

_INSERT_CHECK = sa.text(
    "INSERT INTO checks (slug, title, category, default_severity, source, status) "
    "VALUES (:slug, :title, :category, :default_severity, 'llm', 'draft') "
    "ON CONFLICT (slug) DO NOTHING "
    "RETURNING id"
)

_INSERT_CHECK_VERSION = sa.text(
    "INSERT INTO check_versions "
    "(check_id, version_number, definition, definition_hash, rationale, "
    "affected_views, params_schema) "
    "VALUES (:check_id, 1, CAST(:definition AS jsonb), :definition_hash, :rationale, "
    ":affected_views, CAST(:params_schema AS jsonb))"
)


def persist_llm_drafts(conn: sa.Connection, drafts: list[LLMDraftCheck]) -> list[str]:
    """Same idempotent-by-slug shape as `cdss.authoring.derive.persist_draft_checks`
    -- source='llm' instead of 'profiling' is the only difference that matters
    to F3's review gate, which treats every draft identically regardless of
    origin."""
    inserted_ids: list[str] = []
    for draft in drafts:
        result = conn.execute(
            _INSERT_CHECK,
            {
                "slug": draft.slug,
                "title": draft.title,
                "category": draft.category,
                "default_severity": draft.default_severity,
            },
        )
        row = result.one_or_none()
        if row is None:
            continue
        check_id = str(row.id)
        inserted_ids.append(check_id)
        definition_json = json.dumps(draft.definition, sort_keys=True)
        doc = check_doc_from_dict(draft.definition)
        conn.execute(
            _INSERT_CHECK_VERSION,
            {
                "check_id": check_id,
                "definition": definition_json,
                "definition_hash": hashlib.sha256(definition_json.encode("utf-8")).hexdigest(),
                "rationale": draft.rationale,
                "affected_views": draft.affected_views,
                "params_schema": json.dumps({name: p.type for name, p in doc.params.items()}),
            },
        )
    return inserted_ids


def _latest_catalog_path(catalog_dir: Path = Path("artifacts/catalog")) -> Path:
    latest_version = next_catalog_version(catalog_dir) - 1
    if latest_version < 1:
        raise FileNotFoundError(f"no semantic-catalog-vN.json found in {catalog_dir}")
    return catalog_dir / f"semantic-catalog-v{latest_version}.json"


def main() -> int:
    """Production entrypoint: the latest `semantic-catalog-vN.json` artifact
    (Phase 1) against the real OpenAI API (D-004) and the real app DB."""
    catalog_path = _latest_catalog_path()
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    client = OpenAIClient(load_llm_config())
    drafts = generate_llm_drafts(client, catalog)

    engine = sa.create_engine(load_app_db_url())
    try:
        with engine.begin() as conn:
            inserted_ids = persist_llm_drafts(conn, drafts)
    finally:
        engine.dispose()

    print(f"generated {len(drafts)} valid draft(s) from {catalog_path.name}")
    print(f"persisted {len(inserted_ids)} new draft(s) (rest already existed by slug)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CATEGORY_BRIEFS",
    "LLMClient",
    "LLMConfig",
    "LLMDraftCheck",
    "MissingLLMConfigError",
    "OpenAIClient",
    "build_catalog_context",
    "build_prompt",
    "build_repair_prompt",
    "draft_checks_for_category",
    "generate_llm_drafts",
    "load_llm_config",
    "parse_llm_response",
    "persist_llm_drafts",
]
