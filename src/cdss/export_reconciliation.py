"""Phase 1 step 7: export reconciliation & discrepancy log.

Compares live-profiled column metadata (`INFORMATION_SCHEMA.COLUMNS`, D-015
-- catalog-only, no PHI) against `schema_for_SQL_PROJ.txt`'s per-view
hypotheses: documented columns/types (`columnsinformation`) and free-text
relation mentions (`tablerelations`). D-017: this documentation is
unverified -- live always wins; every disagreement is logged here rather
than silently reconciled one way or the other. 6 of the 10 in-scope views
have empty hypothesis stubs (no columns/relations documented at all) -- a
view like that trivially produces zero discrepancies, not a parse error.

`tablerelations` is inconsistently formatted across entries (blank-line
separated, arrow-separated, en-dash-separated, or one unstructured
sentence -- all four appear in the real file), so related-table extraction
does not try to parse a delimiter structure: it scans the free text for any
schema-qualified token (`dbo.X`, `fqb.X`, ...) instead, which is robust to
all four styles.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_SCHEMA_QUALIFIED_NAME_RE = re.compile(r"\b([a-zA-Z]{3,})\.([a-zA-Z0-9_]{2,})\b")
_COLUMN_ROW_RE = re.compile(r"^([A-Za-z_]\w*)\s*\|\s*([A-Za-z_]\w*)", re.MULTILINE)
_HEADER_NAMES = {"column", "columns"}

_TYPE_FAMILIES: dict[str, str] = {
    "int": "integer",
    "bigint": "integer",
    "smallint": "integer",
    "tinyint": "integer",
    "decimal": "numeric",
    "numeric": "numeric",
    "float": "numeric",
    "real": "numeric",
    "money": "numeric",
    "smallmoney": "numeric",
    "nvarchar": "string",
    "varchar": "string",
    "nchar": "string",
    "char": "string",
    "text": "string",
    "ntext": "string",
    "bit": "boolean",
    "date": "datetime",
    "datetime": "datetime",
    "datetime2": "datetime",
    "smalldatetime": "datetime",
    "datetimeoffset": "datetime",
}

ColumnDiscrepancyType = Literal[
    "documented_missing_live", "undocumented_live_column", "type_mismatch"
]
RelationStatus = Literal["in_scope_corroborated", "in_scope_uncorroborated", "out_of_scope"]


@dataclass(frozen=True)
class ExportColumnHypothesis:
    name: str
    data_type_hint: str | None


@dataclass(frozen=True)
class ExportViewHypothesis:
    qualified_name: str
    columns: list[ExportColumnHypothesis]
    related_table_names: list[str]


@dataclass(frozen=True)
class ColumnDiscrepancy:
    discrepancy_type: ColumnDiscrepancyType
    column_name: str
    documented_type: str | None
    live_type: str | None


@dataclass(frozen=True)
class RelationDiscrepancy:
    related_table_name: str
    status: RelationStatus


@dataclass(frozen=True)
class ViewDiscrepancyReport:
    qualified_name: str
    column_discrepancies: list[ColumnDiscrepancy]
    relation_discrepancies: list[RelationDiscrepancy]


def _type_family(data_type: str) -> str | None:
    return _TYPE_FAMILIES.get(data_type.strip().lower())


def _parse_columns_information(text: str) -> list[ExportColumnHypothesis]:
    if not text.strip():
        return []
    columns = []
    for name, data_type in _COLUMN_ROW_RE.findall(text):
        if name.lower().lstrip("#") in _HEADER_NAMES:
            continue
        columns.append(ExportColumnHypothesis(name=name, data_type_hint=data_type))
    return columns


def _parse_related_tables(text: str) -> list[str]:
    if not text.strip():
        return []
    seen: dict[str, str] = {}
    for schema, name in _SCHEMA_QUALIFIED_NAME_RE.findall(text):
        qualified = f"{schema}.{name}"
        seen.setdefault(qualified.lower(), qualified)
    return list(seen.values())


def parse_export_hypotheses(path: Path) -> dict[str, ExportViewHypothesis]:
    """Pure parse of `schema_for_SQL_PROJ.txt` -- no DB access."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    hypotheses: dict[str, ExportViewHypothesis] = {}
    for entry in raw:
        qualified_name = entry["table"]
        hypotheses[qualified_name] = ExportViewHypothesis(
            qualified_name=qualified_name,
            columns=_parse_columns_information(entry.get("columnsinformation", "")),
            related_table_names=_parse_related_tables(entry.get("tablerelations", "")),
        )
    return hypotheses


def reconcile_view(
    hypothesis: ExportViewHypothesis,
    *,
    live_columns_by_view: dict[str, list[tuple[str, str]]],
    in_scope_views: frozenset[str],
) -> ViewDiscrepancyReport:
    """`live_columns_by_view` maps lowercased qualified view name -> `(name,
    data_type)` columns, e.g. from `profiler.fetch_columns()` for every
    in-scope view. `in_scope_views` is the lowercased 10-view scope, used to
    classify each documented relation."""
    this_key = hypothesis.qualified_name.lower()
    live_columns = live_columns_by_view.get(this_key, [])
    live_by_name = {name.lower(): data_type for name, data_type in live_columns}
    documented_by_name = {c.name.lower(): c for c in hypothesis.columns}

    column_discrepancies: list[ColumnDiscrepancy] = []
    for doc_name_lower, doc_col in documented_by_name.items():
        if doc_name_lower not in live_by_name:
            column_discrepancies.append(
                ColumnDiscrepancy(
                    discrepancy_type="documented_missing_live",
                    column_name=doc_col.name,
                    documented_type=doc_col.data_type_hint,
                    live_type=None,
                )
            )
            continue
        live_type = live_by_name[doc_name_lower]
        doc_family = _type_family(doc_col.data_type_hint) if doc_col.data_type_hint else None
        live_family = _type_family(live_type)
        if doc_family is not None and live_family is not None and doc_family != live_family:
            column_discrepancies.append(
                ColumnDiscrepancy(
                    discrepancy_type="type_mismatch",
                    column_name=doc_col.name,
                    documented_type=doc_col.data_type_hint,
                    live_type=live_type,
                )
            )

    if hypothesis.columns:
        for name, data_type in live_columns:
            if name.lower() not in documented_by_name:
                column_discrepancies.append(
                    ColumnDiscrepancy(
                        discrepancy_type="undocumented_live_column",
                        column_name=name,
                        documented_type=None,
                        live_type=data_type,
                    )
                )

    relation_discrepancies: list[RelationDiscrepancy] = []
    this_names = {name.lower() for name, _dt in live_columns}
    for related in hypothesis.related_table_names:
        related_key = related.lower()
        if related_key not in in_scope_views:
            status: RelationStatus = "out_of_scope"
        else:
            related_columns = live_columns_by_view.get(related_key)
            if related_columns is None:
                status = "in_scope_uncorroborated"
            else:
                related_names = {name.lower() for name, _dt in related_columns}
                shared_id_like = {n for n in this_names & related_names if n.endswith("id")}
                status = "in_scope_corroborated" if shared_id_like else "in_scope_uncorroborated"
        relation_discrepancies.append(
            RelationDiscrepancy(related_table_name=related, status=status)
        )

    return ViewDiscrepancyReport(
        qualified_name=hypothesis.qualified_name,
        column_discrepancies=column_discrepancies,
        relation_discrepancies=relation_discrepancies,
    )
