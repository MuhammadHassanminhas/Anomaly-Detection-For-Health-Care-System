"""check_versions_fallback_template

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-17 00:00:00.000000

Phase 5 step 1: the deterministic narration floor (F8). `fallback_template`
is mandatory -- every check_version must have one so narration can never
block a finding, even before any LLM code exists. The column carries a
server-side default (a generic, honest placeholder, not fabricated
per-check content) so: (a) `ALTER TABLE ... ADD COLUMN` backfills every
existing check_version (Phase 2-4 checks) in the same statement, and (b)
the three existing `INSERT INTO check_versions` call sites
(`cdss.authoring.derive`, `cdss.authoring.llm_draft`, `cdss.review.amend_check`)
-- none of which name this column -- keep working unmodified. Per-check
human-authored text replaces the placeholder at this phase's review
touchpoint; that is a review action, not a schema concern.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_FALLBACK_TEMPLATE = "This check has flagged a record for manual review."


def upgrade() -> None:
    op.add_column(
        "check_versions",
        sa.Column(
            "fallback_template",
            sa.Text(),
            nullable=False,
            server_default=_DEFAULT_FALLBACK_TEMPLATE,
        ),
    )


def downgrade() -> None:
    op.drop_column("check_versions", "fallback_template")
