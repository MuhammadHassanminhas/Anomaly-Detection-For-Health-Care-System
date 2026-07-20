"""Phase 3 step 6: finding materialization. Upserts `findings` (append-only
`finding_events` alongside) from a single `CheckExecutionResult`'s classified
rows -- the step between "executor produced tri-state rows" (step 5) and
"staff sees a queue" (Phase 9/10).

Transitions implemented, matching ARCHITECTURE.md Section 1's step 4 and the
phase spec's step 6 text literally:

- `fail`, no existing finding for `(check_id, dedupe_key)` -> insert `open`,
  `created` event.
- `fail`, existing finding not touched by this run yet -> bump
  `last_seen_run_id`/evidence, `reseen` event. A `resolved_system` finding
  recurring is reopened to `open` (the auto-close was provisional, made by
  the system, not a human) -- a `resolved`/`dismissed` finding recurring
  keeps its human-asserted status untouched but still gets the `reseen`
  event, so staff see the recurrence without the system silently overriding
  their judgement.
- `pass`, existing *active* (`open`/`acknowledged`) finding, check opts into
  auto-resolve (`auto_resolve=True`, a caller-supplied flag -- no DSL field
  for this exists yet, same "caller decides, this module doesn't look it up"
  precedent as `watermark_manager`'s strategy argument) -> `resolved_system`
  status + event.
- `indeterminate` -> no finding action; that's F6/step 7's job (indeterminacy
  surfacing), not materialization's.
- `snoozed_until` is never read or written here -- ARCHITECTURE.md describes
  snooze as a queue-visibility filter orthogonal to status, set by a Phase 9
  lifecycle action, not by materialization.

**Idempotency**: a finding already advanced to `last_seen_run_id == run_id`
is skipped outright on a repeat pass over the same row -- this is what makes
calling this function twice with the same `run_id` and the same result set
a true no-op the second time (the step's own named deliverable), independent
of whatever run-level idempotency watermark advancement provides.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any

import sqlalchemy as sa

from cdss.executor import CheckExecutionResult

_ACTIVE_STATUSES = frozenset({"open", "acknowledged"})


def canonical_entity_key(
    entity_key_columns: tuple[str, ...], values: tuple[Any, ...]
) -> dict[str, Any]:
    """Pair entity-key column names with a row's positional key values --
    the named shape `findings.entity_key` (JSONB) and the dedupe hash both
    need; `ExecutedRow.entity_key` itself is a bare positional tuple."""
    return dict(zip(entity_key_columns, values, strict=True))


def compute_dedupe_key(check_id: str, entity_key: dict[str, Any]) -> str:
    """sha256 of `check_id` + a canonical (sorted-key, typed-rendered via
    `default=str` -- same rendering the audit sink already uses for
    non-JSON-native values like `datetime`) JSON encoding of `entity_key`.
    Frozen by a golden test (`test_dedupe_key_is_stable`) per the phase
    spec's own risk note: changing this later re-flags every open finding."""
    canonical = json.dumps(entity_key, sort_keys=True, default=str)
    digest = hashlib.sha256(f"{check_id}:{canonical}".encode()).hexdigest()
    return digest


@dataclass(frozen=True)
class CreatedFinding:
    """One `fail` row that just became a brand-new `findings` row --
    Phase 5 step 7's own hook: only a genuinely new finding gets narrated
    inline (`cdss.run`), never a `reseen`/`reopened` recurrence of one
    already narrated on an earlier run."""

    finding_id: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class MaterializationStats:
    created: int = 0
    reseen: int = 0
    reopened: int = 0
    resolved_system: int = 0
    skipped_idempotent: int = 0
    created_findings: tuple[CreatedFinding, ...] = ()


_SELECT_FINDING_SQL = sa.text(
    "SELECT id, status, last_seen_run_id FROM findings "
    "WHERE check_id = :check_id AND dedupe_key = :dedupe_key"
)

_INSERT_FINDING_SQL = sa.text(
    """
    INSERT INTO findings
        (check_id, check_version_id, practice_id, dedupe_key, entity_key,
         status, severity, evidence, first_seen_run_id, last_seen_run_id)
    VALUES
        (:check_id, :check_version_id, :practice_id, :dedupe_key, CAST(:entity_key AS jsonb),
         'open', :severity, CAST(:evidence AS jsonb), :run_id, :run_id)
    RETURNING id
    """
)

