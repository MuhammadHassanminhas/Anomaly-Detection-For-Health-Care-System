"""Phase 5 step 1: `fallback_template` migration (0004) + wiring proof.
DB-gated: app-DB tests require CDSS_APP_DB_URL, the one fixture-evidence
test additionally requires the LocalDB fixture (D-026) -- both skip (never
fail) otherwise, D-009.1.
"""

from __future__ import annotations

import json

import pyodbc
import pytest
import sqlalchemy as sa

from cdss.narrate import render_fallback
from cdss.review import CheckDetail, dry_run

_DEFAULT_FALLBACK_TEMPLATE = "This check has flagged a record for manual review."

_INVOICE_DEFINITION = {
    "id": "narration-fallback-fixture-check",
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


def _insert_check_version(
    conn: sa.Connection, *, slug: str, fallback_template: str | None
) -> tuple[str, str]:
    check_id = conn.execute(
        sa.text(
            "INSERT INTO checks (slug, title, category, default_severity, source, status) "
            "VALUES (:slug, :title, :category, :severity, 'manual', 'draft') RETURNING id"
        ),
        {
            "slug": slug,
            "title": _INVOICE_DEFINITION["title"],
            "category": _INVOICE_DEFINITION["category"],
            "severity": _INVOICE_DEFINITION["default_severity"],
        },
    ).scalar_one()
    columns = (
        "check_id, version_number, definition, definition_hash, rationale, "
        "affected_views, params_schema"
    )
    values = (
        ":check_id, 1, CAST(:definition AS jsonb), 'hash', 'evidence', "
        ":affected_views, CAST('{}' AS jsonb)"
    )
    params: dict[str, object] = {
        "check_id": check_id,
        "definition": json.dumps(_INVOICE_DEFINITION),
        "affected_views": ["fqb.Invoices"],
    }
    if fallback_template is not None:
        columns += ", fallback_template"
        values += ", :fallback_template"
        params["fallback_template"] = fallback_template
    version_id = conn.execute(
        sa.text(f"INSERT INTO check_versions ({columns}) VALUES ({values}) RETURNING id"),
        params,
    ).scalar_one()
    return str(check_id), str(version_id)


# --- schema / mandatory-field proof (app DB only) ---------------------------


def test_fallback_template_defaults_when_not_specified(conn: sa.Connection) -> None:
    _, version_id = _insert_check_version(
        conn, slug="narration-fallback-default-check", fallback_template=None
    )
    stored = conn.execute(
        sa.text("SELECT fallback_template FROM check_versions WHERE id = :id"),
        {"id": version_id},
    ).scalar_one()
    assert stored == _DEFAULT_FALLBACK_TEMPLATE


def test_fallback_template_rejects_an_explicit_null(conn: sa.Connection) -> None:
    check_id = conn.execute(
        sa.text(
            "INSERT INTO checks (slug, title, category, default_severity, source, status) "
            "VALUES ('narration-fallback-null-check', 'x', 'data-quality', 'high', "
            "'manual', 'draft') RETURNING id"
        )
    ).scalar_one()
    with pytest.raises(sa.exc.IntegrityError):
        conn.execute(
            sa.text(
                "INSERT INTO check_versions "
                "(check_id, version_number, definition, definition_hash, affected_views, "
                "params_schema, fallback_template) "
                "VALUES (:check_id, 1, CAST('{}' AS jsonb), 'hash', ARRAY[]::text[], "
                "CAST('{}' AS jsonb), NULL)"
            ),
            {"check_id": check_id},
        )


def test_a_custom_fallback_template_is_stored_and_retrievable(conn: sa.Connection) -> None:
    template = "Invoice {{InvoiceTransactionID}} has a negative total of {{TotalAmount}}."
    _, version_id = _insert_check_version(
        conn, slug="narration-fallback-custom-check", fallback_template=template
    )
    stored = conn.execute(
        sa.text("SELECT fallback_template FROM check_versions WHERE id = :id"),
        {"id": version_id},
    ).scalar_one()
    assert stored == template


# --- render_fallback against real fixture-DB evidence (app DB + fixture DB) -


def test_render_fallback_against_real_fixture_evidence(
    conn: sa.Connection, fixture_conn: pyodbc.Connection
) -> None:
    template = (
        "Invoice {{InvoiceTransactionID}} at practice {{PracticeID}} has a negative "
        "total amount of {{TotalAmount}}, dated {{InvoiceDate}}."
    )
    check_id, version_id = _insert_check_version(
        conn, slug="narration-fallback-dry-run-check", fallback_template=template
    )
    detail = CheckDetail(
        check_id=check_id,
        slug="narration-fallback-dry-run-check",
        title=_INVOICE_DEFINITION["title"],
        category=_INVOICE_DEFINITION["category"],
        default_severity=_INVOICE_DEFINITION["default_severity"],
        status="draft",
        version_id=version_id,
        version_number=1,
        definition=_INVOICE_DEFINITION,
        rationale="evidence",
        affected_views=["fqb.Invoices"],
    )
    result = dry_run(detail)
    assert result.status == "ok"
    fail_rows = [row for row in result.rows if row.tri_state == "fail"]
    assert len(fail_rows) >= 1
    for row in fail_rows:
        rendered = render_fallback(template, evidence=row.evidence, params={})
        assert "{{" not in rendered
        assert str(row.evidence["InvoiceTransactionID"]) in rendered
