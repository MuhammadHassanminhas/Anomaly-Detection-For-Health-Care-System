"""Phase 4 step 3: the review-gate CLI (F3 -- "the one gate"). DB-gated
tests require CDSS_APP_DB_URL and skip (never fail) otherwise -- D-009.1;
`test_dry_run_*` additionally requires the LocalDB fixture (D-026). The
static bypass test needs neither -- it only greps source files.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pyodbc
import pytest
import sqlalchemy as sa

from cdss.dsl import ParamDef, ParamDefault
from cdss.review import (
    CheckDetail,
    amend_check,
    approve_check,
    compiled_sql,
    default_param_value,
    dry_run,
    dry_run_params,
    get_check_detail,
    list_checks,
    reject_check,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

_SIMPLE_DEFINITION = {
    "id": "review-gate-fixture-check",
    "title": "Invoice has a negative total amount",
    "category": "data-quality",
    "default_severity": "high",
    "entity": {
        "view": "fqb.Invoices",
        "key": ["InvoiceTransactionID"],
        "practice_column": "PracticeID",
        "base_filters": ["IsDeleted = 0"],
    },
    "params": {},
    "prerequisites": ["TotalAmount IS NOT NULL"],
    "predicate": "TotalAmount < 0",
    "evidence": ["InvoiceTransactionID", "TotalAmount", "InvoiceDate", "PracticeID"],
    "actions": ["flag-for-data-steward-review"],
    "resolution": (
        "TotalAmount is corrected to a non-negative value, or the finding is dismissed "
        "with a reason."
    ),
}


_BROKEN_SELF_JOIN_DEFINITION = {
    "id": "review-gate-broken-check",
    "title": "Cancelled appointment without follow-up flag",
    "category": "workflow",
    "default_severity": "high",
    "entity": {
        "view": "dbo.Appointments",
        "key": ["AppointmentID"],
        "practice_column": "PracticeID",
        "base_filters": ["IsDeleted = 0"],
    },
    "params": {},
    "prerequisites": ["CancelledTime IS NOT NULL"],
    "predicate": {
        "all": [
            "AppointmentStatus = 'Cancelled'",
            {
                "not_exists": {
                    "on": "dbo.Appointments.AppointmentID = dbo.Appointments.CancelledTime",
                    "view": "dbo.Appointments",
                }
            },
        ]
    },
    "evidence": ["AppointmentID", "AppointmentStatus", "CancelledTime", "PracticeID"],
    "actions": ["raise-recall-task"],
    "resolution": "Ensure follow-up on cancelled appointments is documented.",
}


def _seed_draft_check(conn: sa.Connection, *, slug: str = "review-gate-fixture-check") -> str:
    definition = {**_SIMPLE_DEFINITION, "id": slug}
    row = conn.execute(
        sa.text(
            "INSERT INTO checks (slug, title, category, default_severity, source, status) "
            "VALUES (:slug, :title, 'data-quality', 'medium', 'profiling', 'draft') "
            "RETURNING id"
        ),
        {"slug": slug, "title": definition["title"]},
    ).one()
    check_id = str(row.id)
    conn.execute(
        sa.text(
            "INSERT INTO check_versions "
            "(check_id, version_number, definition, definition_hash, rationale, "
            "affected_views, params_schema) "
            "VALUES (:check_id, 1, CAST(:definition AS jsonb), 'hash', 'evidence', "
            "ARRAY['fqb.Invoices']::text[], CAST('{}' AS jsonb))"
        ),
        {"check_id": check_id, "definition": json.dumps(definition)},
    )
    return check_id


# --- pure ---------------------------------------------------------------------


def test_default_param_value_fixed_strategy_returns_its_value() -> None:
    param = ParamDef(type="integer", default=ParamDefault(strategy="fixed", value=60))
    assert default_param_value(param) == 60


def test_default_param_value_percentile_strategy_returns_fallback() -> None:
    param = ParamDef(
        type="integer",
        default=ParamDefault(strategy="percentile", measure="m", p=95, fallback=7),
    )
    assert default_param_value(param) == 7


def test_dry_run_params_excludes_array_typed_params() -> None:
    from cdss.dsl import check_doc_from_dict

    definition = {
        **_SIMPLE_DEFINITION,
        "params": {
            "stale_days": {"type": "integer", "default": {"strategy": "fixed", "value": 60}},
            "valid_codes": {
                "type": "array",
                "default": {"strategy": "fixed", "value": ["A", "B"]},
            },
        },
        "predicate": "TotalAmount < 0",
    }
    doc = check_doc_from_dict(definition)
    params = dry_run_params(doc)
    assert params == {"stale_days": 60}


def test_compiled_sql_produces_the_predicate() -> None:
    detail = CheckDetail(
        check_id="00000000-0000-0000-0000-000000000000",
        slug="review-gate-fixture-check",
        title="Fixture invoice has a negative total amount",
        category="data-quality",
        default_severity="medium",
        status="draft",
        version_id="00000000-0000-0000-0000-000000000001",
        version_number=1,
        definition=_SIMPLE_DEFINITION,
        rationale="evidence",
        affected_views=["fqb.Invoices"],
    )
    sql_text = compiled_sql(detail)
    assert "TotalAmount < 0" in sql_text
    assert "FROM fqb.Invoices" in sql_text


# --- static bypass proof (F3: no path from draft to active outside cdss.review) --


def test_no_other_module_writes_checks_status_to_active() -> None:
    pattern = re.compile(r"UPDATE\s+checks\b", re.IGNORECASE)
    offenders = []
    for path in (_REPO_ROOT / "src" / "cdss").rglob("*.py"):
        if path.name == "review.py":
            continue
        text = path.read_text(encoding="utf-8")
        if pattern.search(text):
            offenders.append(str(path))
    assert offenders == [], f"found a checks-status write outside cdss.review: {offenders}"


# --- DB-gated (app DB only) ---------------------------------------------------


def test_list_checks_includes_a_seeded_draft(conn: sa.Connection) -> None:
    _seed_draft_check(conn, slug="review-gate-list-check")
    summaries = list_checks(conn, status="draft")
    assert any(s.slug == "review-gate-list-check" for s in summaries)


def test_get_check_detail_returns_definition_and_rationale(conn: sa.Connection) -> None:
    _seed_draft_check(conn, slug="review-gate-detail-check")
    detail = get_check_detail(conn, "review-gate-detail-check")
    assert detail.status == "draft"
    assert detail.definition["id"] == "review-gate-detail-check"
    assert detail.rationale == "evidence"
    assert detail.affected_views == ["fqb.Invoices"]


def test_get_check_detail_raises_for_unknown_slug(conn: sa.Connection) -> None:
    with pytest.raises(ValueError, match="no check with slug"):
        get_check_detail(conn, "does-not-exist")


def test_approve_check_sets_active_and_records_reviewer(
    conn: sa.Connection, fixture_conn: pyodbc.Connection
) -> None:
    # approve_check now runs the check's fixture dry-run before activating it
    # (Phase 4 step 5) -- needs the fixture DB too, not just the app DB.
    _seed_draft_check(conn, slug="review-gate-approve-check")
    approve_check(conn, "review-gate-approve-check", reviewer="dr-smith", note="looks right")
    detail = get_check_detail(conn, "review-gate-approve-check")
    assert detail.status == "active"
    row = conn.execute(
        sa.text("SELECT reviewed_by, review_note FROM check_versions WHERE id = :id"),
        {"id": detail.version_id},
    ).one()
    assert row.reviewed_by == "dr-smith"
    assert row.review_note == "looks right"


def test_approve_check_raises_when_not_in_draft(
    conn: sa.Connection, fixture_conn: pyodbc.Connection
) -> None:
    _seed_draft_check(conn, slug="review-gate-double-approve")
    approve_check(conn, "review-gate-double-approve", reviewer="dr-smith")
    with pytest.raises(ValueError, match="not in draft"):
        approve_check(conn, "review-gate-double-approve", reviewer="dr-jones")


def test_reject_check_requires_a_reason(conn: sa.Connection) -> None:
    _seed_draft_check(conn, slug="review-gate-reject-check")
    with pytest.raises(ValueError, match="mandatory"):
        reject_check(conn, "review-gate-reject-check", reviewer="dr-smith", reason="   ")


def test_reject_check_sets_rejected_and_records_reason(conn: sa.Connection) -> None:
    _seed_draft_check(conn, slug="review-gate-reject-check-2")
    reject_check(
        conn, "review-gate-reject-check-2", reviewer="dr-smith", reason="wrong domain evidence"
    )
    detail = get_check_detail(conn, "review-gate-reject-check-2")
    assert detail.status == "rejected"
    row = conn.execute(
        sa.text("SELECT review_note FROM check_versions WHERE id = :id"), {"id": detail.version_id}
    ).one()
    assert row.review_note == "wrong domain evidence"


def test_amend_check_requires_a_note(conn: sa.Connection) -> None:
    _seed_draft_check(conn, slug="review-gate-amend-no-note")
    with pytest.raises(ValueError, match="mandatory"):
        amend_check(
            conn,
            "review-gate-amend-no-note",
            new_definition=_SIMPLE_DEFINITION,
            affected_views=["fqb.Invoices"],
            reviewer="dr-smith",
            note="  ",
        )


def test_amend_check_creates_a_new_version_and_resets_to_draft(
    conn: sa.Connection, fixture_conn: pyodbc.Connection
) -> None:
    _seed_draft_check(conn, slug="review-gate-amend-check")
    approve_check(conn, "review-gate-amend-check", reviewer="dr-smith")
    amended_definition = {**_SIMPLE_DEFINITION, "id": "review-gate-amend-check"}
    amended_definition["predicate"] = "TotalAmount < -0.01"
    version_id = amend_check(
        conn,
        "review-gate-amend-check",
        new_definition=amended_definition,
        affected_views=["fqb.Invoices"],
        reviewer="dr-smith",
        note="tightened the threshold",
    )
    detail = get_check_detail(conn, "review-gate-amend-check")
    assert detail.status == "draft"
    assert detail.version_number == 2
    assert detail.version_id == version_id
    assert detail.definition["predicate"] == "TotalAmount < -0.01"
    row = conn.execute(
        sa.text("SELECT review_note, reviewed_by FROM check_versions WHERE id = :id"),
        {"id": version_id},
    ).one()
    assert "tightened the threshold" in row.review_note
    assert row.reviewed_by is None


# --- DB-gated (app DB + fixture DB) -------------------------------------------


def test_dry_run_against_the_fixture_matches_known_counts(
    conn: sa.Connection, fixture_conn: pyodbc.Connection
) -> None:
    # Phase 4 steps 5-6 widened the fixture DB with extra fqb.Invoices rows
    # -- more passes for this check too.
    _seed_draft_check(conn, slug="review-gate-dry-run-check")
    detail = get_check_detail(conn, "review-gate-dry-run-check")
    result = dry_run(detail)
    assert result.status == "ok"
    assert result.rows_examined == 17
    assert result.n_fail == 1
    assert result.n_pass == 15
    assert result.n_indeterminate == 1


def test_approve_check_refuses_a_check_that_fails_its_fixture_test(
    conn: sa.Connection, fixture_conn: pyodbc.Connection
) -> None:
    """The self-referencing exists/not_exists check (same defect
    tests/test_fixture_suite.py catalogs) cannot clear the fixture-test bar
    -- approve_check must refuse it, leaving it in draft."""
    row = conn.execute(
        sa.text(
            "INSERT INTO checks (slug, title, category, default_severity, source, status) "
            "VALUES ('review-gate-broken-self-join-check', :title, :category, :severity, "
            "'llm', 'draft') RETURNING id"
        ),
        {
            "title": _BROKEN_SELF_JOIN_DEFINITION["title"],
            "category": _BROKEN_SELF_JOIN_DEFINITION["category"],
            "severity": _BROKEN_SELF_JOIN_DEFINITION["default_severity"],
        },
    ).one()
    check_id = str(row.id)
    conn.execute(
        sa.text(
            "INSERT INTO check_versions "
            "(check_id, version_number, definition, definition_hash, rationale, "
            "affected_views, params_schema) "
            "VALUES (:check_id, 1, CAST(:definition AS jsonb), 'hash', 'evidence', "
            "ARRAY['dbo.Appointments']::text[], CAST('{}' AS jsonb))"
        ),
        {"check_id": check_id, "definition": json.dumps(_BROKEN_SELF_JOIN_DEFINITION)},
    )

    with pytest.raises(ValueError, match="failed its fixture test"):
        approve_check(conn, "review-gate-broken-self-join-check", reviewer="dr-smith")

    detail = get_check_detail(conn, "review-gate-broken-self-join-check")
    assert detail.status == "draft"  # refused before any status write
