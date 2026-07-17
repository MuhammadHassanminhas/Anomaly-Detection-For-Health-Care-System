"""check_versions_review_note

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-17 00:00:00.000000

Phase 4 step 3: the review-gate CLI (`cdss.review`) records a reviewer note
on every approve/reject/amend, distinct from `rationale` (the generator's own
machine-written evidence citation, never overwritten by a human decision).
Nullable Text, purely additive -- `reviewed_by`/`reviewed_at` already exist
from the Phase 3 step 1 baseline; this just adds the one missing field the
phase-04 spec's own text calls for ("approval writes reviewed_by/at/note").
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("check_versions", sa.Column("review_note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("check_versions", "review_note")
