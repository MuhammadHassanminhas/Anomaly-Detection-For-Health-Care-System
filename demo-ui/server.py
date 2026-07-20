"""CDSS demo-UI backend -- read-only status API + one clearly-labeled demo
write action, both against the REAL app DB (CDSS_APP_DB_URL) by calling the
REAL `cdss.*` functions directly (no reimplemented business logic, no
hardcoded numbers anywhere in this file or in index.html).

**Scope discipline (explicit, per product-owner instruction 2026-07-20):**
this file lives entirely in `demo-ui/`, outside `src/` and `tests/`. Every
gate command (`ruff check src tests`, `ruff format --check src tests`,
`mypy` with `files = ["src"]`, `pytest` with `testpaths = tests`, all per
`pyproject.toml`/`scripts/check.ps1`) is scoped to those two directories --
this file and `index.html` are invisible to all of them. It imports *from*
`cdss` (read-only queries, real service functions) but nothing in `cdss`
imports from here -- the dependency arrow points one way. Deleting this
whole folder has zero effect on the actual system.

Two kinds of endpoint:
  GET  /api/status      -- read-only snapshot of real app-DB state, nothing
                            cached, nothing invented; every field is a live
                            query result or an explicit "no data yet".
  POST /api/demo/run    -- a single clearly-labeled demonstration action:
                            seeds a handful of findings, drives a synthetic
                            (labeled) reason-coded feedback stream through
                            the real `cdss.feedback.dismiss`, then runs the
                            real `cdss.calibration.precision`/`demotion`
                            jobs and the real `cdss.run.run_once` executor
                            against the fixture SQL Server (LocalDB, D-026)
                            -- never the real production source DB. Every
                            row this writes is tagged so it's obviously
                            demo data in the dashboard (slug/practice_id
                            carry a `demo-` prefix + a timestamp suffix, so
                            repeated runs never collide and nothing needs
                            deleting afterward).

Run: `uv run python demo-ui/server.py` from the repo root (needs
CDSS_APP_DB_URL in the environment; the demo-run action additionally needs
the fixture LocalDB instance -- `scripts/fixture_db.ps1` -- reachable).
"""

from __future__ import annotations

import json
import time
import traceback
import uuid
import yaml
from datetime import UTC, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pyodbc
import sqlalchemy as sa

from cdss.app_db import MissingAppDbConfigError, load_app_db_url
from cdss.calibration.demotion import run_demotion_job
from cdss.calibration.precision import run_precision_job
from cdss.check_registry import load_active_checks
from cdss.executor import create_run, finish_run
from cdss.feedback import compute_reason_code_distribution, dismiss
from cdss.run import get_or_create_catalog_version, run_once
from cdss.source import AuditedSourceConnection

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples" / "checks"
STATIC_DIR = Path(__file__).resolve().parent
PORT = 8010

_FIXTURE_CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=(localdb)\\MSSQLLocalDB;DATABASE=cdss_fixture;"
    "Trusted_Connection=yes;"
)
_ALLOWED_OBJECTS = frozenset({"dbo.appointments", "dbo.invoices", "fqb.invoices", "dbo.patient"})


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(value)


def _rows(result: sa.CursorResult[Any]) -> list[dict[str, Any]]:
    return [dict(row._mapping) for row in result]


# --- read-only status queries -------------------------------------------


def _checks_summary(conn: sa.Connection) -> list[dict[str, Any]]:
    return _rows(
        conn.execute(
            sa.text(
                "SELECT status, source, COUNT(*) AS count FROM checks "
                "GROUP BY status, source ORDER BY status, source"
            )
        )
    )


def _active_checks(conn: sa.Connection) -> list[dict[str, Any]]:
    return _rows(
        conn.execute(
            sa.text(
                "SELECT c.slug, c.category, c.default_severity, "
                "COUNT(DISTINCT pcc.practice_id) AS practices, "
                "COUNT(DISTINCT pcc.practice_id) FILTER (WHERE pcc.demoted) AS demoted_practices "
                "FROM checks c JOIN practice_check_config pcc ON pcc.check_id = c.id "
                "WHERE c.status = 'active' "
                "GROUP BY c.id, c.slug, c.category, c.default_severity ORDER BY c.slug"
            )
        )
    )


def _findings_summary(conn: sa.Connection) -> list[dict[str, Any]]:
    return _rows(
        conn.execute(
            sa.text("SELECT status, COUNT(*) AS count FROM findings GROUP BY status ORDER BY status")
        )
    )


def _demoted_pairs(conn: sa.Connection) -> list[dict[str, Any]]:
    return _rows(
        conn.execute(
            sa.text(
                "SELECT pcc.practice_id, c.slug, pcc.demoted_at, pcc.demotion_reason "
                "FROM practice_check_config pcc JOIN checks c ON c.id = pcc.check_id "
                "WHERE pcc.demoted = true ORDER BY pcc.demoted_at DESC"
            )
        )
    )


