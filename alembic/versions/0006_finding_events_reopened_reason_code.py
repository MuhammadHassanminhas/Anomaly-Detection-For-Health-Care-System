"""finding_events_reopened_reason_code

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-20 00:00:00.000000

Phase 6 step 1: the feedback service. Two gaps found in the existing
`finding_events` constraints while building `cdss.feedback`:

1. `ck_finding_events_event` (0001) has no `'reopened'` value -- there was
   no `reopen()` caller until now. `reopen()` transitions a dismissed or
   snoozed finding back to `status='open'`, mirroring the auto-reopen
   `materialize.py` already does for `resolved_system` on reseen.
2. `reason_code` was free-text with no value whitelist -- only a
   NOT-NULL-when-dismissed rule (0001's `ck_finding_events_reason_code_required`,
   left unchanged here). `dismiss()` needs a fixed vocabulary; product-owner
   direction: the minimal binary split matching `precision_stats`'
   own `genuine_issue_count` column -- `genuine_issue` / `not_genuine`.
3. The step's own `dismiss(finding, reason_code, actor, note?)` signature
   names an optional free-text `note` with nowhere to persist it -- 0001's
   `finding_events` has no `note` column. Adding it here rather than
   silently dropping the argument.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_EVENT_CONSTRAINT = (
    "event IN ('created','reseen','acknowledged','dismissed','resolved_system',"
    "'resolved_manual','snoozed','unsnoozed')"
)
_NEW_EVENT_CONSTRAINT = (
    "event IN ('created','reseen','acknowledged','dismissed','resolved_system',"
    "'resolved_manual','snoozed','unsnoozed','reopened')"
)
_REASON_CODE_CONSTRAINT = "reason_code IS NULL OR reason_code IN ('genuine_issue','not_genuine')"


def upgrade() -> None:
    op.drop_constraint("ck_finding_events_event", "finding_events", type_="check")
    op.create_check_constraint("ck_finding_events_event", "finding_events", _NEW_EVENT_CONSTRAINT)
    op.create_check_constraint("ck_finding_events_reason_code", "finding_events", _REASON_CODE_CONSTRAINT)
    op.add_column("finding_events", sa.Column("note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("finding_events", "note")
    op.drop_constraint("ck_finding_events_reason_code", "finding_events", type_="check")
    op.drop_constraint("ck_finding_events_event", "finding_events", type_="check")
    op.create_check_constraint("ck_finding_events_event", "finding_events", _OLD_EVENT_CONSTRAINT)
