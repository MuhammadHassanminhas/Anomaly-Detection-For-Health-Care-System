"""narratives_composition_fields

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-20 00:00:00.000000

Phase 5 step 4: the Tier S narration pipeline. `narratives` (created empty
of these fields in 0001, before any narration code existed) needs three
more columns to record what `cdss.narrate.compose` actually produced:
`model_id`/`prompt_hash` (nullable -- the fallback paths, both the
validator-blocked case and the LLM-outage case, never call a model, so
there is nothing to record) and `actions` (the LLM's selected action
codes, or empty on any fallback -- a fallback narrative makes no action
claim of its own). Also widens `ck_narratives_validation_status`: 0001 only
anticipated `valid`/`blocked_fallback`; the spec's own step 4 text
distinguishes a *validator-blocked* narrative (`blocked_fallback`, the
existing value) from an *LLM-outage* one (`fallback_static`, new) -- both
render via the same deterministic fallback template, but the distinction
matters for anyone auditing whether the LLM validator or an upstream
outage produced a given fallback.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_CONSTRAINT = "validation_status IN ('valid','blocked_fallback')"
_NEW_CONSTRAINT = "validation_status IN ('valid','blocked_fallback','fallback_static')"


def upgrade() -> None:
    op.add_column("narratives", sa.Column("model_id", sa.Text(), nullable=True))
    op.add_column("narratives", sa.Column("prompt_hash", sa.Text(), nullable=True))
    op.add_column(
        "narratives",
        sa.Column("actions", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.drop_constraint("ck_narratives_validation_status", "narratives", type_="check")
    op.create_check_constraint("ck_narratives_validation_status", "narratives", _NEW_CONSTRAINT)


def downgrade() -> None:
    op.drop_constraint("ck_narratives_validation_status", "narratives", type_="check")
    op.create_check_constraint("ck_narratives_validation_status", "narratives", _OLD_CONSTRAINT)
    op.drop_column("narratives", "actions")
    op.drop_column("narratives", "prompt_hash")
    op.drop_column("narratives", "model_id")
