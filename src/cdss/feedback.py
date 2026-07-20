"""Phase 6 step 1: the feedback service (`cdss.feedback`).

`dismiss`/`acknowledge`/`snooze`/`reopen` are the front door the API
(Phase 9) will call -- each pairs a `findings.status` transition with an
append-only `finding_events` row, matching `cdss.materialize`'s own
"update the finding, insert the event, same connection, caller owns the
transaction" convention (`materialize.py`'s `_INSERT_EVENT_SQL` pattern).

Reason-code enforcement is two layers deep, same defense-in-depth pattern
already used for the Tier M/Tier S redaction boundaries elsewhere in this
codebase: `REASON_CODES` rejects an invalid code here, *before* any write,
and migration 0006's `ck_finding_events_reason_code` is the DB-level
backstop the phase spec's precondition names.

`reopen()` applies to a finding a human previously dismissed, or one
currently snoozed (regardless of its underlying status) -- it always
clears `snoozed_until` and sets `status='open'`. Calling it on anything
else (an `open`/`acknowledged`/`resolved`/`resolved_system` finding that
isn't snoozed) is a no-op state, so it raises rather than silently
succeeding.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa

REASON_CODES = frozenset({"genuine_issue", "not_genuine"})


class FindingNotFoundError(ValueError):
    """No `findings` row matches the given `finding_id`."""


class InvalidReasonCodeError(ValueError):
    """`dismiss()` was given a `reason_code` outside `REASON_CODES`."""


class InvalidTransitionError(ValueError):
    """`reopen()` was called on a finding that is neither dismissed nor snoozed."""


@dataclass(frozen=True)
class TransitionResult:
    finding_id: str
    event: str
    status: str


_SELECT_FINDING_SQL = sa.text(
    "SELECT id, status, snoozed_until FROM findings WHERE id = :finding_id"
)

_UPDATE_STATUS_SQL = sa.text(
    "UPDATE findings SET status = :status, updated_at = now() WHERE id = :finding_id"
)

_UPDATE_SNOOZE_SQL = sa.text(
    "UPDATE findings SET snoozed_until = :until, updated_at = now() WHERE id = :finding_id"
)

_UPDATE_REOPEN_SQL = sa.text(
    "UPDATE findings SET status = 'open', snoozed_until = NULL, updated_at = now() "
    "WHERE id = :finding_id"
)

_INSERT_EVENT_SQL = sa.text(
    "INSERT INTO finding_events (finding_id, event, reason_code, actor, note, run_id) "
    "VALUES (:finding_id, :event, :reason_code, :actor, :note, :run_id)"
)


def _fetch_finding(conn: sa.Connection, finding_id: str) -> sa.Row[tuple[object, ...]]:
    row = conn.execute(_SELECT_FINDING_SQL, {"finding_id": finding_id}).one_or_none()
    if row is None:
        raise FindingNotFoundError(f"no finding with id {finding_id!r}")
    return row


def _record_event(
    conn: sa.Connection,
    *,
    finding_id: str,
    event: str,
    actor: str,
    reason_code: str | None = None,
    note: str | None = None,
    run_id: str | None = None,
) -> None:
    conn.execute(
        _INSERT_EVENT_SQL,
        {
            "finding_id": finding_id,
            "event": event,
            "reason_code": reason_code,
            "actor": actor,
            "note": note,
            "run_id": run_id,
        },
    )


def dismiss(
    conn: sa.Connection,
    finding_id: str,
    *,
    reason_code: str,
    actor: str,
    note: str | None = None,
    run_id: str | None = None,
) -> TransitionResult:
    """Dismiss a finding. Rejects an invalid `reason_code` before any write
    -- `InvalidReasonCodeError` is the front door, migration 0006's
    `ck_finding_events_reason_code` CHECK is the backstop."""
    if reason_code not in REASON_CODES:
        raise InvalidReasonCodeError(f"reason_code {reason_code!r} not in {sorted(REASON_CODES)}")
    _fetch_finding(conn, finding_id)
    conn.execute(_UPDATE_STATUS_SQL, {"finding_id": finding_id, "status": "dismissed"})
    _record_event(
        conn,
        finding_id=finding_id,
        event="dismissed",
        actor=actor,
        reason_code=reason_code,
        note=note,
        run_id=run_id,
    )
    return TransitionResult(finding_id=str(finding_id), event="dismissed", status="dismissed")


def acknowledge(
    conn: sa.Connection, finding_id: str, *, actor: str, run_id: str | None = None
) -> TransitionResult:
    _fetch_finding(conn, finding_id)
    conn.execute(_UPDATE_STATUS_SQL, {"finding_id": finding_id, "status": "acknowledged"})
    _record_event(conn, finding_id=finding_id, event="acknowledged", actor=actor, run_id=run_id)
    return TransitionResult(finding_id=str(finding_id), event="acknowledged", status="acknowledged")


def snooze(
    conn: sa.Connection,
    finding_id: str,
    until: datetime,
    *,
    actor: str,
    run_id: str | None = None,
) -> TransitionResult:
    """Sets `snoozed_until` only -- `status` is left untouched (ARCHITECTURE.md:
    snooze is a queue-visibility filter orthogonal to status, not a status
    transition of its own)."""
    finding = _fetch_finding(conn, finding_id)
    conn.execute(_UPDATE_SNOOZE_SQL, {"finding_id": finding_id, "until": until})
    _record_event(conn, finding_id=finding_id, event="snoozed", actor=actor, run_id=run_id)
    return TransitionResult(finding_id=str(finding_id), event="snoozed", status=str(finding.status))


def reopen(
    conn: sa.Connection, finding_id: str, *, actor: str, run_id: str | None = None
) -> TransitionResult:
    """Valid only for a `dismissed` finding or one currently snoozed
    (`snoozed_until IS NOT NULL`, any status) -- anything else has nothing
    to reopen, so this raises rather than silently no-op-ing."""
    finding = _fetch_finding(conn, finding_id)
    if finding.status != "dismissed" and finding.snoozed_until is None:
        raise InvalidTransitionError(
            f"finding {finding_id!r} is not dismissed or snoozed (status={finding.status!r})"
        )
    conn.execute(_UPDATE_REOPEN_SQL, {"finding_id": finding_id})
    _record_event(conn, finding_id=finding_id, event="reopened", actor=actor, run_id=run_id)
    return TransitionResult(finding_id=str(finding_id), event="reopened", status="open")


@dataclass(frozen=True)
class ReasonCodeDistributionEntry:
    """Phase 6 step 5's own named deliverable input: how a check's dismissals
    split across `REASON_CODES`, all-time, not scoped to one run -- a check
    dismissed mostly `not_genuine` is a calibration/design candidate, not a
    demotion one (precision already covers the demotion signal, step 3)."""

    check_id: str
    slug: str
    genuine_issue_count: int
    not_genuine_count: int


_SELECT_REASON_CODE_DISTRIBUTION_SQL = sa.text(
    """
    SELECT c.id AS check_id, c.slug,
           COUNT(*) FILTER (WHERE fe.reason_code = 'genuine_issue') AS genuine_issue_count,
           COUNT(*) FILTER (WHERE fe.reason_code = 'not_genuine') AS not_genuine_count
    FROM finding_events fe
    JOIN findings f ON f.id = fe.finding_id
    JOIN checks c ON c.id = f.check_id
    WHERE fe.event = 'dismissed' AND fe.reason_code IS NOT NULL
    GROUP BY c.id, c.slug
    ORDER BY c.slug
    """
)


def compute_reason_code_distribution(
    conn: sa.Connection,
) -> tuple[ReasonCodeDistributionEntry, ...]:
    """Every check with at least one reason-coded dismissal, ever -- a check
    with none is omitted outright (nothing to report), not a zero row."""
    rows = conn.execute(_SELECT_REASON_CODE_DISTRIBUTION_SQL).all()
    return tuple(
        ReasonCodeDistributionEntry(
            check_id=str(row.check_id),
            slug=row.slug,
            genuine_issue_count=row.genuine_issue_count,
            not_genuine_count=row.not_genuine_count,
        )
        for row in rows
    )


__all__ = [
    "REASON_CODES",
    "FindingNotFoundError",
    "InvalidReasonCodeError",
    "InvalidTransitionError",
    "ReasonCodeDistributionEntry",
    "TransitionResult",
    "acknowledge",
    "compute_reason_code_distribution",
    "dismiss",
    "reopen",
    "snooze",
]
