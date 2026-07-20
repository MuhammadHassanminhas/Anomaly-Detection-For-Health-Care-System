"""Phase 6 step 1: the feedback service, `cdss.feedback`. All DB-gated
(every behavior needs the real `findings`/`finding_events` constraints,
not just Python logic) via the shared `conn` fixture -- CDSS_APP_DB_URL
required, skipped otherwise (D-009.1).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from cdss.feedback import (
    REASON_CODES,
    FindingNotFoundError,
    InvalidReasonCodeError,
    InvalidTransitionError,
    acknowledge,
    compute_reason_code_distribution,
    dismiss,
    reopen,
    snooze,
)


def test_reason_codes_is_the_expected_minimal_binary_set() -> None:
    assert {"genuine_issue", "not_genuine"} == REASON_CODES


def _seed_finding(conn: sa.Connection, *, dedupe_key: str = "dk-1") -> str:
    check_id = str(
        conn.execute(
            sa.text(
                "INSERT INTO checks (slug, title, category, default_severity, source, status) "
                "VALUES ('test-check', 'Test', 'data-quality', 'medium', 'manual', 'active') "
                "RETURNING id"
            )
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
                "VALUES (:check_id, 1, '{}'::jsonb, 'hash', ARRAY[]::text[], '{}'::jsonb) "
                "RETURNING id"
            ),
            {"check_id": check_id},
        )
        .one()
        .id
    )
    conn.execute(
        sa.text("INSERT INTO practices (practice_id, name) VALUES ('practice-1', 'Test Practice')")
    )
    run_id = str(conn.execute(sa.text("INSERT INTO runs DEFAULT VALUES RETURNING id")).one().id)
    finding_id = str(
        conn.execute(
            sa.text(
                "INSERT INTO findings "
                "(check_id, check_version_id, practice_id, dedupe_key, entity_key, "
                " status, severity, evidence, first_seen_run_id, last_seen_run_id) "
                "VALUES (:check_id, :version_id, 'practice-1', :dedupe_key, '{}'::jsonb, "
                " 'open', 'medium', '{}'::jsonb, :run_id, :run_id) "
                "RETURNING id"
            ),
            {
                "check_id": check_id,
                "version_id": version_id,
                "dedupe_key": dedupe_key,
                "run_id": run_id,
            },
        )
        .one()
        .id
    )
    return finding_id


def _events(conn: sa.Connection, finding_id: str) -> list[sa.Row[tuple[object, ...]]]:
    return conn.execute(
        sa.text(
            "SELECT event, reason_code, actor, note, run_id FROM finding_events "
            "WHERE finding_id = :id ORDER BY occurred_at"
        ),
        {"id": finding_id},
    ).all()


def _finding(conn: sa.Connection, finding_id: str) -> sa.Row[tuple[object, ...]]:
    return conn.execute(
        sa.text("SELECT status, snoozed_until FROM findings WHERE id = :id"), {"id": finding_id}
    ).one()


def test_dismiss_sets_status_and_writes_event_with_reason_actor_note(conn: sa.Connection) -> None:
    finding_id = _seed_finding(conn)
    result = dismiss(
        conn,
        finding_id,
        reason_code="genuine_issue",
        actor="alice",
        note="confirmed by chart review",
    )
    assert result.status == "dismissed"
    assert _finding(conn, finding_id).status == "dismissed"
    events = _events(conn, finding_id)
    assert len(events) == 1
    assert events[0].event == "dismissed"
    assert events[0].reason_code == "genuine_issue"
    assert events[0].actor == "alice"
    assert events[0].note == "confirmed by chart review"


def test_dismiss_rejects_invalid_reason_code_before_any_write(conn: sa.Connection) -> None:
    finding_id = _seed_finding(conn)
    with pytest.raises(InvalidReasonCodeError):
        dismiss(conn, finding_id, reason_code="not_an_issue", actor="alice")
    assert _finding(conn, finding_id).status == "open"
    assert _events(conn, finding_id) == []


def test_dismiss_unknown_finding_raises(conn: sa.Connection) -> None:
    with pytest.raises(FindingNotFoundError):
        dismiss(
            conn,
            "00000000-0000-0000-0000-000000000000",
            reason_code="genuine_issue",
            actor="alice",
        )


def test_acknowledge_sets_status_and_writes_event(conn: sa.Connection) -> None:
    finding_id = _seed_finding(conn)
    result = acknowledge(conn, finding_id, actor="alice")
    assert result.status == "acknowledged"
    assert _finding(conn, finding_id).status == "acknowledged"
    events = _events(conn, finding_id)
    assert len(events) == 1 and events[0].event == "acknowledged"


def test_snooze_sets_snoozed_until_leaves_status_untouched(conn: sa.Connection) -> None:
    finding_id = _seed_finding(conn)
    until = datetime(2026, 8, 1, tzinfo=UTC)
    result = snooze(conn, finding_id, until, actor="alice")
    assert result.status == "open"
    row = _finding(conn, finding_id)
    assert row.status == "open"
    assert row.snoozed_until == until
    events = _events(conn, finding_id)
    assert len(events) == 1 and events[0].event == "snoozed"


def test_reopen_from_dismissed_clears_status_to_open(conn: sa.Connection) -> None:
    finding_id = _seed_finding(conn)
    dismiss(conn, finding_id, reason_code="genuine_issue", actor="alice")
    result = reopen(conn, finding_id, actor="bob")
    assert result.status == "open"
    assert _finding(conn, finding_id).status == "open"
    events = _events(conn, finding_id)
    assert [e.event for e in events] == ["dismissed", "reopened"]


def test_reopen_from_snoozed_clears_snoozed_until(conn: sa.Connection) -> None:
    finding_id = _seed_finding(conn)
    snooze(conn, finding_id, datetime(2026, 8, 1, tzinfo=UTC), actor="alice")
    reopen(conn, finding_id, actor="bob")
    row = _finding(conn, finding_id)
    assert row.status == "open"
    assert row.snoozed_until is None


def test_reopen_on_open_not_snoozed_finding_raises(conn: sa.Connection) -> None:
    finding_id = _seed_finding(conn)
    with pytest.raises(InvalidTransitionError):
        reopen(conn, finding_id, actor="bob")
    assert _events(conn, finding_id) == []


def test_run_id_optional_and_recorded_when_given(conn: sa.Connection) -> None:
    finding_id = _seed_finding(conn)
    run_id = str(conn.execute(sa.text("INSERT INTO runs DEFAULT VALUES RETURNING id")).one().id)
    acknowledge(conn, finding_id, actor="alice", run_id=run_id)
    events = _events(conn, finding_id)
    assert str(events[0].run_id) == run_id


def _seed_check_for_slug(conn: sa.Connection, *, slug: str) -> tuple[str, str]:
    check_id = str(
        conn.execute(
            sa.text(
                "INSERT INTO checks (slug, title, category, default_severity, source, status) "
                "VALUES (:slug, 'Test', 'data-quality', 'medium', 'manual', 'active') "
                "RETURNING id"
            ),
            {"slug": slug},
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
                "VALUES (:check_id, 1, '{}'::jsonb, 'hash', ARRAY[]::text[], '{}'::jsonb) "
                "RETURNING id"
            ),
            {"check_id": check_id},
        )
        .one()
        .id
    )
    conn.execute(
        sa.text(
            "INSERT INTO practices (practice_id, name) VALUES ('practice-1', 'Test Practice') "
            "ON CONFLICT (practice_id) DO NOTHING"
        )
    )
    return check_id, version_id


def _seed_finding_for_check(
    conn: sa.Connection, *, check_id: str, version_id: str, dedupe_key: str
) -> str:
    run_id = str(conn.execute(sa.text("INSERT INTO runs DEFAULT VALUES RETURNING id")).one().id)
    return str(
        conn.execute(
            sa.text(
                "INSERT INTO findings "
                "(check_id, check_version_id, practice_id, dedupe_key, entity_key, "
                " status, severity, evidence, first_seen_run_id, last_seen_run_id) "
                "VALUES (:check_id, :version_id, 'practice-1', :dedupe_key, '{}'::jsonb, "
                " 'open', 'medium', '{}'::jsonb, :run_id, :run_id) "
                "RETURNING id"
            ),
            {
                "check_id": check_id,
                "version_id": version_id,
                "dedupe_key": dedupe_key,
                "run_id": run_id,
            },
        )
        .one()
        .id
    )


def test_compute_reason_code_distribution_counts_per_check(conn: sa.Connection) -> None:
    check_a, version_a = _seed_check_for_slug(conn, slug="check-a")
    check_b, version_b = _seed_check_for_slug(conn, slug="check-b")
    finding_a1 = _seed_finding_for_check(
        conn, check_id=check_a, version_id=version_a, dedupe_key="a1"
    )
    finding_a2 = _seed_finding_for_check(
        conn, check_id=check_a, version_id=version_a, dedupe_key="a2"
    )
    finding_a3 = _seed_finding_for_check(
        conn, check_id=check_a, version_id=version_a, dedupe_key="a3"
    )
    finding_b1 = _seed_finding_for_check(
        conn, check_id=check_b, version_id=version_b, dedupe_key="b1"
    )
    dismiss(conn, finding_a1, reason_code="genuine_issue", actor="alice")
    dismiss(conn, finding_a2, reason_code="genuine_issue", actor="alice")
    dismiss(conn, finding_a3, reason_code="not_genuine", actor="alice")
    dismiss(conn, finding_b1, reason_code="not_genuine", actor="alice")

    distribution = {e.slug: e for e in compute_reason_code_distribution(conn)}

    assert distribution["check-a"].genuine_issue_count == 2
    assert distribution["check-a"].not_genuine_count == 1
    assert distribution["check-b"].genuine_issue_count == 0
    assert distribution["check-b"].not_genuine_count == 1


def test_compute_reason_code_distribution_omits_check_with_no_dismissals(
    conn: sa.Connection,
) -> None:
    check_id, version_id = _seed_check_for_slug(conn, slug="check-never-dismissed")
    finding_id = _seed_finding_for_check(
        conn, check_id=check_id, version_id=version_id, dedupe_key="x1"
    )
    acknowledge(conn, finding_id, actor="alice")  # a non-dismissal event, no reason_code

    distribution = compute_reason_code_distribution(conn)

    assert "check-never-dismissed" not in {e.slug for e in distribution}
