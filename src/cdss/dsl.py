"""Phase 2 step 2: parse a YAML check document into a typed model, then
validate it against the semantic catalog (F2 -- every referenced view/
column/join/param/action must be known) and the stub action registry.

Structural validation is entirely owned by `check-dsl.schema.json` (step 1)
-- this module never re-implements shape rules, only builds a typed model
from an already-schema-valid document and checks its references.

Leaf expressions (comparisons, null tests, date arithmetic, `in`) are opaque
strings by design (docs/dsl.md); column references inside them are extracted
with `sqlglot` (already a project dependency, D-014 -- the same parser the
Phase 0 SQL guard uses) rather than a hand-rolled tokenizer. `{param}`
placeholders are not valid SQL syntax, so they are substituted with an inert
literal before parsing -- found live while building this: sqlglot otherwise
misparses a bare `{name}` as a struct literal and reports its inner
identifier as a spurious column reference.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import sqlglot
import yaml
from sqlglot import exp
from sqlglot.errors import ParseError

SCHEMA_PATH = Path(__file__).parent / "schemas" / "check-dsl.schema.json"

# Phase 4 builds the real curated action library (the F8 narration
# allowlist's action-code source); this stub only recognizes the actions the
# Phase 2 step 1 example checks actually use -- never invented ahead of need.
STUB_ACTION_LIBRARY: frozenset[str] = frozenset(
    {
        "verify-invoice",
        "raise-billing-task",
        "request-nhi-lookup",
        "flag-for-data-steward-review",
        "raise-recall-task",
    }
)

_PARAM_REF = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class CheckValidationError(ValueError):
    """A check document failed structural (JSON Schema) validation."""


class CheckReferenceError(ValueError):
    """A structurally valid check document references a view, column, join,
    param, or action the catalog/registry doesn't know about (F2)."""


def _load_schema() -> dict[str, Any]:
    result: dict[str, Any] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return result


# --- typed model --------------------------------------------------------------


@dataclass(frozen=True)
class EntityDef:
    view: str
    key: tuple[str, ...]
    practice_column: str
    base_filters: tuple[str, ...]


@dataclass(frozen=True)
class ParamDefault:
    strategy: str
    value: Any = None
    measure: str | None = None
    p: float | None = None
    fallback: Any = None


@dataclass(frozen=True)
class ParamDef:
    type: str
    default: ParamDefault


@dataclass(frozen=True)
class ExistsClause:
    view: str
    on: str
    where: str | None


@dataclass(frozen=True)
class AllNode:
    all: tuple[PredicateNode, ...]


@dataclass(frozen=True)
class AnyNode:
    any: tuple[PredicateNode, ...]


@dataclass(frozen=True)
class NotNode:
    not_: PredicateNode


@dataclass(frozen=True)
class ExistsNode:
    exists: ExistsClause


@dataclass(frozen=True)
class NotExistsNode:
    not_exists: ExistsClause


PredicateNode = str | AllNode | AnyNode | NotNode | ExistsNode | NotExistsNode


@dataclass(frozen=True)
class CheckDoc:
    id: str
    title: str
    category: str
    default_severity: str
    entity: EntityDef
    params: dict[str, ParamDef]
    prerequisites: tuple[str, ...]
    predicate: PredicateNode
    evidence: tuple[str, ...]
    actions: tuple[str, ...]
    resolution: str


def _parse_exists_clause(raw: dict[str, Any]) -> ExistsClause:
    return ExistsClause(view=raw["view"], on=raw["on"], where=raw.get("where"))


def _parse_predicate_node(raw: Any) -> PredicateNode:
    if isinstance(raw, str):
        return raw
    if "all" in raw:
        return AllNode(all=tuple(_parse_predicate_node(n) for n in raw["all"]))
    if "any" in raw:
        return AnyNode(any=tuple(_parse_predicate_node(n) for n in raw["any"]))
    if "not" in raw:
        return NotNode(not_=_parse_predicate_node(raw["not"]))
    if "exists" in raw:
        return ExistsNode(exists=_parse_exists_clause(raw["exists"]))
    if "not_exists" in raw:
        return NotExistsNode(not_exists=_parse_exists_clause(raw["not_exists"]))
    raise CheckValidationError(  # pragma: no cover -- the schema already rejects this shape
        f"unrecognized predicate node: {raw!r}"
    )


