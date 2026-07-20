"""practice_check_config_demotion_fields

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-20 00:00:00.000000

Phase 6 step 3: auto-demotion (F5). `practice_check_config.demoted` (0001)
is a bare bool with no record of *when* or *why* -- the phase spec's own
step 3 text asks for "demoted_at, reason snapshot: the stats that triggered
it". `demoted_at` (nullable -- most rows are never demoted) and
`demotion_reason` (JSONB, nullable -- a frozen copy of the `precision_stats`
row that crossed the floor: window_size/genuine_issue_count/
total_feedback_count/precision/computed_at, same "snapshot the evidence"
precedent as `calibration_runs.params_before`/`params_after`).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "practice_check_config", sa.Column("demoted_at", sa.TIMESTAMP(timezone=True), nullable=True)
    )
    op.add_column("practice_check_config", sa.Column("demotion_reason", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("practice_check_config", "demotion_reason")
    op.drop_column("practice_check_config", "demoted_at")
