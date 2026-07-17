"""Phase 4 step 3: the review-gate CLI (F3 -- "the one gate"). `python -m
cdss.review` is the only application code path that can move a check from
`draft` to `active` (or to `rejected`, or amend it into a new version) --
every write here is scoped to `checks.status = 'draft'` rows, so a check
that has already left draft (active/rejected/retired) cannot be silently
re-approved through this same path. Direct SQL against the app DB is the
only bypass, and it always will be -- that's an operator/ops-access concern,
not something application code can prevent; `tests/test_review_gate.py`
proves no *other* module in this codebase writes `checks.status`.

`amend` always resets `checks.status` back to `draft`: a changed definition
has not been reviewed yet, regardless of what the check's status was before
the amendment, so it must re-enter the same gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from typing import Any

import pyodbc
import sqlalchemy as sa

from cdss.app_db import load_app_db_url
from cdss.check_registry import LoadedCheck
from cdss.compiler import compile_check
from cdss.dsl import (
    AllNode,
    AnyNode,
    CheckDoc,
    NotNode,
    ParamDef,
    PredicateNode,
    check_doc_from_dict,
)
from cdss.executor import CheckExecutionResult, execute_check
from cdss.source import AuditedSourceConnection

_FIXTURE_CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=(localdb)\\MSSQLLocalDB;DATABASE=cdss_fixture;"
    "Trusted_Connection=yes;"
)

_DRY_RUN_PRACTICE_ID = "review-dry-run"


@dataclass(frozen=True)
class CheckSummary:
    check_id: str
    slug: str
    title: str
    category: str
    default_severity: str
    status: str
    version_id: str
    version_number: int


@dataclass(frozen=True)
class CheckDetail(CheckSummary):
    definition: dict[str, Any]
    rationale: str | None
    affected_views: list[str]


_LATEST_VERSION_CTE = """
    WITH latest_versions AS (
        SELECT DISTINCT ON (check_id) *
        FROM check_versions
        ORDER BY check_id, version_number DESC
    )