_UPDATE_FINDING_RESEEN_SQL = sa.text(
    """
    UPDATE findings
    SET last_seen_run_id = :run_id, evidence = CAST(:evidence AS jsonb),
        status = :status, updated_at = now()
    WHERE id = :finding_id
    """
)

_UPDATE_FINDING_RESOLVED_SYSTEM_SQL = sa.text(
    """
    UPDATE findings SET status = 'resolved_system', last_seen_run_id = :run_id, updated_at = now()
    WHERE id = :finding_id
    """
)

_INSERT_EVENT_SQL = sa.text(
    "INSERT INTO finding_events (finding_id, event, run_id) VALUES (:finding_id, :event, :run_id)"
)


def materialize_check_result(
    conn: sa.Connection,
    run_id: str,
    result: CheckExecutionResult,
    *,
    entity_key_columns: tuple[str, ...],
    severity: str,
    auto_resolve: bool,
) -> MaterializationStats:
    """Upsert every `fail`/`pass` row in `result.rows` into `findings` +
    `finding_events`. `entity_key_columns` and `severity` come from the
    check's parsed `CheckDoc` (`doc.entity.key`, `doc.default_severity`) --
    kept as plain arguments rather than importing `cdss.dsl` here, so this
    module stays decoupled from the DSL model, matching `execute_check`'s
    own `project_columns`/`doc` split."""
    stats = MaterializationStats()
    for row in result.rows:
        if row.tri_state not in ("fail", "pass"):
            continue

        entity_key = canonical_entity_key(entity_key_columns, row.entity_key)
        dedupe_key = compute_dedupe_key(result.check_id, entity_key)
        existing = conn.execute(
            _SELECT_FINDING_SQL, {"check_id": result.check_id, "dedupe_key": dedupe_key}
        ).one_or_none()

        if row.tri_state == "fail":
            if existing is None:
                finding_id = (
                    conn.execute(
                        _INSERT_FINDING_SQL,
                        {
                            "check_id": result.check_id,
                            "check_version_id": result.check_version_id,
                            "practice_id": result.practice_id,
                            "dedupe_key": dedupe_key,
                            "entity_key": json.dumps(entity_key, default=str),
                            "severity": severity,
                            "evidence": json.dumps(row.evidence, default=str),
                            "run_id": run_id,
                        },
                    )
                    .one()
                    .id
                )
                conn.execute(
                    _INSERT_EVENT_SQL,
                    {"finding_id": finding_id, "event": "created", "run_id": run_id},
                )
                stats = replace(
                    stats,
                    created=stats.created + 1,
                    created_findings=stats.created_findings
                    + (CreatedFinding(finding_id=str(finding_id), evidence=row.evidence),),
                )
                continue

            if str(existing.last_seen_run_id) == str(run_id):
                stats = replace(stats, skipped_idempotent=stats.skipped_idempotent + 1)
                continue

            reopening = existing.status == "resolved_system"
            new_status = "open" if reopening else existing.status
            conn.execute(
                _UPDATE_FINDING_RESEEN_SQL,
                {
                    "finding_id": existing.id,
                    "run_id": run_id,
                    "evidence": json.dumps(row.evidence, default=str),
                    "status": new_status,
                },
            )
            conn.execute(
                _INSERT_EVENT_SQL, {"finding_id": existing.id, "event": "reseen", "run_id": run_id}
            )
            if reopening:
                stats = replace(stats, reopened=stats.reopened + 1)
            else:
                stats = replace(stats, reseen=stats.reseen + 1)

        else:  # row.tri_state == "pass"
            if existing is None or not auto_resolve or existing.status not in _ACTIVE_STATUSES:
                continue
            if str(existing.last_seen_run_id) == str(run_id):
                stats = replace(stats, skipped_idempotent=stats.skipped_idempotent + 1)
                continue
            conn.execute(
                _UPDATE_FINDING_RESOLVED_SYSTEM_SQL, {"finding_id": existing.id, "run_id": run_id}
            )
            conn.execute(
                _INSERT_EVENT_SQL,
                {"finding_id": existing.id, "event": "resolved_system", "run_id": run_id},
            )
            stats = replace(stats, resolved_system=stats.resolved_system + 1)

    return stats


__all__ = [
    "CreatedFinding",
    "MaterializationStats",
    "canonical_entity_key",
    "compute_dedupe_key",
    "materialize_check_result",
]
