"""SQLAlchemy Core repository over the app database (Phase 3 step 2).

SourceAuditLogRepository is the app-DB half of the dual audit write (D-016):
cdss.source.AuditedSourceConnection writes the primary, append-only JSONL
line for every accepted statement, then -- when given an app_db_sink -- also
calls .record() here to mirror the same event into source_audit_log. JSONL
stays authoritative; this mirror exists so findings/executions can later be
joined against their originating source statements in SQL.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from cdss.source import AuditEvent

metadata = sa.MetaData()

# Column shape mirrors alembic/versions/0001_initial_schema.py's
# source_audit_log table exactly; id/created_at rely on that migration's
# server-side defaults rather than repeating them here.
source_audit_log = sa.Table(
    "source_audit_log",
    metadata,
    sa.Column(
        "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
    ),
    sa.Column("run_id", UUID(as_uuid=True), nullable=True),
    sa.Column("component", sa.Text(), nullable=False),
    sa.Column("statement", sa.Text(), nullable=False),
    sa.Column("params", JSONB(), nullable=True),
    sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("duration_ms", sa.Integer(), nullable=False),
    sa.Column("row_count", sa.Integer(), nullable=True),
    sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=True),
)


class SourceAuditLogRepository:
    """Inserts one source_audit_log row per AuditEvent."""

    def __init__(self, engine: sa.engine.Engine) -> None:
        self._engine = engine

    def record(self, event: AuditEvent) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                source_audit_log.insert().values(
                    run_id=uuid.UUID(event.run_id) if event.run_id is not None else None,
                    component=event.component,
                    statement=event.statement,
                    params=list(event.params),
                    started_at=datetime.fromisoformat(event.timestamp),
                    duration_ms=round(event.duration_ms),
                    row_count=event.rows_returned,
                )
            )


__all__ = ["SourceAuditLogRepository", "source_audit_log"]
