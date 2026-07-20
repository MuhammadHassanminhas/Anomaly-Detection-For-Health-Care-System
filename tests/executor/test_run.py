"""Phase 3 step 8: the run loop + cost report. Pure tests for
`render_cost_report`/`write_cost_report` need no DB; `run_once`'s own
integration test is the step's named deliverable ("one-command run against
fixture DB producing the report") and requires both CDSS_APP_DB_URL and the
LocalDB fixture -- skips (never fails) otherwise, D-009.1, reusing
`tests/executor/test_end_to_end.py`'s exact seeding/fixture-wiring pattern.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pyodbc
import pytest
import sqlalchemy as sa
import yaml

from cdss.check_registry import load_active_checks
from cdss.materialize import MaterializationStats
from cdss.run import (
    CheckRunSummary,
    NarrationStats,
    RunReport,
    get_or_create_catalog_version,
    render_cost_report,
    run_once,
    write_cost_report,
)
from cdss.source import AuditedSourceConnection

EXAMPLES_DIR = Path(__file__).parent.parent.parent / "examples" / "checks"
_ALLOWED_OBJECTS = frozenset({"dbo.appointments", "dbo.invoices", "fqb.invoices", "dbo.patient"})

# name -> (params, expected counts) -- same fixture-DB expectations
# tests/executor/test_end_to_end.py already proved live.
_EXAMPLES: dict[str, tuple[dict[str, object], dict[str, int]]] = {
    "appointment-completed-no-invoice": (
        {"invoice_lag_days": 7},
        {"rows_examined": 6, "n_fail": 1, "n_pass": 3, "n_indeterminate": 2},
    ),
    "appointment-invalid-status-code": (
        {},
        {"rows_examined": 29, "n_pass": 21, "n_fail": 5, "n_indeterminate": 3},
    ),
    "invoice-negative-total-amount": (
        {},
        {"rows_examined": 17, "n_fail": 1, "n_pass": 15, "n_indeterminate": 1},
    ),
    "invoice-stale-unpaid-balance": (
        {"stale_days": 60},
        {"rows_examined": 16, "n_fail": 2, "n_pass": 13, "n_indeterminate": 1},
    ),
    "patient-active-missing-nhi": (
        {},
        {"rows_examined": 6, "n_fail": 2, "n_pass": 3, "n_indeterminate": 1},
    ),
    "patient-no-recent-appointment": (
        {"recall_window_days": 365},
        {"rows_examined": 5, "n_fail": 1, "n_pass": 3, "n_indeterminate": 1},
    ),
}


def _seed_all_examples(conn: sa.Connection, *, with_system_check: bool = False) -> None:
    conn.execute(
        sa.text("INSERT INTO practices (practice_id, name) VALUES ('practice-1', 'Test Practice')")
    )
    for name, (params, _) in _EXAMPLES.items():
        raw = yaml.safe_load((EXAMPLES_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
        check_id = str(
            conn.execute(
                sa.text(
                    "INSERT INTO checks (slug, title, category, default_severity, source, status) "
                    "VALUES (:slug, :title, :category, :severity, 'manual', 'active') "
                    "RETURNING id"
                ),
                {
                    "slug": name,
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
            {
                "check_id": check_id,
                "definition": json.dumps(raw),
                "view": raw["entity"]["view"],
            },
        )
        conn.execute(
            sa.text(
                "INSERT INTO practice_check_config (practice_id, check_id, params) "
                "VALUES ('practice-1', :check_id, CAST(:params AS jsonb))"
            ),
            {"check_id": check_id, "params": json.dumps(params)},
        )

    if with_system_check:
        system_check_id = str(
            conn.execute(
                sa.text(
                    "INSERT INTO checks (slug, title, category, default_severity, source, status) "
                    "VALUES ('system-indeterminate-rate', 'Indeterminate rate', 'data-quality', "
                    "'medium', 'manual', 'active') RETURNING id"
                )
            )
            .one()
            .id
        )
        conn.execute(
            sa.text(
                "INSERT INTO check_versions "
                "(check_id, version_number, definition, definition_hash, "
                "affected_views, params_schema) "
                "VALUES (:check_id, 1, '{\"kind\": \"system\"}'::jsonb, 'hash', "
                "ARRAY[]::text[], '{}'::jsonb)"
            ),
            {"check_id": system_check_id},
        )
        conn.execute(
            sa.text(
                "INSERT INTO practice_check_config (practice_id, check_id) "
                "VALUES ('practice-1', :check_id)"
            ),
            {"check_id": system_check_id},
        )


@pytest.fixture
def source_conn(fixture_conn: pyodbc.Connection, tmp_path: Path) -> AuditedSourceConnection:
    class _Adapter:
        timeout = 0

        def cursor(self) -> pyodbc.Cursor:
            return fixture_conn.cursor()

    return AuditedSourceConnection(
        _Adapter(),  # type: ignore[arg-type]
        component="test-run",
        allowed_objects=_ALLOWED_OBJECTS,
        audit_dir=tmp_path,
    )


# --- pure: report rendering -------------------------------------------------


def _synthetic_report() -> RunReport:
    summary = CheckRunSummary(
        slug="example-check",
        practice_id="practice-1",
        status="ok",
        duration_ms=42,
        rows_examined=10,
        n_pass=6,
        n_fail=3,
        n_indeterminate=1,
        watermark_from=None,
        watermark_to=datetime(2026, 1, 1, tzinfo=UTC),
        watermark_strategy="bounded_full_scan",
        materialization=MaterializationStats(created=2, reseen=1),
        narration=NarrationStats(composed=1, cached=1, blocked=0, fallback=0),
    )
    return RunReport(
        run_id="test-run-id",
        started_at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        summaries=(summary,),
        ask_recommendations=("dbo.Hot: hot and unwatermarkable -- recommend an ASK-NNN",),
    )


def test_render_cost_report_includes_every_check_and_ask_recommendation() -> None:
    text = render_cost_report(_synthetic_report())
    assert "example-check" in text
    assert "practice-1" in text
    assert "42" in text
    assert "dbo.Hot: hot and unwatermarkable" in text
    assert "test-run-id" in text
    # exit criterion 5: unwatermarkable-view executions visibly marked with
    # their fallback strategy -- both in the per-row table and its own section.
    assert "bounded_full_scan" in text
    assert "example-check (practice-1): bounded_full_scan fallback" in text
    # Phase 5 step 7: narration stats surface per-check and as a run total.
    assert "Narratives (composed/cached/blocked/fallback)" in text
    assert "1 composed, 1 cached, 0 blocked (validator-rejected), 0 fallback" in text


def test_render_cost_report_no_ask_recommendations_says_none() -> None:
    report = RunReport(
        run_id="r",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        finished_at=datetime(2026, 1, 1, tzinfo=UTC),
        summaries=(),
        ask_recommendations=(),
    )
    text = render_cost_report(report)
    assert "(none)" in text
    assert "(no checks executed)" in text


def test_write_cost_report_writes_the_expected_filename(tmp_path: Path) -> None:
    path = write_cost_report(_synthetic_report(), report_dir=tmp_path)
    assert path == tmp_path / "run-test-run-id-report.md"
    assert path.read_text(encoding="utf-8") == render_cost_report(_synthetic_report())


# --- DB-gated: the named deliverable ----------------------------------------


def test_run_once_against_fixture_db_produces_a_report(
    conn: sa.Connection, source_conn: AuditedSourceConnection, tmp_path: Path
) -> None:
    _seed_all_examples(conn)
    checks = load_active_checks(conn)
    assert len(checks) == 6
    catalog_version_id = get_or_create_catalog_version(
        conn, sha256="test-hash", source_path="test-path"
    )

    report = run_once(
        conn, source_conn, checks, catalog_version_id=catalog_version_id, watermark_plans={}
    )

    assert len(report.summaries) == 6
    by_slug = {s.slug: s for s in report.summaries}
    for slug, (_, expected) in _EXAMPLES.items():
        summary = by_slug[slug]
        assert summary.status == "ok", slug
        assert summary.rows_examined == expected["rows_examined"], slug
        assert summary.n_pass == expected["n_pass"], slug
        assert summary.n_fail == expected["n_fail"], slug
        assert summary.n_indeterminate == expected["n_indeterminate"], slug

    total_findings = conn.execute(sa.text("SELECT COUNT(*) FROM findings")).scalar()
    assert total_findings == sum(expected["n_fail"] for _, expected in _EXAMPLES.values())
    # narration is opt-in (no narration_client passed here) -- no narratives
    # row should exist, proving this run's wiring is unaffected by step 7.
    assert conn.execute(sa.text("SELECT COUNT(*) FROM narratives")).scalar() == 0

    # the step's own named deliverable: one command produces the report file.
    path = write_cost_report(report, report_dir=tmp_path)
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    for slug in _EXAMPLES:
        assert slug in text


def test_run_once_second_run_creates_no_duplicate_findings(
    conn: sa.Connection, source_conn: AuditedSourceConnection
) -> None:
    """Fixture views have no real watermark column (step 5's own flagged
    gap), so every check here goes through the bounded-full-scan fallback --
    a second run re-scans the same static rows and legitimately emits
    `reseen` events for still-failing entities (the fallback's documented
    semantics, not a bug). What must never happen, and is asserted here, is
    a *second* findings row for the same entity -- the UNIQUE(check_id,
    dedupe_key) constraint plus materialize's own upsert logic."""
    _seed_all_examples(conn)
    checks = load_active_checks(conn)
    catalog_version_id = get_or_create_catalog_version(
        conn, sha256="test-hash", source_path="test-path"
    )

    run_once(conn, source_conn, checks, catalog_version_id=catalog_version_id, watermark_plans={})
    count_after_first = conn.execute(sa.text("SELECT COUNT(*) FROM findings")).scalar()

    run_once(conn, source_conn, checks, catalog_version_id=catalog_version_id, watermark_plans={})
    count_after_second = conn.execute(sa.text("SELECT COUNT(*) FROM findings")).scalar()

    assert count_after_second == count_after_first


