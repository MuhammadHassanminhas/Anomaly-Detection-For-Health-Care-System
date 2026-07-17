"""Phase 4 step 2: cdss.authoring.derive -- the profiling-derived check
generator (F3a).

Reads an already-validated semantic catalog (Phase 1, D-017's sole authority)
and emits draft checks purely from evidence the catalog itself already
recorded -- never a fresh source-DB query, never a value invented ahead of
what profiling found:

- a relationship edge with orphans -> a referential-integrity check
  (`not_exists` against the reference view);
- a sentinel candidate -> a placeholder-value check (flags the exact
  sentinel value/description profiling found);
- a categorical column's captured top_values -> an enum/domain check
  (flags any value outside the observed vocabulary, mirroring
  appointment-invalid-status-code.yaml's existing `not`+`in` pattern);
- a measure column's captured max_value already past "now" at generation
  time -> a range check (`col > sysdatetime()`, evaluated live on every run
  -- the catalog's max_value is only ever the *evidence* that authored the
  check, never a value baked into its compiled SQL);
- a recorded candidate key -> a duplicate check (`COUNT(*) OVER (PARTITION
  BY ...) > 1`, a single leaf expression -- no self-join needed, since the
  compiler's ExistsClause has no aliasing mechanism for referencing the same
  view as both the driving and the joined side; a window function inside the
  tri-state CASE expression's SELECT list sidesteps that entirely and is
  valid T-SQL because it never appears in a WHERE/HAVING clause).

Deterministic: the same catalog dict always yields the same ordered list of
drafts (iteration follows the catalog's own list order throughout, never an
unordered set/dict). Every draft is self-validated before being returned --
`check_doc_from_dict` (structural) and `validate_check_against_catalog` (F2)
both run inside the generator, so a malformed draft is a generator bug caught
here, never something that reaches the review gate (step 3).

Some drafts will be true-but-trivial (e.g. a technical surrogate key that
can never actually duplicate) or wrong in ways only a human reviewer can see
(D-017's "plausible-but-wrong" risk) -- the review gate (step 3, "the one
gate") is the intended filter, not this generator.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa

from cdss.dsl import CatalogIndex, check_doc_from_dict, validate_check_against_catalog

DATE_TYPES: frozenset[str] = frozenset(
    {"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset"}
)

_REFERENTIAL_ACTION = "flag-for-data-steward-review"
_MIN_DOMAIN_VALUES = 2


@dataclass(frozen=True)
class DraftCheck:
    slug: str
    title: str
    category: str
    default_severity: str
    definition: dict[str, Any]
    rationale: str
    affected_views: list[str]


def _slugify(name: str) -> str:
    """CamelCase/PascalCase -> kebab-case, e.g. `DiseaseID` -> `disease-id`,
    `ScheduleDate` -> `schedule-date`. Used only for slug/title generation --
    never for anything the catalog/DSL compares by equality."""
    step1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1-\2", name)
    step2 = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", step1)
    return step2.lower()


def _view_slug(qualified_name: str) -> str:
    return _slugify(qualified_name.split(".")[-1])


def _entity_key_for_view(view: dict[str, Any], *, fallback_column: str) -> list[str]:
    """The view's own recorded single-column candidate key, if profiling
    found one -- otherwise the FK/evidence column itself (flagged, not
    ideal: without a verified unique column, findings dedupe on a
    possibly-repeating key, which is still safe -- just less precise -- for
    a human-reviewed draft)."""
    for candidate in view["candidate_keys"]:
        if len(candidate["columns"]) == 1:
            return list(candidate["columns"])
    return [fallback_column]


def _self_validate(definition: dict[str, Any], catalog_index: CatalogIndex) -> None:
    doc = check_doc_from_dict(definition)
    validate_check_against_catalog(doc, catalog_index)


# --- referential-integrity checks (relationship edges with orphans) --------


def _referential_draft(
    *,
    entity_view: str,
    entity_key: list[str],
    fk_column: str,
    ref_view: str,
    ref_column: str,
    orphan_count: int,
    containment: float | None,
) -> DraftCheck:
    slug = f"{_view_slug(entity_view)}-{_slugify(fk_column)}-orphan-{_view_slug(ref_view)}"
    title = f"{fk_column} on {entity_view} has no matching {ref_column} on {ref_view}"
    definition: dict[str, Any] = {
        "id": slug,
        "title": title,
        "category": "referential",
        "default_severity": "high",
        "entity": {
            "view": entity_view,
            "key": entity_key,
            "practice_column": "PracticeID",
            "base_filters": [],
        },
        "params": {},
        "prerequisites": [f"{fk_column} IS NOT NULL"],
        "predicate": {
            "not_exists": {
                "view": ref_view,
                "on": f"{ref_view}.{ref_column} = {entity_view}.{fk_column}",
            }
        },
        "evidence": [*entity_key, fk_column, "PracticeID"],
        "actions": [_REFERENTIAL_ACTION],
        "resolution": (
            f"{fk_column} is corrected to a value present in {ref_view}.{ref_column}, "
            "or the finding is dismissed with a reason."
        ),
    }
    containment_text = "n/a" if containment is None else f"{containment:.4f}"
    rationale = (
        f"Phase 1 relationship analysis found {orphan_count} value(s) of "
        f"{entity_view}.{fk_column} not present in {ref_view}.{ref_column} "
        f"(containment ratio {containment_text})."
    )
    return DraftCheck(
        slug=slug,
        title=title,
        category="referential",
        default_severity="high",
        definition=definition,
        rationale=rationale,
        affected_views=[entity_view, ref_view],
    )


def _referential_drafts_from_edge(
    edge: dict[str, Any], views_by_name: dict[str, dict[str, Any]]
) -> list[DraftCheck]:
    if edge["status"] != "evaluated":
        return []
    drafts: list[DraftCheck] = []
    # A reference/dictionary view (D-023) can never be a check's own entity
    # (see the per-view loop's own guard in generate_draft_checks) -- only
    # ever a join target. Same rule applies here: whichever side of this
    # edge would become the *entity* for a given direction must be a fact
    # view, regardless of which side has the orphans.
    if edge["orphan_count_a"] and views_by_name[edge["from_view"]]["archetype"] != "reference":
        drafts.append(
            _referential_draft(
                entity_view=edge["from_view"],
                entity_key=_entity_key_for_view(
                    views_by_name[edge["from_view"]], fallback_column=edge["from_column"]
                ),
                fk_column=edge["from_column"],
                ref_view=edge["to_view"],
                ref_column=edge["to_column"],
                orphan_count=edge["orphan_count_a"],
                containment=edge["containment_a_to_b"],
            )
        )
    if edge["orphan_count_b"] and views_by_name[edge["to_view"]]["archetype"] != "reference":
        drafts.append(
            _referential_draft(
                entity_view=edge["to_view"],
                entity_key=_entity_key_for_view(
                    views_by_name[edge["to_view"]], fallback_column=edge["to_column"]
                ),
                fk_column=edge["to_column"],
                ref_view=edge["from_view"],
                ref_column=edge["from_column"],
                orphan_count=edge["orphan_count_b"],
                containment=edge["containment_b_to_a"],
            )
        )
    return drafts


# --- placeholder-value checks (sentinel prevalence) -------------------------


_SENTINEL_TITLES: dict[str, str] = {
    "placeholder_date": "placeholder date value",
    "zero_or_negative_id": "zero/negative sentinel ID",
    "empty_string_overload": "empty-string overload value",
    "magic_value": "magic sentinel value",
}

_STRING_DATA_TYPES: frozenset[str] = frozenset(
    {"char", "varchar", "nchar", "nvarchar", "text", "ntext"}
)


def _is_numeric_column(view: dict[str, Any], column_name: str) -> bool:
    for column in view["columns"]:
        if column["column_name"] == column_name:
            data_type = column["data_type"].lower()
            return data_type not in _STRING_DATA_TYPES and data_type not in DATE_TYPES
    return False


def _sentinel_draft(view: dict[str, Any], sentinel: dict[str, Any]) -> DraftCheck:
    entity_view = view["qualified_name"]
    column = sentinel["column_name"]
    numeric = _is_numeric_column(view, column)
    literal = sentinel["value"] if numeric else f"'{sentinel['value']}'"
    entity_key = _entity_key_for_view(view, fallback_column=column)
    slug = (
        f"{_view_slug(entity_view)}-{_slugify(column)}-"
        f"{sentinel['sentinel_type'].replace('_', '-')}"
    )
    label = _SENTINEL_TITLES[sentinel["sentinel_type"]]
    title = f"{column} on {entity_view} holds a {label}"
    definition: dict[str, Any] = {
        "id": slug,
        "title": title,
        "category": "data-quality",
        "default_severity": "medium",
        "entity": {
            "view": entity_view,
            "key": entity_key,
            "practice_column": "PracticeID",
            "base_filters": [],
        },
        "params": {},
        "prerequisites": [f"{column} IS NOT NULL"],
        "predicate": f"{column} = {literal}",
        "evidence": [*entity_key, column, "PracticeID"],
        "actions": [_REFERENTIAL_ACTION],
        "resolution": (
            f"{column} is corrected to a real value, or the finding is dismissed with a reason."
        ),
    }
    rationale = (
        f"Phase 1 profiling found {sentinel['frequency']} row(s) with "
        f"{entity_view}.{column} = {sentinel['value']!r} ({sentinel['description']})."
    )
    return DraftCheck(
        slug=slug,
        title=title,
        category="data-quality",
        default_severity="medium",
        definition=definition,
        rationale=rationale,
        affected_views=[entity_view],
    )


# --- enum/domain-violation checks (categorical top_values) ------------------


def _enum_draft(view: dict[str, Any], column: dict[str, Any]) -> DraftCheck:
    entity_view = view["qualified_name"]
    column_name = column["column_name"]
    values = [tv["value"] for tv in column["top_values"] if tv["value"] is not None]
    entity_key = _entity_key_for_view(view, fallback_column=column_name)
    param_name = f"{_slugify(column_name).replace('-', '_')}_valid_values"
    slug = f"{_view_slug(entity_view)}-{_slugify(column_name)}-invalid-domain-value"
    title = f"{column_name} on {entity_view} has a value outside the reviewed domain"
    definition: dict[str, Any] = {
        "id": slug,
        "title": title,
        "category": "data-quality",
        "default_severity": "medium",
        "entity": {
            "view": entity_view,
            "key": entity_key,
            "practice_column": "PracticeID",
            "base_filters": [],
        },
        "params": {
            param_name: {
                "type": "array",
                "default": {"strategy": "fixed", "value": values},
            }
        },
        "prerequisites": [f"{column_name} IS NOT NULL"],
        "predicate": {"not": f"{column_name} IN ({{{param_name}}})"},
        "evidence": [*entity_key, column_name, "PracticeID"],
        "actions": [_REFERENTIAL_ACTION],
        "resolution": (
            f"{column_name} is corrected to a value in the reviewed domain, "
            "or the finding is dismissed with a reason."
        ),
    }
    rationale = (
        f"Phase 1 profiling captured {len(values)} distinct value(s) for "
        f"{entity_view}.{column_name}: {values!r}."
    )
    return DraftCheck(
        slug=slug,
        title=title,
        category="data-quality",
        default_severity="medium",
        definition=definition,
        rationale=rationale,
        affected_views=[entity_view],
    )


# --- range checks (measure columns with an already-impossible max_value) ----


def _parse_catalog_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _range_draft(
    view: dict[str, Any], column: dict[str, Any], *, now: datetime
) -> DraftCheck | None:
    if column["column_class"] != "measure":
        return None
    if column["data_type"].lower() not in DATE_TYPES:
        return None
    if column["max_value"] is None:
        return None
    parsed_max = _parse_catalog_datetime(column["max_value"])
    if parsed_max is None or parsed_max <= now:
        return None

    entity_view = view["qualified_name"]
    column_name = column["column_name"]
    entity_key = _entity_key_for_view(view, fallback_column=column_name)
    slug = f"{_view_slug(entity_view)}-{_slugify(column_name)}-impossible-future-date"
    title = f"{column_name} on {entity_view} is set to a future date"
    definition: dict[str, Any] = {
        "id": slug,
        "title": title,
        "category": "data-quality",
        "default_severity": "medium",
        "entity": {
            "view": entity_view,
            "key": entity_key,
            "practice_column": "PracticeID",
            "base_filters": [],
        },
        "params": {},
        "prerequisites": [f"{column_name} IS NOT NULL"],
        "predicate": f"{column_name} > sysdatetime()",
        "evidence": [*entity_key, column_name, "PracticeID"],
        "actions": [_REFERENTIAL_ACTION],
        "resolution": (
            f"{column_name} is corrected to a non-future date, or the finding is dismissed "
            "with a reason."
        ),
    }
    rationale = (
        f"Phase 1 profiling captured a max value of {column['max_value']} for "
        f"{entity_view}.{column_name}, already later than generation time ({now.isoformat()}) "
        "-- at least one future-dated row already exists."
    )
    return DraftCheck(
        slug=slug,
        title=title,
        category="data-quality",
        default_severity="medium",
        definition=definition,
        rationale=rationale,
        affected_views=[entity_view],
    )


# --- duplicate checks (recorded candidate keys) -----------------------------


def _duplicate_draft(view: dict[str, Any], candidate: dict[str, Any]) -> DraftCheck:
    entity_view = view["qualified_name"]
    columns = list(candidate["columns"])
    partition = ", ".join(f"{entity_view}.{c}" for c in columns)
    slug = f"{_view_slug(entity_view)}-{'-'.join(_slugify(c) for c in columns)}-duplicate"
    title = f"{', '.join(columns)} on {entity_view} is duplicated across rows"
    definition: dict[str, Any] = {
        "id": slug,
        "title": title,
        "category": "data-quality",
        "default_severity": "high",
        "entity": {
            "view": entity_view,
            "key": columns,
            "practice_column": "PracticeID",
            "base_filters": [],
        },
        "params": {},
        "prerequisites": [],
        "predicate": f"COUNT(*) OVER (PARTITION BY {partition}) > 1",
        "evidence": [*columns, "PracticeID"],
        "actions": [_REFERENTIAL_ACTION],
        "resolution": (
            f"No other row shares the same {', '.join(columns)}, or the finding is dismissed "
            "with a reason."
        ),
    }
    rationale = (
        f"Phase 1 profiling found {entity_view}.{'/'.join(columns)} unique across "
        f"{candidate['distinct_count']} of {candidate['row_count']} rows "
        f"({candidate['evidence_method']}) -- this check guards that invariant going forward."
    )
    return DraftCheck(
        slug=slug,
        title=title,
        category="data-quality",
        default_severity="high",
        definition=definition,
        rationale=rationale,
        affected_views=[entity_view],
    )


# --- orchestration -----------------------------------------------------------


def generate_draft_checks(
    catalog: dict[str, Any], *, now: datetime | None = None
) -> list[DraftCheck]:
    """Deterministic: the same catalog dict always yields the same ordered
    list of drafts. Every draft is self-validated (structural + F2) before
    being returned -- a generator bug raises here, never reaches the review
    gate silently malformed."""
    generation_time = now if now is not None else datetime.now()
    catalog_index = CatalogIndex(catalog)
    views_by_name = {view["qualified_name"]: view for view in catalog["views"]}

    drafts: list[DraftCheck] = []

    for edge in catalog["relationships"]:
        drafts.extend(_referential_drafts_from_edge(edge, views_by_name))

    for view in catalog["views"]:
        if view["archetype"] == "reference":
            # A reference/dictionary view (D-023) is shared vocabulary, not
            # per-practice operational data -- it has no practice_column at
            # all (confirmed live: dbo.Disease has none) and must only ever
            # appear as a join target (the referential-integrity loop above
            # already does this correctly), never as a check's own entity.
            continue
        for sentinel in view["sentinels"]:
            drafts.append(_sentinel_draft(view, sentinel))
        for column in view["columns"]:
            if column["column_class"] == "categorical_coded" and (
                len(column["top_values"]) >= _MIN_DOMAIN_VALUES
            ):
                drafts.append(_enum_draft(view, column))
            range_draft = _range_draft(view, column, now=generation_time)
            if range_draft is not None:
                drafts.append(range_draft)
        for candidate in view["candidate_keys"]:
            drafts.append(_duplicate_draft(view, candidate))

    for draft in drafts:
        _self_validate(draft.definition, catalog_index)

    return drafts


# --- persistence (drafts land as checks(source=profiling, status=draft)) ----

_INSERT_CHECK = sa.text(
    "INSERT INTO checks (slug, title, category, default_severity, source, status) "
    "VALUES (:slug, :title, :category, :default_severity, 'profiling', 'draft') "
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


def _params_schema(definition: dict[str, Any]) -> dict[str, str]:
    return {name: param["type"] for name, param in definition["params"].items()}


def persist_draft_checks(conn: sa.Connection, drafts: list[DraftCheck]) -> list[str]:
    """Insert each draft as `checks(source='profiling', status='draft')` +
    its `check_versions` row (version 1, with the generator's own rationale
    attached). Idempotent by slug: re-running the generator against an
    unchanged catalog and persisting again inserts nothing new for a slug
    that's already there (`ON CONFLICT (slug) DO NOTHING`) -- a check that
    already exists (draft or otherwise) is never silently superseded here;
    that is the review gate's (step 3) job, not this function's. Returns the
    `checks.id` of every draft actually inserted this call."""
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
        conn.execute(
            _INSERT_CHECK_VERSION,
            {
                "check_id": check_id,
                "definition": definition_json,
                "definition_hash": hashlib.sha256(definition_json.encode("utf-8")).hexdigest(),
                "rationale": draft.rationale,
                "affected_views": draft.affected_views,
                "params_schema": json.dumps(_params_schema(draft.definition)),
            },
        )
    return inserted_ids


__all__ = ["DraftCheck", "generate_draft_checks", "persist_draft_checks"]
