"""Phase 3 close-out: live proof of the 6 numbered exit criteria in
`docs/phases/phase-03-app-db-executor.md`, beyond what each individual
step's own tests already cover. Criteria 1 and 2 are proven by
`scripts/check.ps1` and `tests/migrations` respectively (nothing new needed
here). This file covers 3, 4, and 6, which needed genuinely new live
scenarios; criterion 5 is covered by `tests/executor/test_run.py`'s
`render_cost_report` assertions.

Requires both CDSS_APP_DB_URL and the LocalDB fixture -- skips (never
fails) otherwise, D-009.1.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pyodbc
import pytest
import sqlalchemy as sa
import yaml
from sqlalchemy.engine import Engine

from cdss.app_db_repo import SourceAuditLogRepository, source_audit_log
from cdss.check_registry import load_active_checks
from cdss.executor import fetch_live_columns
from cdss.materialize import compute_dedupe_key
from cdss.run import WatermarkPlan, get_or_create_catalog_version, run_once
from cdss.source import AuditedSourceConnection

EXAMPLES_DIR = Path(__file__).parent.parent.parent / "examples" / "checks"
_ALLOWED_OBJECTS = frozenset({"dbo.appointments", "dbo.invoices", "fqb.invoices", "dbo.patient"})


def _seed_one_check(conn: sa.Connection, slug: str, params: dict[str, object]) -> str:
    """Seeds `practice-1` + one example check by slug. Returns the check id."""
    conn.execute(
        sa.text("INSERT INTO practices (practice_id, name) VALUES ('practice-1', 'Test Practice')")
    )
    raw = yaml.safe_load((EXAMPLES_DIR / f"{slug}.yaml").read_text(encoding="utf-8"))
    check_id = str(
        conn.execute(
            sa.text(
                "INSERT INTO checks (slug, title, category, default_severity, source, status) "
                "VALUES (:slug, :title, :category, :severity, 'manual', 'active') "
                "RETURNING id"
            ),
            {
                "slug": slug,
                "title": raw["title"],
                "category": raw["category"],
                "severity": raw["default_severity"],
            },
        )
        .one()
        .id
    )
    conn.execute(
        sa.text(
            "INSERT INTO check_versions "
            "(check_id, version_number, definition, definition_hash, "
            "affected_views, params_schema) "
            "VALUES (:check_id, 1, CAST(:definition AS jsonb), 'hash', "
            "ARRAY[:view]::text[], '{}'::jsonb)"
        ),
        {"check_id": check_id, "definition": json.dumps(raw), "view": raw["entity"]["view"]},
    )
    conn.execute(
        sa.text(
            "INSERT INTO practice_check_config (practice_id, check_id, params) "
            "VALUES ('practice-1', :check_id, CAST(:params AS jsonb))"
        ),
        {"check_id": check_id, "params": json.dumps(params)},
    )
    return check_id


@pytest.fixture
def source_conn(fixture_conn: pyodbc.Connection, tmp_path: Path) -> AuditedSourceConnection:
    class _Adapter:
        timeout = 0

        def cursor(self) -> pyodbc.Cursor:
            return fixture_conn.cursor()

    return AuditedSourceConnection(
        _Adapter(),  # type: ignore[arg-type]
        component="test-exit-criteria",
        allowed_objects=_ALLOWED_OBJECTS,
        audit_dir=tmp_path,
    )


# --- criterion 3: idempotency ------------------------------------------------


def test_criterion_3_watermarked_path_second_run_has_zero_new_findings_and_events(
    conn: sa.Connection, source_conn: AuditedSourceConnection
) -> None:
    """`ScheduleDate` is a real `DATETIME2` column on the fixture's
    `dbo.Appointments` (step 5 already reused it the same way -- "purely to
    prove the plumbing... not a claim about that view's real watermark
    semantics"). With it wired as the watermark column, a second consecutive
    run's scan window starts strictly after the first run's `now`, so none
    of the (already-existing, already-processed) fixture rows fall in it --
    genuinely zero new findings *and* zero new events, unlike the
    bounded-full-scan fallback (see `test_run.py`'s own documented tension)."""
    _seed_one_check(conn, "appointment-completed-no-invoice", {"invoice_lag_days": 7})
    checks = load_active_checks(conn)
    catalog_version_id = get_or_create_catalog_version(
        conn, sha256="c3-hash", source_path="c3-path"
    )
    watermark_plans = {"dbo.Appointments": WatermarkPlan(column="ScheduleDate")}

    report_1 = run_once(
        conn,
        source_conn,
        checks,
        catalog_version_id=catalog_version_id,
        watermark_plans=watermark_plans,
    )
    assert len(report_1.summaries) == 1
    summary_1 = report_1.summaries[0]
    assert summary_1.status == "ok"
    assert summary_1.watermark_strategy == "watermarked"
    assert summary_1.n_fail == 1
    findings_after_run_1 = conn.execute(sa.text("SELECT COUNT(*) FROM findings")).scalar()
    events_after_run_1 = conn.execute(sa.text("SELECT COUNT(*) FROM finding_events")).scalar()
    assert findings_after_run_1 == 1
    assert events_after_run_1 == 1

    report_2 = run_once(
        conn,
        source_conn,
        checks,
        catalog_version_id=catalog_version_id,
        watermark_plans=watermark_plans,
    )
    summary_2 = report_2.summaries[0]
    assert summary_2.rows_examined == 0
    assert summary_2.n_fail == 0

    findings_after_run_2 = conn.execute(sa.text("SELECT COUNT(*) FROM findings")).scalar()
    events_after_run_2 = conn.execute(sa.text("SELECT COUNT(*) FROM finding_events")).scalar()
    assert findings_after_run_2 == findings_after_run_1
    assert events_after_run_2 == events_after_run_1


# --- criterion 4: pass -> fail -> pass lifecycle -----------------------------


@pytest.fixture
def _temp_invoice_row(fixture_conn: pyodbc.Connection) -> Iterator[int]:
    """A dedicated fqb.Invoices_Base row (id 999, well outside the 1-7 range
    every other fixture-DB test hardcodes counts against), inserted passing
    and cleaned up unconditionally -- other tests' row-count assertions must
    never see it."""
    invoice_id = 999
    cursor = fixture_conn.cursor()
    cursor.execute(
        "INSERT INTO fqb.Invoices_Base "
        "(InvoiceTransactionID, PatientID, InvoiceDate, UnpaidAmount, TotalAmount, "
        "PracticeID, IsDeleted, IsActive) "
        "VALUES (?, 1, SYSDATETIME(), 0.00, 100.00, 100, 0, 1)",
        [invoice_id],
    )
    try:
        yield invoice_id
    finally:
        cursor.execute("DELETE FROM fqb.Invoices_Base WHERE InvoiceTransactionID = ?", [invoice_id])


def test_criterion_4_pass_fail_pass_traverses_open_to_resolved_system(
    conn: sa.Connection,
    source_conn: AuditedSourceConnection,
    fixture_conn: pyodbc.Connection,
    _temp_invoice_row: int,
) -> None:
    check_id = _seed_one_check(conn, "invoice-negative-total-amount", {})
    checks = load_active_checks(conn)
    catalog_version_id = get_or_create_catalog_version(
        conn, sha256="c4-hash", source_path="c4-path"
    )
    dedupe_key = compute_dedupe_key(check_id, {"InvoiceTransactionID": _temp_invoice_row})
    cursor = fixture_conn.cursor()

    def _finding() -> sa.Row[tuple[object, ...]] | None:
        return conn.execute(
            sa.text(
                "SELECT * FROM findings WHERE check_id = :check_id AND dedupe_key = :dedupe_key"
            ),
            {"check_id": check_id, "dedupe_key": dedupe_key},
        ).one_or_none()

    def _events(finding_id: object) -> list[str]:
        rows = conn.execute(
            sa.text("SELECT event FROM finding_events WHERE finding_id = :id ORDER BY occurred_at"),
            {"id": finding_id},
        ).all()
        return [r.event for r in rows]

    # run 1: passing -- no finding.
    run_once(conn, source_conn, checks, catalog_version_id=catalog_version_id, watermark_plans={})
    assert _finding() is None

    # run 2: now failing -- finding created, open.
    cursor.execute(
        "UPDATE fqb.Invoices_Base SET TotalAmount = -50.00 WHERE InvoiceTransactionID = ?",
        [_temp_invoice_row],
    )
    run_once(conn, source_conn, checks, catalog_version_id=catalog_version_id, watermark_plans={})
    finding = _finding()
    assert finding is not None
    assert finding.status == "open"
    assert _events(finding.id) == ["created"]

    # run 3: passing again, auto_resolve=True (default) -- resolved_system.
    cursor.execute(
        "UPDATE fqb.Invoices_Base SET TotalAmount = 100.00 WHERE InvoiceTransactionID = ?",
        [_temp_invoice_row],
    )
    run_once(conn, source_conn, checks, catalog_version_id=catalog_version_id, watermark_plans={})
    finding = _finding()
    assert finding is not None
    assert finding.status == "resolved_system"
    assert _events(finding.id) == ["created", "resolved_system"]

    # "dismissal without reason code is rejected by the database itself."
    with pytest.raises(sa.exc.IntegrityError):
        conn.execute(
            sa.text("INSERT INTO finding_events (finding_id, event) VALUES (:id, 'dismissed')"),
            {"id": finding.id},
        )


# --- criterion 6: both audit sinks, run id attached --------------------------


@pytest.fixture
def clean_app_db_tables(migrated_db: Engine) -> Iterator[Engine]:
    """`SourceAuditLogRepository.record()` commits its own engine-level
    transaction by design (step 2) -- the rolled-back `conn` fixture can't
    clean it up. This test therefore uses `migrated_db` directly. Deliberately
    stays clear of `findings`/`finding_events` (the latter is append-only,
    DB-enforced -- see the `finding_events_no_delete` trigger -- so a test
    outside the auto-rollback `conn` fixture must never create one); `runs`
    and `source_audit_log` are both freely deletable."""
    yield migrated_db
    with migrated_db.begin() as conn:
        for table in ("source_audit_log", "runs"):
            conn.execute(sa.text(f"DELETE FROM {table}"))


def test_criterion_6_every_source_statement_audited_in_both_sinks_with_run_id(
    clean_app_db_tables: Engine, fixture_conn: pyodbc.Connection, tmp_path: Path
) -> None:
    """Proves the dual-sink/run-id plumbing itself (D-016, exit criterion
    6) directly through `fetch_live_columns` -- one real source statement,
    executed with a real `run_id` from a real `runs` row -- rather than
    through the full `run_once`/materialize path, which would create
    `findings`/`finding_events` rows this test then couldn't clean up."""

    class _Adapter:
        timeout = 0

        def cursor(self) -> pyodbc.Cursor:
            return fixture_conn.cursor()

    source_conn = AuditedSourceConnection(
        _Adapter(),  # type: ignore[arg-type]
        component="test-criterion-6",
        allowed_objects=_ALLOWED_OBJECTS,
        audit_dir=tmp_path,
        app_db_sink=SourceAuditLogRepository(clean_app_db_tables),
    )

    # AUTOCOMMIT: SourceAuditLogRepository.record() opens its own
    # engine-level transaction per call and runs synchronously inside
    # execute_query() -- against a plain `begin()` block, its FK reference to
    # a not-yet-committed `runs` row would fail to resolve (proven live: this
    # exact test failed with a ForeignKeyViolation before switching to
    # autocommit). Autocommit makes the run row visible immediately.
    with clean_app_db_tables.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        run_id = conn.execute(sa.text("INSERT INTO runs DEFAULT VALUES RETURNING id")).one().id
        run_id = str(run_id)
        columns = fetch_live_columns(source_conn, "fqb.Invoices", run_id=run_id)

    assert "TotalAmount" in columns  # the statement genuinely executed

    jsonl_lines = list(tmp_path.glob("source-audit-*.jsonl"))
    assert len(jsonl_lines) == 1
    jsonl_events = [json.loads(line) for line in jsonl_lines[0].read_text().splitlines()]
    assert len(jsonl_events) == 1
    assert jsonl_events[0]["run_id"] == run_id

    with clean_app_db_tables.connect() as conn:
        mirrored = conn.execute(
            sa.select(source_audit_log).where(source_audit_log.c.run_id == uuid.UUID(run_id))
        ).all()
    assert len(mirrored) == 1
    assert mirrored[0].component == "test-criterion-6"
    assert mirrored[0].statement == jsonl_events[0]["statement"]
