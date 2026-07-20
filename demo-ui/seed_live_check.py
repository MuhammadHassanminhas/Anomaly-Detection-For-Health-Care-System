"""One-off setup script, not part of the codebase (lives in demo-ui/, same
scope-discipline as server.py -- see its own header). Seeds every checked-in
example check (`examples/checks/`, the same DSL definitions this project's
own test suite already proves work) as `active` in the app DB, one practice,
covering every in-scope view (dbo.Appointments, fqb.Invoices, dbo.Patient --
the D-025 4-view scope), so `python -m cdss.run` has real, broad work to do
against the real source DB instead of a single table. No synthetic/demo data
involved.

Run once: `uv run python demo-ui/seed_live_check.py` (safe to re-run --
skips any slug that already exists).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import sqlalchemy as sa
import yaml

from cdss.app_db import load_app_db_url

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples" / "checks"
PRACTICE_ID = "practice-live"

# slug -> params for that check's own declared (non-percentile-learned)
# params -- the same defaults tests/executor/test_run.py's own _EXAMPLES
# table already established and proves work against real/fixture data.
# invoice_lag_days/stale_days/recall_window_days are plain `integer`-typed
# params with no percentile learning wired into this seed path -- these are
# each check's own reasonable fixed default, not fabricated data.
CHECKS: dict[str, dict[str, object]] = {
    "appointment-completed-no-invoice": {"invoice_lag_days": 7},
    "appointment-invalid-status-code": {},
    "invoice-negative-total-amount": {},
    "invoice-stale-unpaid-balance": {"stale_days": 60},
    "patient-active-missing-nhi": {},
    "patient-no-recent-appointment": {"recall_window_days": 365},
}


def _seed_one(conn: sa.Connection, slug: str, params: dict[str, object]) -> None:
    existing = conn.execute(
        sa.text("SELECT id FROM checks WHERE slug = :slug"), {"slug": slug}
    ).one_or_none()
    if existing is not None:
        print(f"'{slug}' already exists (id={existing.id}) -- skipped.")
        return

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
            "VALUES (:pid, :check_id, CAST(:params AS jsonb))"
        ),
        {"pid": PRACTICE_ID, "check_id": check_id, "params": json.dumps(params)},
    )
    print(f"seeded '{slug}' as active, check_id={check_id}")


def main() -> int:
    engine = sa.create_engine(load_app_db_url())
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO practices (practice_id, name) VALUES (:pid, :pid) "
                "ON CONFLICT (practice_id) DO NOTHING"
            ),
            {"pid": PRACTICE_ID},
        )
        for slug, params in CHECKS.items():
            _seed_one(conn, slug, params)
    return 0


if __name__ == "__main__":
    sys.exit(main())