def test_run_once_evaluates_indeterminacy_per_practice(
    conn: sa.Connection, source_conn: AuditedSourceConnection
) -> None:
    _seed_all_examples(conn, with_system_check=True)
    checks = load_active_checks(conn)
    assert len(checks) == 7
    catalog_version_id = get_or_create_catalog_version(
        conn, sha256="test-hash", source_path="test-path"
    )

    report = run_once(
        conn,
        source_conn,
        checks,
        catalog_version_id=catalog_version_id,
        watermark_plans={},
        indeterminate_threshold=0.15,
    )

    system_summaries = [s for s in report.summaries if s.slug == "system-indeterminate-rate"]
    # every target check with an indeterminate rate above 0.15 gets exactly
    # one system summary row -- never one per indeterminate source row.
    assert len(system_summaries) >= 1
    assert all(s.practice_id == "practice-1" for s in system_summaries)
    # system-check findings are never narrated -- their `definition` isn't
    # DSL-shaped (`{"kind": "system"}`), so composing a narrative for them
    # would fail; the run loop must simply never try.
    assert all(s.narration == NarrationStats() for s in system_summaries)


# --- Phase 5 step 7: narration wired into the run loop -----------------------


class _FakeLLMClient:
    def __init__(self, response: str) -> None:
        self._response = response
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        return self._response


