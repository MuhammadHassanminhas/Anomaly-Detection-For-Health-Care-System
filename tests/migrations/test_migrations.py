"""Deliverable 2 of Phase 3 step 1: constraint tests -- each violation must
be rejected by PostgreSQL itself, not merely by application code. Every test
runs inside its own transaction (the `conn` fixture) that's rolled back
after the test, so seed data never leaks between tests or into other
suites.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

_FINDING_COLUMNS = (
    "check_id, check_version_id, practice_id, dedupe_key, entity_key, "
    "severity, evidence, first_seen_run_id, last_seen_run_id"
)


def _seed_check(conn: sa.Connection) -> str:
    row = conn.execute(
        sa.text(
            "INSERT INTO checks (slug, title, category, default_severity, source, status) "
            "VALUES ('test-check', 'Test Check', 'data-quality', 'medium', 'manual', 'active') "
            "RETURNING id"
        )
    ).one()
    return str(row.id)


def _seed_check_version(conn: sa.Connection, check_id: str) -> str:
    row = conn.execute(
        sa.text(
            "INSERT INTO check_versions "
            "(check_id, version_number, definition, definition_hash, affected_views, "
            "params_schema) "
            "VALUES (:check_id, 1, '{}'::jsonb, 'hash', ARRAY[]::text[], '{}'::jsonb) "
            "RETURNING id"
        ),
        {"check_id": check_id},
    ).one()
    return str(row.id)


def _seed_practice(conn: sa.Connection) -> str:
    conn.execute(
        sa.text("INSERT INTO practices (practice_id, name) VALUES ('practice-1', 'Test Practice')")
    )
    return "practice-1"


def _seed_run(conn: sa.Connection) -> str:
    row = conn.execute(sa.text("INSERT INTO runs DEFAULT VALUES RETURNING id")).one()
    return str(row.id)


def _seed_finding(conn: sa.Connection, *, dedupe_key: str = "dk-1") -> str:
    check_id = _seed_check(conn)
    version_id = _seed_check_version(conn, check_id)
    practice_id = _seed_practice(conn)
    run_id = _seed_run(conn)
    row = conn.execute(
        sa.text(
            f"INSERT INTO findings ({_FINDING_COLUMNS}) VALUES "
            "(:check_id, :version_id, :practice_id, :dedupe_key, '{}'::jsonb, "
            "'medium', '{}'::jsonb, :run_id, :run_id) RETURNING id"
        ),
        {
            "check_id": check_id,
            "version_id": version_id,
            "practice_id": practice_id,
            "dedupe_key": dedupe_key,
            "run_id": run_id,
        },
    ).one()
    return str(row.id)


def test_findings_check_version_id_not_null(conn: sa.Connection) -> None:
    check_id = _seed_check(conn)
    practice_id = _seed_practice(conn)
    run_id = _seed_run(conn)
    with pytest.raises(sa.exc.IntegrityError):
        conn.execute(
            sa.text(
                f"INSERT INTO findings ({_FINDING_COLUMNS}) VALUES "
                "(:check_id, NULL, :practice_id, 'dk', '{}'::jsonb, 'medium', '{}'::jsonb, "
                ":run_id, :run_id)"
            ),
            {"check_id": check_id, "practice_id": practice_id, "run_id": run_id},
        )


def test_findings_unique_check_dedupe_key(conn: sa.Connection) -> None:
    check_id = _seed_check(conn)
    version_id = _seed_check_version(conn, check_id)
    practice_id = _seed_practice(conn)
    run_id = _seed_run(conn)
    values = {
        "check_id": check_id,
        "version_id": version_id,
        "practice_id": practice_id,
        "run_id": run_id,
    }
    insert = sa.text(
        f"INSERT INTO findings ({_FINDING_COLUMNS}) VALUES "
        "(:check_id, :version_id, :practice_id, 'dup-key', '{}'::jsonb, 'medium', '{}'::jsonb, "
        ":run_id, :run_id)"
    )
    conn.execute(insert, values)
    with pytest.raises(sa.exc.IntegrityError):
        conn.execute(insert, values)


def test_finding_events_dismissed_requires_reason_code(conn: sa.Connection) -> None:
    finding_id = _seed_finding(conn)
    with pytest.raises(sa.exc.IntegrityError):
        conn.execute(
            sa.text("INSERT INTO finding_events (finding_id, event) VALUES (:fid, 'dismissed')"),
            {"fid": finding_id},
        )


def test_finding_events_dismissed_with_reason_code_accepted(conn: sa.Connection) -> None:
    finding_id = _seed_finding(conn)
    conn.execute(
        sa.text(
            "INSERT INTO finding_events (finding_id, event, reason_code) "
            "VALUES (:fid, 'dismissed', 'not_an_issue')"
        ),
        {"fid": finding_id},
    )


def test_finding_events_append_only_update_rejected(conn: sa.Connection) -> None:
    finding_id = _seed_finding(conn)
    event_id = (
        conn.execute(
            sa.text(
                "INSERT INTO finding_events (finding_id, event) VALUES (:fid, 'created') RETURNING id"
            ),
            {"fid": finding_id},
        )
        .one()
        .id
    )
    with pytest.raises(sa.exc.DBAPIError):
        conn.execute(
            sa.text("UPDATE finding_events SET reason_code = 'x' WHERE id = :id"),
            {"id": event_id},
        )


def test_finding_events_append_only_delete_rejected(conn: sa.Connection) -> None:
    finding_id = _seed_finding(conn)
    event_id = (
        conn.execute(
            sa.text(
                "INSERT INTO finding_events (finding_id, event) VALUES (:fid, 'created') RETURNING id"
            ),
            {"fid": finding_id},
        )
        .one()
        .id
    )
    with pytest.raises(sa.exc.DBAPIError):
        conn.execute(sa.text("DELETE FROM finding_events WHERE id = :id"), {"id": event_id})