def _latest_precision(conn: sa.Connection) -> list[dict[str, Any]]:
    return _rows(
        conn.execute(
            sa.text(
                "SELECT DISTINCT ON (ps.practice_id, ps.check_id) "
                "ps.practice_id, c.slug, ps.window_size, ps.genuine_issue_count, "
                "ps.total_feedback_count, ps.precision, ps.computed_at "
                "FROM precision_stats ps JOIN checks c ON c.id = ps.check_id "
                "ORDER BY ps.practice_id, ps.check_id, ps.computed_at DESC"
            )
        )
    )


def _reason_code_distribution(conn: sa.Connection) -> list[dict[str, Any]]:
    return [
        {
            "check_id": e.check_id,
            "slug": e.slug,
            "genuine_issue_count": e.genuine_issue_count,
            "not_genuine_count": e.not_genuine_count,
        }
        for e in compute_reason_code_distribution(conn)
    ]


def _recent_calibration_runs(conn: sa.Connection) -> list[dict[str, Any]]:
    return _rows(
        conn.execute(
            sa.text(
                "SELECT cr.run_at, cr.practice_id, c.slug, cr.params_before, cr.params_after, cr.notes "
                "FROM calibration_runs cr JOIN checks c ON c.id = cr.check_id "
                "ORDER BY cr.run_at DESC LIMIT 20"
            )
        )
    )


def _recent_runs(conn: sa.Connection) -> list[dict[str, Any]]:
    return _rows(
        conn.execute(
            sa.text(
                "SELECT id, started_at, finished_at, status FROM runs "
                "ORDER BY started_at DESC LIMIT 10"
            )
        )
    )


def _recent_narratives(conn: sa.Connection) -> list[dict[str, Any]]:
    """Real LLM-composed narrative text -- never the demo/fixture scenario's
    own data (`f.practice_id NOT LIKE 'demo-%'`), so this always reflects a
    genuine `cdss.run` production execution against the real source DB."""
    return _rows(
        conn.execute(
            sa.text(
                "SELECT c.slug, f.practice_id, n.validation_status, n.model_id, "
                "n.rendered_text, n.created_at "
                "FROM narratives n "
                "JOIN findings f ON f.id = n.finding_id "
                "JOIN checks c ON c.id = f.check_id "
                "WHERE f.practice_id NOT LIKE 'demo-%' "
                "ORDER BY n.created_at DESC LIMIT 10"
            )
        )
    )


def build_status(engine: sa.Engine) -> dict[str, Any]:
    with engine.connect() as conn:
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "checks_summary": _checks_summary(conn),
            "active_checks": _active_checks(conn),
            "findings_summary": _findings_summary(conn),
            "demoted_pairs": _demoted_pairs(conn),
            "latest_precision": _latest_precision(conn),
            "reason_code_distribution": _reason_code_distribution(conn),
            "recent_calibration_runs": _recent_calibration_runs(conn),
            "recent_runs": _recent_runs(conn),
            "recent_narratives": _recent_narratives(conn),
        }


# --- demo write action: the real feedback loop, fixture-DB only ---------


