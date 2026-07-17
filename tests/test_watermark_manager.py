"""Phase 3 step 4: watermark manager tests -- first run (no watermark ⇒
initial full window), incremental run, and both fallback paths.

get_watermark/set_watermark tests require CDSS_APP_DB_URL and skip (never
fail) otherwise -- D-009.1 -- using the shared `conn` fixture (rolled back
after each test). The window/hash/escalation functions are pure and need no
database at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from cdss.watermark_manager import (
    ScanWindow,
    compute_bounded_full_scan_window,
    compute_entity_key_hash,
    compute_watermarked_window,
    get_watermark,
    get_watermark_hash,
    has_entity_key_set_changed,
    set_watermark,
    set_watermark_hash,
    should_escalate_to_ask,
)

_NOW = datetime(2026, 7, 17, 12, 0, 0, tzinfo=UTC)

# --- persisted watermark read/write (DB-gated) -----------------------------


def test_get_watermark_returns_none_when_never_set(conn: sa.Connection) -> None:
    assert get_watermark(conn, "dbo.vw_appointments", "UpdatedAt") is None


def test_set_then_get_watermark_round_trips(conn: sa.Connection) -> None:
    value = datetime(2026, 7, 1, 8, 30, 0, tzinfo=UTC)
    set_watermark(conn, "dbo.vw_appointments", "UpdatedAt", value)
    assert get_watermark(conn, "dbo.vw_appointments", "UpdatedAt") == value


def test_set_watermark_twice_upserts_not_duplicates(conn: sa.Connection) -> None:
    first = datetime(2026, 7, 1, 8, 30, 0, tzinfo=UTC)
    second = datetime(2026, 7, 2, 9, 0, 0, tzinfo=UTC)
    set_watermark(conn, "dbo.vw_appointments", "UpdatedAt", first)
    set_watermark(conn, "dbo.vw_appointments", "UpdatedAt", second)

    assert get_watermark(conn, "dbo.vw_appointments", "UpdatedAt") == second
    rows = conn.execute(
        sa.text(
            "SELECT count(*) AS n FROM watermarks "
            "WHERE view_name = 'dbo.vw_appointments' AND column_name = 'UpdatedAt'"
        )
    ).one()
    assert rows.n == 1


def test_watermarks_are_independent_per_view_and_column(conn: sa.Connection) -> None:
    set_watermark(conn, "dbo.vw_appointments", "UpdatedAt", datetime(2026, 1, 1, tzinfo=UTC))
    set_watermark(conn, "dbo.vw_appointments", "InsertedAt", datetime(2026, 2, 1, tzinfo=UTC))
    set_watermark(conn, "dbo.vw_invoices", "UpdatedAt", datetime(2026, 3, 1, tzinfo=UTC))

    assert get_watermark(conn, "dbo.vw_appointments", "UpdatedAt") == datetime(
        2026, 1, 1, tzinfo=UTC
    )
    assert get_watermark(conn, "dbo.vw_appointments", "InsertedAt") == datetime(
        2026, 2, 1, tzinfo=UTC
    )
    assert get_watermark(conn, "dbo.vw_invoices", "UpdatedAt") == datetime(2026, 3, 1, tzinfo=UTC)


# --- persisted entity-key-set hash read/write (DB-gated, fallback b) -------


def test_get_watermark_hash_returns_none_when_never_set(conn: sa.Connection) -> None:
    assert get_watermark_hash(conn, "dbo.vw_immunisation") is None


def test_set_then_get_watermark_hash_round_trips(conn: sa.Connection) -> None:
    set_watermark_hash(conn, "dbo.vw_immunisation", "abc123")
    assert get_watermark_hash(conn, "dbo.vw_immunisation") == "abc123"


def test_set_watermark_hash_twice_upserts_not_duplicates(conn: sa.Connection) -> None:
    set_watermark_hash(conn, "dbo.vw_immunisation", "first-hash")
    set_watermark_hash(conn, "dbo.vw_immunisation", "second-hash")

    assert get_watermark_hash(conn, "dbo.vw_immunisation") == "second-hash"
    rows = conn.execute(
        sa.text(
            "SELECT count(*) AS n FROM watermarks "
            "WHERE view_name = 'dbo.vw_immunisation' AND column_name = '__entity_key_hash__'"
        )
    ).one()
    assert rows.n == 1


def test_watermark_hash_is_independent_per_view(conn: sa.Connection) -> None:
    set_watermark_hash(conn, "dbo.vw_immunisation", "hash-a")
    set_watermark_hash(conn, "dbo.vw_patientalerts", "hash-b")

    assert get_watermark_hash(conn, "dbo.vw_immunisation") == "hash-a"
    assert get_watermark_hash(conn, "dbo.vw_patientalerts") == "hash-b"


def test_watermark_hash_does_not_collide_with_a_real_watermark_column(
    conn: sa.Connection,
) -> None:
    set_watermark(conn, "dbo.vw_immunisation", "UpdatedAt", datetime(2026, 1, 1, tzinfo=UTC))
    set_watermark_hash(conn, "dbo.vw_immunisation", "hash-a")

    assert get_watermark(conn, "dbo.vw_immunisation", "UpdatedAt") == datetime(
        2026, 1, 1, tzinfo=UTC
    )
    assert get_watermark_hash(conn, "dbo.vw_immunisation") == "hash-a"


def test_fallback_b_end_to_end_first_run_then_unchanged_then_changed(
    conn: sa.Connection,
) -> None:
    view = "dbo.vw_immunisation"

    # First run: no previous hash recorded yet -- always counts as changed.
    previous = get_watermark_hash(conn, view)
    current = compute_entity_key_hash([(1,), (2,), (3,)])
    assert has_entity_key_set_changed(previous_hash=previous, current_hash=current) is True
    set_watermark_hash(conn, view, current)

    # Second run: same entity-key set (fetched in a different order) -- unchanged.
    previous = get_watermark_hash(conn, view)
    current = compute_entity_key_hash([(3,), (1,), (2,)])
    assert has_entity_key_set_changed(previous_hash=previous, current_hash=current) is False
    set_watermark_hash(conn, view, current)

    # Third run: a new key appears -- changed.
    previous = get_watermark_hash(conn, view)
    current = compute_entity_key_hash([(1,), (2,), (3,), (4,)])
    assert has_entity_key_set_changed(previous_hash=previous, current_hash=current) is True


# --- watermarked window: first run + incremental (pure) --------------------


def test_first_run_has_no_lower_bound() -> None:
    window = compute_watermarked_window(watermark=None, lookback=None, now=_NOW)
    assert window == ScanWindow(from_ts=None, to_ts=_NOW)


def test_first_run_has_no_lower_bound_even_with_lookback_declared() -> None:
    window = compute_watermarked_window(watermark=None, lookback=timedelta(days=3), now=_NOW)
    assert window == ScanWindow(from_ts=None, to_ts=_NOW)


def test_incremental_run_without_lookback_starts_at_watermark() -> None:
    watermark = datetime(2026, 7, 10, tzinfo=UTC)
    window = compute_watermarked_window(watermark=watermark, lookback=None, now=_NOW)
    assert window == ScanWindow(from_ts=watermark, to_ts=_NOW)


def test_incremental_run_with_lookback_widens_backward() -> None:
    watermark = datetime(2026, 7, 10, tzinfo=UTC)
    lookback = timedelta(days=2)
    window = compute_watermarked_window(watermark=watermark, lookback=lookback, now=_NOW)
    assert window == ScanWindow(from_ts=watermark - lookback, to_ts=_NOW)


# --- fallback (a): bounded full scan (pure) --------------------------------


def test_bounded_full_scan_window_spans_lookback_to_now() -> None:
    lookback = timedelta(days=7)
    window = compute_bounded_full_scan_window(lookback=lookback, now=_NOW)
    assert window == ScanWindow(from_ts=_NOW - lookback, to_ts=_NOW)


# --- fallback (b): snapshot-hash diff (pure) -------------------------------


def test_entity_key_hash_is_order_independent() -> None:
    forward = compute_entity_key_hash([(1,), (2,), (3,)])
    reversed_order = compute_entity_key_hash([(3,), (1,), (2,)])
    assert forward == reversed_order


def test_entity_key_hash_changes_when_keys_change() -> None:
    original = compute_entity_key_hash([(1,), (2,), (3,)])
    changed = compute_entity_key_hash([(1,), (2,), (4,)])
    assert original != changed


def test_entity_key_hash_is_deterministic_across_calls() -> None:
    keys = [("practice-1", 42), ("practice-1", 43)]
    assert compute_entity_key_hash(keys) == compute_entity_key_hash(keys)


def test_no_previous_hash_counts_as_changed() -> None:
    assert has_entity_key_set_changed(previous_hash=None, current_hash="abc123") is True


def test_matching_hash_counts_as_unchanged() -> None:
    assert has_entity_key_set_changed(previous_hash="abc123", current_hash="abc123") is False


def test_differing_hash_counts_as_changed() -> None:
    assert has_entity_key_set_changed(previous_hash="abc123", current_hash="def456") is True


# --- hot + unwatermarkable escalation (pure) --------------------------------


def test_hot_and_unwatermarkable_escalates() -> None:
    assert should_escalate_to_ask(is_watermarkable=False, is_hot=True) is True


def test_watermarkable_never_escalates_even_if_hot() -> None:
    assert should_escalate_to_ask(is_watermarkable=True, is_hot=True) is False


def test_unwatermarkable_but_not_hot_does_not_escalate() -> None:
    assert should_escalate_to_ask(is_watermarkable=False, is_hot=False) is False


def test_watermarkable_and_not_hot_does_not_escalate() -> None:
    assert should_escalate_to_ask(is_watermarkable=True, is_hot=False) is False
