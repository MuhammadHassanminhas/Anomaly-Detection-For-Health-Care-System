"""Watermark manager (Phase 3 step 4): computes the source-DB scan window
for a check's driving view on this run, and persists the watermark's
last-seen value in the app DB's `watermarks` table (Phase 3 step 1 schema).

Three scoping strategies exist, per ARCHITECTURE.md/the phase spec, chosen by
the caller per view -- there is no per-view config table in the Phase 3 step 1
schema yet, so which strategy applies is an argument the caller (the step 5
executor) supplies, not something this module looks up itself:

  - **watermarked**: increment scope is `watermark - lookback` through `now`;
    a first run (no watermark row yet) has no lower bound at all -- a full
    scan (`compute_watermarked_window`).
  - **bounded_full_scan** (fallback a): the view has no usable watermark
    column (Phase 1 classification: fallback_needed). No watermark is read
    or persisted; every run scans the same trailing window, `now - lookback`
    through `now` (`compute_bounded_full_scan_window`).
  - **snapshot_hash_diff** (fallback b): entity-key-set hashing primitives
    (`compute_entity_key_hash`, `has_entity_key_set_changed`) plus
    `get_watermark_hash`/`set_watermark_hash`, which persist the previous
    hash in `watermarks.last_hash` (migration 0002 -- `last_value` is
    TIMESTAMPTZ and can't hold one). A hash is keyed on `view_name` alone
    (the hash covers the whole view's entity-key set, not any one column),
    stored under the synthetic `column_name` sentinel `_ENTITY_KEY_HASH_COLUMN`
    so it shares `watermarks`' existing (view_name, column_name) primary key
    without colliding with any real source column name.

`should_escalate_to_ask` is the pure decision rule behind ARCHITECTURE.md's
"a view that is both hot and unwatermarkable triggers an ASK-NNN
recommendation" -- surfacing it in the actual cost report is step 8's job.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa

_SELECT_WATERMARK_SQL = sa.text(
    "SELECT last_value FROM watermarks WHERE view_name = :view_name AND column_name = :column_name"
)

_UPSERT_WATERMARK_SQL = sa.text(
    """
    INSERT INTO watermarks (view_name, column_name, last_value, updated_at)
    VALUES (:view_name, :column_name, :last_value, now())
    ON CONFLICT (view_name, column_name)
    DO UPDATE SET last_value = EXCLUDED.last_value, updated_at = now()
    """
)

# Fallback (b) hashes are per-view, not per-column -- stored under this
# sentinel column_name so they share watermarks' (view_name, column_name)
# primary key without ever colliding with a real source column name.
_ENTITY_KEY_HASH_COLUMN = "__entity_key_hash__"

_SELECT_WATERMARK_HASH_SQL = sa.text(
    "SELECT last_hash FROM watermarks WHERE view_name = :view_name AND column_name = :column_name"
)

_UPSERT_WATERMARK_HASH_SQL = sa.text(
    """
    INSERT INTO watermarks (view_name, column_name, last_hash, updated_at)
    VALUES (:view_name, :column_name, :last_hash, now())
    ON CONFLICT (view_name, column_name)
    DO UPDATE SET last_hash = EXCLUDED.last_hash, updated_at = now()
    """
)


@dataclass(frozen=True)
class ScanWindow:
    """The scope of one increment: `from_ts` is the (exclusive) lower bound,
    or `None` for no lower bound at all -- a full scan. `to_ts` is the
    (inclusive) upper bound, always `now`."""

    from_ts: datetime | None
    to_ts: datetime


def get_watermark(conn: sa.Connection, view_name: str, column_name: str) -> datetime | None:
    """The current persisted watermark for (view, column), or None if this
    (view, column) has never had one recorded -- the first-run signal."""
    row = conn.execute(
        _SELECT_WATERMARK_SQL, {"view_name": view_name, "column_name": column_name}
    ).one_or_none()
    return row.last_value if row is not None else None


def set_watermark(conn: sa.Connection, view_name: str, column_name: str, value: datetime) -> None:
    """Upsert the watermark for (view, column). Participates in the caller's
    transaction (unlike the step 2 audit sink) so a failed run's watermark
    advance rolls back with everything else it wrote."""
    conn.execute(
        _UPSERT_WATERMARK_SQL,
        {"view_name": view_name, "column_name": column_name, "last_value": value},
    )


def compute_watermarked_window(
    *, watermark: datetime | None, lookback: timedelta | None, now: datetime
) -> ScanWindow:
    """First run (`watermark` is None): no lower bound at all -- the initial
    full window. Incremental run: the lower bound is the watermark widened
    backward by the check's declared lookback (if any), to re-catch
    late-arriving updates near the previous boundary."""
    if watermark is None:
        return ScanWindow(from_ts=None, to_ts=now)
    from_ts = watermark - lookback if lookback is not None else watermark
    return ScanWindow(from_ts=from_ts, to_ts=now)


def compute_bounded_full_scan_window(*, lookback: timedelta, now: datetime) -> ScanWindow:
    """Fallback (a): no watermark column exists to read at all, so no
    watermark is read or persisted -- every run scans the same trailing
    window relative to `now`."""
    return ScanWindow(from_ts=now - lookback, to_ts=now)


def get_watermark_hash(conn: sa.Connection, view_name: str) -> str | None:
    """The previously recorded entity-key-set hash for `view_name`, or None
    if one has never been recorded -- the first-run signal for fallback (b),
    same meaning as `get_watermark` returning None for the watermarked path."""
    row = conn.execute(
        _SELECT_WATERMARK_HASH_SQL,
        {"view_name": view_name, "column_name": _ENTITY_KEY_HASH_COLUMN},
    ).one_or_none()
    return row.last_hash if row is not None else None


def set_watermark_hash(conn: sa.Connection, view_name: str, value: str) -> None:
    """Upsert the entity-key-set hash for `view_name`. Participates in the
    caller's transaction, same as `set_watermark`."""
    conn.execute(
        _UPSERT_WATERMARK_HASH_SQL,
        {"view_name": view_name, "column_name": _ENTITY_KEY_HASH_COLUMN, "last_hash": value},
    )


def compute_entity_key_hash(entity_keys: Iterable[tuple[object, ...]]) -> str:
    """Fallback (b) building block: a deterministic hash of an entity-key
    set, order-independent (keys are sorted before hashing) so re-fetching
    the same set in a different row order still hashes identically."""
    canonical = sorted(str(key) for key in entity_keys)
    digest = hashlib.sha256("\n".join(canonical).encode("utf-8"))
    return digest.hexdigest()


def has_entity_key_set_changed(*, previous_hash: str | None, current_hash: str) -> bool:
    """Fallback (b): whether the entity-key set changed since the last
    recorded hash. A missing previous hash (first run) always counts as
    changed -- there's nothing to diff against yet."""
    return previous_hash != current_hash


def should_escalate_to_ask(*, is_watermarkable: bool, is_hot: bool) -> bool:
    """A view that is both hot and unwatermarkable can't be served cheaply
    by either fallback strategy -- the signal the cost report (step 8) turns
    into an ASK-NNN recommendation, never a base-table workaround."""
    return is_hot and not is_watermarkable


__all__ = [
    "ScanWindow",
    "compute_bounded_full_scan_window",
    "compute_entity_key_hash",
    "compute_watermarked_window",
    "get_watermark",
    "get_watermark_hash",
    "has_entity_key_set_changed",
    "set_watermark",
    "set_watermark_hash",
    "should_escalate_to_ask",
]