def run_demo_scenario(engine: sa.Engine) -> dict[str, Any]:
    """Real code, fixture data, clearly-labeled rows -- never the production
    source DB. Mirrors `tests/e2e/test_feedback_loop.py`'s own demotion-loop
    scenario, but COMMITS (the test's own `conn` fixture always rolls back)
    so the dashboard has something real to show afterward."""
    try:
        fixture_raw = pyodbc.connect(_FIXTURE_CONN_STR, timeout=3, autocommit=True)
    except pyodbc.Error as exc:
        raise RuntimeError(
            f"fixture SQL Server (LocalDB) not reachable ({exc}); "
            "run scripts/fixture_db.ps1 -Recreate first"
        ) from exc

    stamp = str(int(time.time()))
    slug = f"demo-invoice-negative-total-amount-{stamp}"
    practice_a = f"demo-practice-A-{stamp}"
    practice_b = f"demo-practice-B-{stamp}"

    class _Adapter:
        timeout = 0

        def cursor(self) -> pyodbc.Cursor:
            return fixture_raw.cursor()

    source_conn = AuditedSourceConnection(
        _Adapter(),  # type: ignore[arg-type]
        component="demo-ui",
        allowed_objects=_ALLOWED_OBJECTS,
        audit_dir=STATIC_DIR / "audit",
    )

    try:
        with engine.begin() as conn:
            raw = yaml.safe_load(
                (EXAMPLES_DIR / "invoice-negative-total-amount.yaml").read_text(encoding="utf-8")
            )
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
            version_id = str(
                conn.execute(
                    sa.text(
                        "INSERT INTO check_versions "
                        "(check_id, version_number, definition, definition_hash, "
                        "affected_views, params_schema) "
                        "VALUES (:check_id, 1, CAST(:definition AS jsonb), 'hash', "
                        "ARRAY[:view]::text[], '{}'::jsonb) RETURNING id"
                    ),
                    {
                        "check_id": check_id,
                        "definition": json.dumps(raw),
                        "view": raw["entity"]["view"],
                    },
                )
                .one()
                .id
            )
            for pid in (practice_a, practice_b):
                conn.execute(
                    sa.text("INSERT INTO practices (practice_id, name) VALUES (:pid, :pid)"),
                    {"pid": pid},
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO practice_check_config (practice_id, check_id, params) "
                        "VALUES (:pid, :check_id, '{}'::jsonb)"
                    ),
                    {"pid": pid, "check_id": check_id},
                )

            # one real, properly-completed 'runs' row for every seeded
            # finding to reference (findings.first/last_seen_run_id are
            # NOT NULL) -- reusing a single seed run rather than minting
            # one per finding keeps the real `runs` table honest instead of
            # littering it with never-finished rows.
            seed_run_id = create_run(conn, triggered_by="demo-ui-seed")

            def _seed_finding(practice_id: str, dedupe_key: str) -> str:
                run_id = seed_run_id
                return str(
                    conn.execute(
                        sa.text(
                            "INSERT INTO findings "
                            "(check_id, check_version_id, practice_id, dedupe_key, entity_key, "
                            " status, severity, evidence, first_seen_run_id, last_seen_run_id) "
                            "VALUES (:check_id, :version_id, :practice_id, :dedupe_key, "
                            "'{}'::jsonb, 'open', 'medium', '{}'::jsonb, :run_id, :run_id) "
                            "RETURNING id"
                        ),
                        {
                            "check_id": check_id,
                            "version_id": version_id,
                            "practice_id": practice_id,
                            "dedupe_key": dedupe_key,
                            "run_id": run_id,
                        },
                    )
                    .one()
                    .id
                )

            # practice A: mostly 'not_genuine' -- precision falls below the
            # D-011 floor (0.30). practice B: mostly 'genuine_issue' --
            # precision stays healthy. Every actor is labeled 'demo-ui'.
            for i in range(12):
                fid = _seed_finding(practice_a, f"demo-a-not-genuine-{stamp}-{i}")
                dismiss(conn, fid, reason_code="not_genuine", actor="demo-ui")
            for i in range(3):
                fid = _seed_finding(practice_a, f"demo-a-genuine-{stamp}-{i}")
                dismiss(conn, fid, reason_code="genuine_issue", actor="demo-ui")
            for i in range(13):
                fid = _seed_finding(practice_b, f"demo-b-genuine-{stamp}-{i}")
                dismiss(conn, fid, reason_code="genuine_issue", actor="demo-ui")
            for i in range(2):
                fid = _seed_finding(practice_b, f"demo-b-not-genuine-{stamp}-{i}")
                dismiss(conn, fid, reason_code="not_genuine", actor="demo-ui")
            finish_run(conn, seed_run_id, status="completed")

            precision_results = run_precision_job(conn)
            demotion_results = run_demotion_job(conn)

            checks = load_active_checks(conn)
            catalog_version_id = get_or_create_catalog_version(
                conn, sha256=f"demo-ui-{stamp}", source_path="demo-ui"
            )
            report = run_once(
                conn, source_conn, checks, catalog_version_id=catalog_version_id, watermark_plans={}
            )
    finally:
        fixture_raw.close()

    pairs_fired = sorted({(s.slug, s.practice_id) for s in report.summaries})
    return {
        "slug": slug,
        "practice_a": practice_a,
        "practice_b": practice_b,
        "precision": [
            {"practice_id": r.practice_id, "precision": str(r.precision), "n": r.total_feedback_count}
            for r in precision_results
        ],
        "demoted": [
            {"practice_id": r.practice_id, "demoted_this_run": r.demoted_this_run}
            for r in demotion_results
        ],
        "run_id": report.run_id,
        "pairs_that_fired_this_run": pairs_fired,
    }


# --- tiny HTTP layer ------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    engine: sa.Engine

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's own naming
        if self.path == "/api/status":
            try:
                self._send_json({"ok": True, "data": build_status(self.engine)})
            except Exception as exc:  # noqa: BLE001 -- surfaced to the UI verbatim, not hidden
                self._send_json({"ok": False, "error": str(exc)}, status=500)
            return
        if self.path in ("/", "/index.html"):
            self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/demo/run":
            try:
                result = run_demo_scenario(self.engine)
                self._send_json({"ok": True, "data": result})
            except Exception as exc:  # noqa: BLE001
                self._send_json(
                    {"ok": False, "error": str(exc), "trace": traceback.format_exc()}, status=500
                )
            return
        self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        print(f"[demo-ui] {self.address_string()} - {format % args}")


def main() -> int:
    try:
        engine = sa.create_engine(load_app_db_url())
    except MissingAppDbConfigError as exc:
        print(f"error: {exc}")
        print("set CDSS_APP_DB_URL first (same as every other cdss.* script).")
        return 1

    Handler.engine = engine
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"CDSS demo UI: http://127.0.0.1:{PORT}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