"""

_LIST_SQL = sa.text(
    _LATEST_VERSION_CTE
    + """
    SELECT c.id AS check_id, c.slug, c.title, c.category, c.default_severity, c.status,
           v.id AS version_id, v.version_number
    FROM checks c
    JOIN latest_versions v ON v.check_id = c.id
    WHERE c.status = :status
    ORDER BY c.slug
    """
)

_DETAIL_SQL = sa.text(
    _LATEST_VERSION_CTE
    + """
    SELECT c.id AS check_id, c.slug, c.title, c.category, c.default_severity, c.status,
           v.id AS version_id, v.version_number, v.definition, v.rationale, v.affected_views
    FROM checks c
    JOIN latest_versions v ON v.check_id = c.id
    WHERE c.slug = :slug
    """
)


def list_checks(conn: sa.Connection, *, status: str = "draft") -> list[CheckSummary]:
    rows = conn.execute(_LIST_SQL, {"status": status}).mappings().all()
    return [
        CheckSummary(
            check_id=str(row["check_id"]),
            slug=row["slug"],
            title=row["title"],
            category=row["category"],
            default_severity=row["default_severity"],
            status=row["status"],
            version_id=str(row["version_id"]),
            version_number=row["version_number"],
        )
        for row in rows
    ]


def get_check_detail(conn: sa.Connection, slug: str) -> CheckDetail:
    row = conn.execute(_DETAIL_SQL, {"slug": slug}).mappings().one_or_none()
    if row is None:
        raise ValueError(f"no check with slug '{slug}'")
    return CheckDetail(
        check_id=str(row["check_id"]),
        slug=row["slug"],
        title=row["title"],
        category=row["category"],
        default_severity=row["default_severity"],
        status=row["status"],
        version_id=str(row["version_id"]),
        version_number=row["version_number"],
        definition=row["definition"],
        rationale=row["rationale"],
        affected_views=list(row["affected_views"]),
    )


def compiled_sql(detail: CheckDetail) -> str:
    doc = check_doc_from_dict(detail.definition)
    return compile_check(doc).sql_text


def default_param_value(param: ParamDef) -> Any:
    """The value a draft's dry-run binds for a scalar param -- no practice
    has calibrated anything for a not-yet-approved check, so a `fixed`
    param uses its own static value and a `percentile` param uses its
    declared `fallback` (F4's own "used until the practice has enough
    data" semantics, applied here at review time too)."""
    if param.default.strategy == "fixed":
        return param.default.value
    return param.default.fallback


def dry_run_params(doc: CheckDoc) -> dict[str, Any]:
    """Array-typed params are never bound here -- the compiler expands them
    into per-element named params from the doc's own fixed default
    (cdss.compiler._expand_array_params / cdss.executor._array_element_bindings),
    so there is no bare `@name` left for an array param to bind to."""
    return {name: default_param_value(p) for name, p in doc.params.items() if p.type != "array"}


def dry_run(detail: CheckDetail) -> CheckExecutionResult:
    """Execute `detail`'s definition against the fixture SQL Server (D-026,
    the same disposable synthetic instance every other phase's live proofs
    use) with no practice overrides -- the review-time "does this even run,
    and what does it find on known data" proof the phase spec calls for.
    Never raises for a source-execution failure (a draft referencing a view
    the fixture doesn't have, for instance) -- `execute_check` already
    degrades that to `status='error'`, which the CLI displays plainly."""
    doc = check_doc_from_dict(detail.definition)
    connection = pyodbc.connect(_FIXTURE_CONN_STR, timeout=3, autocommit=True)
    try:
        source_conn = AuditedSourceConnection(
            connection,
            component="review",
            allowed_objects=frozenset(view.lower() for view in detail.affected_views),
        )
        loaded_check = LoadedCheck(
            check_id=detail.check_id,
            slug=detail.slug,
            title=detail.title,
            category=detail.category,
            default_severity=detail.default_severity,
            check_version_id=detail.version_id,
            version_number=detail.version_number,
            definition=detail.definition,
            definition_hash=hashlib.sha256(
                json.dumps(detail.definition, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            affected_views=detail.affected_views,
            params_schema={name: p.type for name, p in doc.params.items()},
            practice_id=_DRY_RUN_PRACTICE_ID,
            enabled=True,
            demoted=False,
            params=dry_run_params(doc),
            params_source="default",
        )
        return execute_check(source_conn, loaded_check)
    finally:
        connection.close()


def _predicate_has_leaf(node: PredicateNode) -> bool:
    """True if `node`'s tree contains at least one bare leaf-expression
    string -- the only kind of predicate node that can evaluate SQL `NULL`.
    An `exists`/`not_exists` clause never can: `EXISTS`/`NOT EXISTS` is
    always two-valued in T-SQL."""
    if isinstance(node, str):
        return True
    if isinstance(node, AllNode):
        return any(_predicate_has_leaf(child) for child in node.all)
    if isinstance(node, AnyNode):
        return any(_predicate_has_leaf(child) for child in node.any)
    if isinstance(node, NotNode):
        return _predicate_has_leaf(node.not_)
    return False


def can_reach_indeterminate(doc: CheckDoc) -> bool:
    """F6: indeterminate is only reachable via a `NULL`/`FALSE` prerequisite
    or a `NULL`-producing leaf comparison inside the predicate -- a bare
    `exists`/`not_exists` node with empty `prerequisites` can never produce
    one. Product-owner directive (2026-07-17, Phase 4 step 5): the fixture
    gate below only requires a check's dry-run to reach `indeterminate` when
    the check's own structure can actually produce it -- a pure
    `exists`/`not_exists` check is a legitimate DSL shape, not a defect."""
    return bool(doc.prerequisites) or _predicate_has_leaf(doc.predicate)


def check_passes_fixture_test(result: CheckExecutionResult, doc: CheckDoc) -> bool:
    """The fixture-test bar a draft must clear before `approve_check` will
    activate it (Phase 4 step 5: "a check without a passing fixture test
    cannot be approved"): the dry-run must complete (`status == 'ok'`) and
    reach both `fail` and `pass`; `indeterminate` is required only when
    `can_reach_indeterminate(doc)` says the check's own structure permits it."""
    if result.status != "ok" or result.n_fail < 1 or result.n_pass < 1:
        return False
    return not (can_reach_indeterminate(doc) and result.n_indeterminate < 1)


_APPROVE_CHECK_SQL = sa.text(
    "UPDATE checks SET status = 'active', updated_at = now() "
    "WHERE slug = :slug AND status = 'draft' RETURNING id"
)
_REJECT_CHECK_SQL = sa.text(
    "UPDATE checks SET status = 'rejected', updated_at = now() "
    "WHERE slug = :slug AND status = 'draft' RETURNING id"
)
_RECORD_REVIEW_SQL = sa.text(
    "UPDATE check_versions SET reviewed_by = :reviewer, reviewed_at = now(), "
    "review_note = :note WHERE id = :version_id"
)


def approve_check(
    conn: sa.Connection, slug: str, *, reviewer: str, note: str | None = None
) -> None:
    """The only application code path that can set `checks.status =
    'active'` -- restricted to a currently-`draft` check (F3: nothing else
    in this codebase writes this transition, see tests/test_review_gate.py),
    and further gated on a passing fixture test (Phase 4 step 5): a check
    whose dry-run against the fixture DB doesn't reach `check_passes_fixture_test`'s
    bar is refused outright, before any status write happens."""
    detail = get_check_detail(conn, slug)
    if detail.status != "draft":
        raise ValueError(f"check '{slug}' is not in draft (status={detail.status!r})")
    result = dry_run(detail)
    doc = check_doc_from_dict(detail.definition)
    if not check_passes_fixture_test(result, doc):
        raise ValueError(
            f"check '{slug}' failed its fixture test: status={result.status}, "
            f"n_fail={result.n_fail}, n_pass={result.n_pass}, "
            f"n_indeterminate={result.n_indeterminate} "
            f"(can_reach_indeterminate={can_reach_indeterminate(doc)})"
        )
    row = conn.execute(_APPROVE_CHECK_SQL, {"slug": slug}).one_or_none()
    if row is None:
        raise ValueError(f"check '{slug}' is not in draft (status={detail.status!r})")
    conn.execute(
        _RECORD_REVIEW_SQL, {"reviewer": reviewer, "note": note, "version_id": detail.version_id}
    )


def reject_check(conn: sa.Connection, slug: str, *, reviewer: str, reason: str) -> None:
    if not reason.strip():
        raise ValueError("a rejection reason is mandatory")
    detail = get_check_detail(conn, slug)
    row = conn.execute(_REJECT_CHECK_SQL, {"slug": slug}).one_or_none()
    if row is None:
        raise ValueError(f"check '{slug}' is not in draft (status={detail.status!r})")
    conn.execute(
        _RECORD_REVIEW_SQL, {"reviewer": reviewer, "note": reason, "version_id": detail.version_id}
    )


_RESET_TO_DRAFT_SQL = sa.text(
    "UPDATE checks SET status = 'draft', updated_at = now() WHERE id = :check_id"
)

_INSERT_AMENDED_VERSION_SQL = sa.text(
    "INSERT INTO check_versions "
    "(check_id, version_number, definition, definition_hash, rationale, "
    "affected_views, params_schema, review_note) "
    "VALUES (:check_id, :version_number, CAST(:definition AS jsonb), :definition_hash, "
    ":rationale, :affected_views, CAST(:params_schema AS jsonb), :note) "
    "RETURNING id"
)


def amend_check(
    conn: sa.Connection,
    slug: str,
    *,
    new_definition: dict[str, Any],
    affected_views: list[str],
    reviewer: str,
    note: str,
) -> str:
    """Insert a new, immutable `check_versions` row (never mutate the prior
    one) and reset `checks.status` to `draft` -- an amended definition has
    not itself been reviewed, whatever the check's status was before. The
    prior version's `rationale` (the generator's own evidence citation) is
    carried forward unchanged; `note` (mandatory) records what the reviewer
    changed and why, distinct from that original evidence."""
    if not note.strip():
        raise ValueError("an amendment note is mandatory")
    detail = get_check_detail(conn, slug)
    doc = check_doc_from_dict(new_definition)
    definition_json = json.dumps(new_definition, sort_keys=True)
    row = conn.execute(
        _INSERT_AMENDED_VERSION_SQL,
        {
            "check_id": detail.check_id,
            "version_number": detail.version_number + 1,
            "definition": definition_json,
            "definition_hash": hashlib.sha256(definition_json.encode("utf-8")).hexdigest(),
            "rationale": detail.rationale,
            "affected_views": affected_views,
            "params_schema": json.dumps({name: p.type for name, p in doc.params.items()}),
            "note": f"{note} (amended by {reviewer})",
        },
    ).one()
    conn.execute(_RESET_TO_DRAFT_SQL, {"check_id": detail.check_id})
    return str(row.id)


# --- CLI ---------------------------------------------------------------------


def _cmd_list(conn: sa.Connection, args: argparse.Namespace) -> None:
    for summary in list_checks(conn, status=args.status):
        print(f"{summary.slug}\t{summary.category}\t{summary.default_severity}\t{summary.title}")


def _cmd_show(conn: sa.Connection, args: argparse.Namespace) -> None:
    detail = get_check_detail(conn, args.slug)
    print(f"slug: {detail.slug}  status: {detail.status}  version: {detail.version_number}")
    print(f"title: {detail.title}")
    print(f"rationale: {detail.rationale}")
    print("definition:")
    print(json.dumps(detail.definition, indent=2, sort_keys=True))
    print("compiled SQL:")
    print(compiled_sql(detail))
    print("fixture dry-run:")
    result = dry_run(detail)
    print(
        f"  status={result.status} rows_examined={result.rows_examined} "
        f"pass={result.n_pass} fail={result.n_fail} indeterminate={result.n_indeterminate}"
    )
    if result.error_message:
        print(f"  error: {result.error_message}")


def _cmd_approve(conn: sa.Connection, args: argparse.Namespace) -> None:
    approve_check(conn, args.slug, reviewer=args.reviewer, note=args.note)
    print(f"approved '{args.slug}'")


def _cmd_reject(conn: sa.Connection, args: argparse.Namespace) -> None:
    reject_check(conn, args.slug, reviewer=args.reviewer, reason=args.reason)
    print(f"rejected '{args.slug}'")


def _cmd_amend(conn: sa.Connection, args: argparse.Namespace) -> None:
    with open(args.definition_file, encoding="utf-8") as handle:
        new_definition = json.load(handle)
    doc = check_doc_from_dict(new_definition)
    affected_views = [doc.entity.view]
    version_id = amend_check(
        conn,
        args.slug,
        new_definition=new_definition,
        affected_views=affected_views,
        reviewer=args.reviewer,
        note=args.note,
    )
    print(f"amended '{args.slug}' -> new check_version {version_id}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m cdss.review")
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="list checks by status (default: draft)")
    list_parser.add_argument("--status", default="draft")
    list_parser.set_defaults(func=_cmd_list)

    show_parser = sub.add_parser(
        "show", help="show a check's definition, rationale, compiled SQL, dry-run"
    )
    show_parser.add_argument("slug")
    show_parser.set_defaults(func=_cmd_show)

    approve_parser = sub.add_parser("approve", help="approve a draft check (-> active)")
    approve_parser.add_argument("slug")
    approve_parser.add_argument("--reviewer", required=True)
    approve_parser.add_argument("--note")
    approve_parser.set_defaults(func=_cmd_approve)

    reject_parser = sub.add_parser("reject", help="reject a draft check (-> rejected)")
    reject_parser.add_argument("slug")
    reject_parser.add_argument("--reviewer", required=True)
    reject_parser.add_argument("--reason", required=True)
    reject_parser.set_defaults(func=_cmd_reject)

    amend_parser = sub.add_parser(
        "amend", help="amend a check's definition (new check_version, -> draft)"
    )
    amend_parser.add_argument("slug")
    amend_parser.add_argument("--definition-file", required=True)
    amend_parser.add_argument("--reviewer", required=True)
    amend_parser.add_argument("--note", required=True)
    amend_parser.set_defaults(func=_cmd_amend)

    return parser


def main() -> int:
    args = _build_parser().parse_args()
    engine = sa.create_engine(load_app_db_url())
    try:
        with engine.begin() as conn:
            args.func(conn, args)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CheckDetail",
    "CheckSummary",
    "amend_check",
    "approve_check",
    "can_reach_indeterminate",
    "check_passes_fixture_test",
    "compiled_sql",
    "default_param_value",
    "dry_run",
    "dry_run_params",
    "get_check_detail",
    "list_checks",
    "reject_check",
]
