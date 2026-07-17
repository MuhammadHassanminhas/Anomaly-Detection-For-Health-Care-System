"""Phase 4 step 1: action library seed. Requires CDSS_APP_DB_URL and skips
(never fails) otherwise -- D-009.1. Every DB-gated test runs inside its own
rolled-back transaction (the `conn` fixture from tests/conftest.py).
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from cdss.action_library import CURATED_ACTIONS, KNOWN_ACTIONS, seed_action_library


def _seed_check(conn: sa.Connection, *, slug: str = "test-check") -> str:
    row = conn.execute(
        sa.text(
            "INSERT INTO checks (slug, title, category, default_severity, source, status) "
            "VALUES (:slug, 'Test Check', 'data-quality', 'medium', 'manual', 'active') "
            "RETURNING id"
        ),
        {"slug": slug},
    ).one()
    return str(row.id)


def test_known_actions_matches_curated_action_codes() -> None:
    assert frozenset(a.code for a in CURATED_ACTIONS) == KNOWN_ACTIONS


def test_no_duplicate_curated_codes() -> None:
    codes = [a.code for a in CURATED_ACTIONS]
    assert len(codes) == len(set(codes))


def test_seed_populates_every_curated_action(conn: sa.Connection) -> None:
    seed_action_library(conn)
    rows = conn.execute(sa.text("SELECT code, title, description FROM action_library")).all()
    seeded = {row.code: (row.title, row.description) for row in rows}
    assert set(seeded) == KNOWN_ACTIONS
    for action in CURATED_ACTIONS:
        assert seeded[action.code] == (action.title, action.description)


def test_seed_is_idempotent(conn: sa.Connection) -> None:
    seed_action_library(conn)
    seed_action_library(conn)
    count = conn.execute(sa.text("SELECT count(*) FROM action_library")).scalar_one()
    assert count == len(CURATED_ACTIONS)


def test_check_actions_rejects_unknown_action_code(conn: sa.Connection) -> None:
    seed_action_library(conn)
    check_id = _seed_check(conn)
    with pytest.raises(sa.exc.IntegrityError):
        conn.execute(
            sa.text(
                "INSERT INTO check_actions (check_id, action_code) "
                "VALUES (:check_id, 'not-a-real-action')"
            ),
            {"check_id": check_id},
        )


def test_check_actions_accepts_a_seeded_action_code(conn: sa.Connection) -> None:
    seed_action_library(conn)
    check_id = _seed_check(conn)
    action_code = next(iter(KNOWN_ACTIONS))
    conn.execute(
        sa.text(
            "INSERT INTO check_actions (check_id, action_code) VALUES (:check_id, :action_code)"
        ),
        {"check_id": check_id, "action_code": action_code},
    )
    count = conn.execute(sa.text("SELECT count(*) FROM check_actions")).scalar_one()
    assert count == 1