class _RaisingLLMClient:
    def complete(self, prompt: str) -> str:
        raise RuntimeError("simulated LLM outage")


_INVOICE_NARRATION_RESPONSE = json.dumps(
    {
        "template": "Invoice {{InvoiceTransactionID}} has a negative total of {{TotalAmount}}.",
        "actions": ["flag-for-data-steward-review"],
    }
)


def test_run_once_narrates_every_new_finding_inline(
    conn: sa.Connection, source_conn: AuditedSourceConnection
) -> None:
    _seed_all_examples(conn)
    checks = load_active_checks(conn)
    catalog_version_id = get_or_create_catalog_version(
        conn, sha256="test-hash", source_path="test-path"
    )
    client = _FakeLLMClient(_INVOICE_NARRATION_RESPONSE)

    report = run_once(
        conn,
        source_conn,
        checks,
        catalog_version_id=catalog_version_id,
        watermark_plans={},
        narration_client=client,
        narration_model_id="gpt-4o-mini",
    )

    total_findings = conn.execute(sa.text("SELECT COUNT(*) FROM findings")).scalar()
    total_narratives = conn.execute(sa.text("SELECT COUNT(*) FROM narratives")).scalar()
    # every new finding this run ends with a narrative row -- the step's own
    # named deliverable ("every new finding ends with a rendered, validated
    # narrative or an explicit fallback").
    assert total_narratives == total_findings

    # invoice-negative-total-amount has exactly 1 failing row (_EXAMPLES),
    # so this is a deterministic single-composition proof, unaffected by
    # whatever cache sharing happens across the run's other checks.
    invoice_summary = next(s for s in report.summaries if s.slug == "invoice-negative-total-amount")
    assert invoice_summary.narration == NarrationStats(composed=1, cached=0, blocked=0, fallback=0)

    invoice_check = next(c for c in checks if c.slug == "invoice-negative-total-amount")
    narrative = conn.execute(
        sa.text(
            "SELECT n.rendered_text, n.validation_status, n.model_id "
            "FROM narratives n JOIN findings f ON f.id = n.finding_id "
            "WHERE f.check_id = :check_id"
        ),
        {"check_id": invoice_check.check_id},
    ).one()
    assert narrative.validation_status == "valid"
    assert narrative.model_id == "gpt-4o-mini"
    assert "has a negative total of" in narrative.rendered_text