def _parse_param_def(raw: dict[str, Any]) -> ParamDef:
    default_raw = raw["default"]
    default = ParamDefault(
        strategy=default_raw["strategy"],
        value=default_raw.get("value"),
        measure=default_raw.get("measure"),
        p=default_raw.get("p"),
        fallback=default_raw.get("fallback"),
    )
    return ParamDef(type=raw["type"], default=default)


def parse_check_document(text: str) -> CheckDoc:
    """Parse+validate a YAML check document. Raises `CheckValidationError` on
    any structural (JSON Schema) violation -- the schema is authoritative for
    shape; this function never second-guesses it, only builds the typed
    model once the schema has already accepted the document."""
    raw = yaml.safe_load(text)
    try:
        jsonschema.validate(instance=raw, schema=_load_schema())
    except jsonschema.ValidationError as exc:
        raise CheckValidationError(str(exc)) from exc

    entity_raw = raw["entity"]
    entity = EntityDef(
        view=entity_raw["view"],
        key=tuple(entity_raw["key"]),
        practice_column=entity_raw["practice_column"],
        base_filters=tuple(entity_raw["base_filters"]),
    )
    params = {name: _parse_param_def(p) for name, p in raw["params"].items()}
    return CheckDoc(
        id=raw["id"],
        title=raw["title"],
        category=raw["category"],
        default_severity=raw["default_severity"],
        entity=entity,
        params=params,
        prerequisites=tuple(raw["prerequisites"]),
        predicate=_parse_predicate_node(raw["predicate"]),
        evidence=tuple(raw["evidence"]),
        actions=tuple(raw["actions"]),
        resolution=raw["resolution"],
    )


# --- semantic catalog index ---------------------------------------------------


class CatalogIndex:
    """Read-only view/column lookup over a semantic-catalog dict (Phase 1).
    Pure in-memory index -- no DB access; assumes the catalog is already
    schema-valid (D-017 is the authority on that, not this class)."""

    def __init__(self, catalog: dict[str, Any]) -> None:
        self._columns_by_view: dict[str, frozenset[str]] = {
            view["qualified_name"]: frozenset(c["column_name"] for c in view["columns"])
            for view in catalog["views"]
        }

    def has_view(self, qualified_name: str) -> bool:
        return qualified_name in self._columns_by_view

    def has_column(self, qualified_name: str, column: str) -> bool:
        return column in self._columns_by_view.get(qualified_name, frozenset())


# --- semantic (F2) validation --------------------------------------------------


def _extract_param_refs(expr: str) -> frozenset[str]:
    return frozenset(_PARAM_REF.findall(expr))


def _extract_columns(expr: str) -> list[tuple[str | None, str]]:
    """Return `(view_or_None, column_name)` pairs referenced in a leaf
    expression -- `view` is `None` for an unqualified column."""
    substituted = _PARAM_REF.sub("1", expr)
    try:
        parsed = sqlglot.parse_one(substituted, read="tsql")
    except ParseError as exc:
        raise CheckReferenceError(f"could not parse expression '{expr}': {exc}") from exc
    refs = []
    for column in parsed.find_all(exp.Column):
        view = f"{column.db}.{column.table}" if column.db and column.table else None
        refs.append((view, column.name))
    return refs


def _require_known_params(expr: str, known_params: set[str]) -> None:
    for param in _extract_param_refs(expr):
        if param not in known_params:
            raise CheckReferenceError(f"unknown param '{{{param}}}' referenced in expression")


def _require_known_columns(expr: str, default_view: str, catalog: CatalogIndex) -> None:
    for view, column in _extract_columns(expr):
        target_view = view or default_view
        if view is not None and not catalog.has_view(target_view):
            raise CheckReferenceError(f"unknown view '{target_view}'")
        if not catalog.has_column(target_view, column):
            raise CheckReferenceError(f"unknown column '{column}' on view '{target_view}'")


