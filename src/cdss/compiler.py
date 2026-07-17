"""Phase 2 step 3: compiles a typed `CheckDoc` (`cdss.dsl`) into one
deterministic, parameterized T-SQL statement computing
`(entity key columns, practice_id, tri_state, evidence columns)` for the
whole increment.

Three-valued evaluation (F6) is implemented by construction, not by custom
NULL-coalescing per leaf: SQL Server's native `AND`/`OR`/`NOT` already behave
as three-valued logic (`FALSE` dominates `AND`, `TRUE` dominates `OR`, `NULL`
propagates otherwise) that matches the DSL's `all`/`any`/`not` combinators
exactly, and `CASE WHEN <cond> THEN` already treats a `NULL` condition the
same as `FALSE` (falls through to the next branch/`ELSE`). The compiler only
has to nest two `CASE` expressions in the right order:

    outer: WHEN <all prerequisites true> THEN <inner> ELSE 'indeterminate'
    inner: WHEN <predicate> THEN 'fail' WHEN NOT <predicate> THEN 'pass' ELSE 'indeterminate'

A `FALSE` outer condition and a `NULL` outer condition both fall through to
the same `ELSE` -- exactly F6's "any NULL/FALSE prerequisite => indeterminate".

Proving pass/fail/indeterminate against real fixture rows is step 5's
deliverable (a real SQL Server); this module only proves the emitted SQL's
*shape* correctly encodes the semantics -- its unit tests inspect compiled
text, never execute it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from cdss.dsl import (
    AllNode,
    AnyNode,
    CheckDoc,
    ExistsClause,
    ExistsNode,
    NotExistsNode,
    NotNode,
    ParamDef,
    PredicateNode,
)

_PARAM_REF = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class CompiledCheck:
    sql_text: str
    sql_hash: str
    params_schema: dict[str, str]


def _dsl_type_for_python_value(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    raise TypeError(f"unsupported array element value: {value!r}")


def _expand_array_params(
    params: dict[str, ParamDef],
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """A single T-SQL parameter can't be bound to a list, so `IN ({param})`
    over an `array`-typed param must become `IN (@param_0, @param_1, ...)` --
    one named parameter per element. That requires the element values to be
    known at compile time, which only a `fixed`-strategy default provides
    (per docs/dsl.md: an `in`-against-catalog-domain array is always static,
    human-reviewed at authoring time, never re-derived at compile or run
    time). Returns `(expansion_names, expanded_schema)`: `expansion_names`
    maps each array param's name to its ordered per-element T-SQL param
    names (substituted for every `{param}` occurrence); `expanded_schema`
    maps each element name to its inferred DSL scalar type, replacing the
    array param's own `params_schema` entry -- there is nothing left to bind
    a bare `@param` to once it has been expanded away."""
    expansion_names: dict[str, list[str]] = {}
    expanded_schema: dict[str, str] = {}
    for name, param in params.items():
        if param.type != "array":
            continue
        if param.default.strategy != "fixed":
            raise ValueError(
                f"array param '{name}' must use a 'fixed' default -- "
                f"'{param.default.strategy}' has no compile-time-known value list"
            )
        values = param.default.value
        if not isinstance(values, list) or not values:
            raise ValueError(f"array param '{name}' fixed default must be a non-empty list")
        element_names = [f"{name}_{i}" for i in range(len(values))]
        expansion_names[name] = element_names
        for element_name, value in zip(element_names, values, strict=True):
            expanded_schema[element_name] = _dsl_type_for_python_value(value)
    return expansion_names, expanded_schema


def _substitute_params(expr: str, array_expansions: dict[str, list[str]]) -> str:
    """`{param}` -> `@param` (T-SQL named-parameter syntax, bound by the
    Phase 3 executor -- never a literal value baked into the SQL text), or,
    for an array param, `{param}` -> `@param_0, @param_1, ...`."""

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in array_expansions:
            return ", ".join(f"@{element}" for element in array_expansions[name])
        return f"@{name}"

    return _PARAM_REF.sub(_replace, expr)


def _compile_exists_clause(clause: ExistsClause, array_expansions: dict[str, list[str]]) -> str:
    condition = f"({_substitute_params(clause.on, array_expansions)})"
    if clause.where is not None:
        condition = f"{condition} AND ({_substitute_params(clause.where, array_expansions)})"
    return f"(SELECT 1 FROM {clause.view} WHERE {condition})"


def _compile_predicate(node: PredicateNode, array_expansions: dict[str, list[str]]) -> str:
    if isinstance(node, str):
        return f"({_substitute_params(node, array_expansions)})"
    if isinstance(node, AllNode):
        children = " AND ".join(_compile_predicate(child, array_expansions) for child in node.all)
        return f"({children})"
    if isinstance(node, AnyNode):
        children = " OR ".join(_compile_predicate(child, array_expansions) for child in node.any)
        return f"({children})"
    if isinstance(node, NotNode):
        return f"(NOT {_compile_predicate(node.not_, array_expansions)})"
    if isinstance(node, ExistsNode):
        return f"EXISTS {_compile_exists_clause(node.exists, array_expansions)}"
    if isinstance(node, NotExistsNode):
        return f"NOT EXISTS {_compile_exists_clause(node.not_exists, array_expansions)}"
    raise TypeError(f"unrecognized predicate node: {node!r}")


def project_columns(doc: CheckDoc) -> tuple[list[str], list[str]]:
    """Return `(head, tail)`: `head` is entity key columns + practice_column
    (author order, deduplicated); `tail` is evidence columns not already in
    `head` (author order). Deduplication is a linear author-order scan, never
    an unordered set, so the projected column list is deterministic (D-021's
    "never a set for anything that must stay ordered" lesson, applied here).
    Public (not compiler-internal): the Phase 3 executor needs this exact
    head/tail split to know where `tri_state` sits in a result row and how
    to re-associate the rest of the row with column names."""
    head: list[str] = list(doc.entity.key)
    if doc.entity.practice_column not in head:
        head.append(doc.entity.practice_column)
    seen = set(head)
    tail: list[str] = []
    for column in doc.evidence:
        if column not in seen:
            seen.add(column)
            tail.append(column)
    return head, tail


def compile_check(doc: CheckDoc, *, watermark_column: str | None = None) -> CompiledCheck:
    """Compile `doc` to one deterministic T-SQL `SELECT`. If
    `watermark_column` is given, the `WHERE` clause is additionally scoped to
    `col > @watermark_from AND col <= @watermark_to` (named params bound by
    the Phase 3 executor); if omitted, no increment clause is emitted -- an
    un-watermarked view's fallback strategy is Phase 3's decision, not the
    compiler's (ARCHITECTURE.md §1.2)."""
    head, tail = project_columns(doc)
    array_expansions, array_expanded_schema = _expand_array_params(doc.params)
    predicate_sql = _compile_predicate(doc.predicate, array_expansions)
    prereq_condition = (
        " AND ".join(f"({_substitute_params(p, array_expansions)})" for p in doc.prerequisites)
        or "1 = 1"
    )

    tri_state_case = (
        "CASE\n"
        f"    WHEN {prereq_condition} THEN\n"
        "      CASE\n"
        f"        WHEN {predicate_sql} THEN 'fail'\n"
        f"        WHEN NOT {predicate_sql} THEN 'pass'\n"
        "        ELSE 'indeterminate'\n"
        "      END\n"
        "    ELSE 'indeterminate'\n"
        "  END"
    )

    select_lines = (
        [f"  [{column}]" for column in head]
        + [f"  {tri_state_case} AS tri_state"]
        + [f"  [{column}]" for column in tail]
    )

    where_clauses = [
        f"({_substitute_params(f, array_expansions)})" for f in doc.entity.base_filters
    ]
    params_schema: dict[str, str] = {
        name: param.type for name, param in doc.params.items() if name not in array_expansions
    }
    params_schema.update(array_expanded_schema)
    if watermark_column is not None:
        where_clauses.append(f"({watermark_column} > @watermark_from)")
        where_clauses.append(f"({watermark_column} <= @watermark_to)")
        params_schema["watermark_from"] = "datetime"
        params_schema["watermark_to"] = "datetime"

    sql_text = (
        "SELECT\n"
        + ",\n".join(select_lines)
        + f"\nFROM {doc.entity.view}"
        + ("\nWHERE " + " AND ".join(where_clauses) if where_clauses else "")
    )
    sql_hash = hashlib.sha256(sql_text.encode("utf-8")).hexdigest()
    return CompiledCheck(sql_text=sql_text, sql_hash=sql_hash, params_schema=params_schema)


__all__ = ["CompiledCheck", "compile_check", "project_columns"]
