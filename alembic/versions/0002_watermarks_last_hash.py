"""watermarks_last_hash

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-17 00:00:00.000000

Phase 3 step 4 follow-up: adds a nullable `last_hash` column to `watermarks`
so the snapshot-hash-diff fallback (b) has somewhere to persist a view's
previous entity-key-set hash. `last_value` (TIMESTAMPTZ) can't hold one --
this was flagged rather than silently added when step 4 was first built, and
is now authorized. Rows using this column key `column_name` on a synthetic
sentinel value (`__entity_key_hash__`, see cdss.watermark_manager) rather
than a real source column, since the hash isn't tied to any one column --
it's a hash of the whole view's entity-key set.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("watermarks", sa.Column("last_hash", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("watermarks", "last_hash")