def _validate_expression(
    expr: str, default_view: str, catalog: CatalogIndex, known_params: set[str]
) -> None:
    _require_known_params(expr, known_params)
    _require_known_columns(expr, default_view, catalog)


def _collect_joined_views(node: PredicateNode, out: set[str]) -> None:
    if isinstance(node, AllNode):
        for child in node.all:
            _collect_joined_views(child, out)
    elif isinstance(node, AnyNode):
        for child in node.any:
            _collect_joined_views(child, out)
    elif isinstance(node, NotNode):
        _collect_joined_views(node.not_, out)
    elif isinstance(node, ExistsNode):
        out.add(node.exists.view)
    elif isinstance(node, NotExistsNode):
        out.add(node.not_exists.view)


def _validate_predicate_tree(
    node: PredicateNode, driving_view: str, catalog: CatalogIndex, known_params: set[str]
) -> None:
    if isinstance(node, str):
        _validate_expression(node, driving_view, catalog, known_params)
    elif isinstance(node, AllNode):
        for child in node.all:
            _validate_predicate_tree(child, driving_view, catalog, known_params)
    elif isinstance(node, AnyNode):
        for child in node.any:
            _validate_predicate_tree(child, driving_view, catalog, known_params)
    elif isinstance(node, NotNode):
        _validate_predicate_tree(node.not_, driving_view, catalog, known_params)
    else:
        clause = node.exists if isinstance(node, ExistsNode) else node.not_exists
        if not catalog.has_view(clause.view):
            raise CheckReferenceError(f"unknown view '{clause.view}'")
        # Unqualified columns inside a join clause default to the joined
        # view, not the driving view -- the natural SQL scoping for a
        # correlated subquery's own condition. In practice every checked-in
        # example fully qualifies both sides, so this fallback rarely fires.
        _validate_expression(clause.on, clause.view, catalog, known_params)
        if clause.where is not None:
            _validate_expression(clause.where, clause.view, catalog, known_params)


def validate_check_against_catalog(doc: CheckDoc, catalog: CatalogIndex) -> None:
    """Raise `CheckReferenceError` naming the first unknown view, column,
    join, param, or action found (F2). Only meaningful once
    `parse_check_document` has already confirmed structural validity."""
    if not catalog.has_view(doc.entity.view):
        raise CheckReferenceError(f"unknown view '{doc.entity.view}'")

    for column in doc.entity.key:
        if not catalog.has_column(doc.entity.view, column):
            raise CheckReferenceError(f"unknown column '{column}' on view '{doc.entity.view}'")
    if not catalog.has_column(doc.entity.view, doc.entity.practice_column):
        raise CheckReferenceError(
            f"unknown column '{doc.entity.practice_column}' on view '{doc.entity.view}'"
        )

    known_params = set(doc.params)

    for expr in doc.entity.base_filters:
        _validate_expression(expr, doc.entity.view, catalog, known_params)
    for expr in doc.prerequisites:
        _validate_expression(expr, doc.entity.view, catalog, known_params)
    _validate_predicate_tree(doc.predicate, doc.entity.view, catalog, known_params)

    joined_views: set[str] = set()
    _collect_joined_views(doc.predicate, joined_views)

    for column in doc.evidence:
        on_driving_view = catalog.has_column(doc.entity.view, column)
        on_a_join = any(catalog.has_column(view, column) for view in joined_views)
        if not (on_driving_view or on_a_join):
            raise CheckReferenceError(
                f"evidence column '{column}' not found on '{doc.entity.view}' or any declared join"
            )

    for action in doc.actions:
        if action not in STUB_ACTION_LIBRARY:
            raise CheckReferenceError(f"unknown action '{action}'")


__all__ = [
    "STUB_ACTION_LIBRARY",
    "AllNode",
    "AnyNode",
    "CatalogIndex",
    "CheckDoc",
    "CheckReferenceError",
    "CheckValidationError",
    "EntityDef",
    "ExistsClause",
    "ExistsNode",
    "NotExistsNode",
    "NotNode",
    "ParamDef",
    "ParamDefault",
    "PredicateNode",
    "parse_check_document",
    "validate_check_against_catalog",
]
