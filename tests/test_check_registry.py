"""Phase 3 step 3: check registry loader tests, incl. the draft-never-runs
proof (F3). Requires CDSS_APP_DB_URL and skips (never fails) otherwise --
D-009.1. Every test runs inside its own rolled-back transaction (the `conn`
fixture from tests/conftest.py).
"""

from __future__ import annotations

import json

import pytest
import sqlalchemy as sa

from cdss.check_registry import load_active_checks


def _seed_check(conn: sa.Connection, *, status: str = "active", slug: str = "test-check") -> str:
    row = conn.execute(
        sa.text(
            "INSERT INTO checks (slug, title, category, default_severity, source, status) "
            "VALUES (:slug, 'Test Check', 'data-quality', 'medium', 'manual', :status) "
            "RETURNING id"
        ),
        {"slug": slug, "status": status},
    ).one()
    return str(row.id)


def _seed_check_version(
    conn: sa.Connection,
    check_id: str,
    *,
    version_number: int = 1,
    definition: dict[str, object] | None = None,
    params_schema: dict[str, object] | None = None,
    rationale: str | None = None,
    fallback_template: str | None = None,
) -> str:
    columns = "check_id, version_number, definition, definition_hash, affected_views, params_schema"
    values = (
        ":check_id, :version_number, CAST(:definition AS jsonb), 'hash', "
        "ARRAY['dbo.vw_test']::text[], CAST(:params_schema AS jsonb)"
    )
    params: dict[str, object] = {
        "check_id": check_id,
        "version_number": version_number,
        "definition": json.dumps(definition or {"id": "test-check"}),
        "params_schema": json.dumps(params_schema or {}),
    }
    if rationale is not None:
        columns += ", rationale"
        values += ", :rationale"
        params["rationale"] = rationale
    if fallback_template is not None:
        columns += ", fallback_template"
        values += ", :fallback_template"
        params["fallback_template"] = fallback_template
    row = conn.execute(
        sa.text(f"INSERT INTO check_versions ({columns}) VALUES ({values}) RETURNING id"),
        params,
    ).one()
    return str(row.id)


def _seed_practice(conn: sa.Connection, practice_id: str = "practice-1") -> str:
    conn.execute(
        sa.text(
            "INSERT INTO practices (practice_id, name) VALUES (:pid, 'Test Practice') "
            "ON CONFLICT (practice_id) DO NOTHING"
        ),
        {"pid": practice_id},
    )
    return practice_id


def _seed_practice_check_config(
    conn: sa.Connection,
    practice_id: str,
    check_id: str,
    *,
    enabled: bool = True,
    demoted: bool = False,
    params: dict[str, object] | None = None,
    params_source: str = "default",
) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO practice_check_config "
            "(practice_id, check_id, enabled, demoted, params, params_source) "
            "VALUES (:practice_id, :check_id, :enabled, :demoted, "
            "CAST(:params AS jsonb), :params_source)"
        ),
        {
            "practice_id": practice_id,
            "check_id": check_id,
            "enabled": enabled,
            "demoted": demoted,
            "params": json.dumps(params or {}),
            "params_source": params_source,
        },
    )


def test_load_active_checks_returns_configured_active_check(conn: sa.Connection) -> None:
    check_id = _seed_check(conn, status="active")
    version_id = _seed_check_version(conn, check_id, rationale="Why this check exists.")
    practice_id = _seed_practice(conn)
    _seed_practice_check_config(conn, practice_id, check_id, params={"stale_days": 30})

    loaded = load_active_checks(conn)

    assert len(loaded) == 1
    check = loaded[0]
    assert check.check_id == check_id
    assert check.slug == "test-check"
    assert check.check_version_id == version_id
    assert check.version_number == 1
    assert check.practice_id == practice_id
    assert check.enabled is True
    assert check.demoted is False
    assert check.params == {"stale_days": 30}
    assert check.params_source == "default"
    assert check.affected_views == ["dbo.vw_test"]
    assert check.rationale == "Why this check exists."
    # fallback_template has no explicit value here -- the migration 0004
    # server_default backfills it, proving the loader surfaces that value
    # rather than silently defaulting to an empty string of its own.
    assert check.fallback_template == "This check has flagged a record for manual review."


@pytest.mark.parametrize("status", ("draft", "in_review", "rejected", "retired"))
def test_non_active_check_never_loads(conn: sa.Connection, status: str) -> None:
    check_id = _seed_check(conn, status=status)
    _seed_check_version(conn, check_id)
    practice_id = _seed_practice(conn)
    # Even with a practice config row present, a non-active check must never
    # surface -- proves the F3 gate is structural, not merely the common case.
    _seed_practice_check_config(conn, practice_id, check_id)

    assert load_active_checks(conn) == []


def test_loader_picks_latest_check_version(conn: sa.Connection) -> None:
    check_id = _seed_check(conn, status="active")
    _seed_check_version(conn, check_id, version_number=1)
    v2_id = _seed_check_version(conn, check_id, version_number=2)
    practice_id = _seed_practice(conn)
    _seed_practice_check_config(conn, practice_id, check_id)

    loaded = load_active_checks(conn)

    assert len(loaded) == 1
    assert loaded[0].check_version_id == v2_id
    assert loaded[0].version_number == 2


def test_loader_filters_by_practice_id(conn: sa.Connection) -> None:
    check_id = _seed_check(conn, status="active")
    _seed_check_version(conn, check_id)
    practice_a = _seed_practice(conn, "practice-a")
    practice_b = _seed_practice(conn, "practice-b")
    _seed_practice_check_config(conn, practice_a, check_id)
    _seed_practice_check_config(conn, practice_b, check_id)

    loaded = load_active_checks(conn, practice_id="practice-b")

    assert len(loaded) == 1
    assert loaded[0].practice_id == "practice-b"


def test_active_check_with_no_practice_config_loads_nothing(conn: sa.Connection) -> None:
    check_id = _seed_check(conn, status="active")
    _seed_check_version(conn, check_id)

    assert load_active_checks(conn) == []