def test_run_once_narration_falls_back_when_the_llm_is_unreachable(
    conn: sa.Connection, source_conn: AuditedSourceConnection
) -> None:
    """A finding must never be lost or delayed by narration (F8): if the LLM
    is unreachable, the finding still materializes and still gets a
    narrative -- the check's own deterministic `fallback_template` -- rather
    than being left un-narrated."""
    _seed_all_examples(conn)
    checks = load_active_checks(conn)
    catalog_version_id = get_or_create_catalog_version(
        conn, sha256="test-hash", source_path="test-path"
    )
    client = _RaisingLLMClient()

    report = run_once(
        conn,
        source_conn,
        checks,
        catalog_version_id=catalog_version_id,
        watermark_plans={},
        narration_client=client,
        narration_model_id="gpt-4o-mini",
    )

    total_findings = conn.execute(sa.text("SELECT COUNT(*) FROM findings")).scalar()
    total_narratives = conn.execute(sa.text("SELECT COUNT(*) FROM narratives")).scalar()
    assert total_narratives == total_findings

    invoice_summary = next(s for s in report.summaries if s.slug == "invoice-negative-total-amount")
    assert invoice_summary.narration == NarrationStats(composed=0, cached=0, blocked=0, fallback=1)

    fallback_statuses = (
        conn.execute(sa.text("SELECT DISTINCT validation_status FROM narratives")).scalars().all()
    )
    assert fallback_statuses == ["fallback_static"]


def test_force_outage_client_raises_without_calling_a_real_api() -> None:
    from cdss.run import _ForceOutageClient

    with pytest.raises(RuntimeError, match="CDSS_LLM_FORCE_OUTAGE"):
        _ForceOutageClient().complete("any prompt")


def test_build_narration_client_honors_the_force_outage_env_var() -> None:
    from cdss.run import _build_narration_client

    client, model_id = _build_narration_client({"CDSS_LLM_FORCE_OUTAGE": "1"})
    assert model_id == "forced-outage"
    with pytest.raises(RuntimeError, match="CDSS_LLM_FORCE_OUTAGE"):
        client.complete("any prompt")
