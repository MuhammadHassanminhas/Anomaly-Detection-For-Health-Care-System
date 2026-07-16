"""initial_schema

Revision ID: 0001
Revises:
Create Date: 2026-07-16 16:55:14.283097

Full ARCHITECTURE.md Section 2.5 app-DB schema (Phase 3 step 1): check
library, per-practice config, execution & findings, discovery & governance.
Constraints as code, not app-layer: findings.check_version_id NOT NULL FK
(F2), UNIQUE(check_id, dedupe_key), finding_events append-only via trigger,
reason_code NOT NULL when event='dismissed' (CHECK).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    # ---- Check library ----------------------------------------------
    op.create_table(
        "checks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("slug", sa.Text(), nullable=False, unique=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("default_severity", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "category IN ('referential','data-quality','workflow','care-gap','revenue-integrity','policy')",
            name="ck_checks_category",
        ),
        sa.CheckConstraint(
            "default_severity IN ('low','medium','high','critical')", name="ck_checks_default_severity"
        ),
        sa.CheckConstraint("source IN ('profiling','llm','discovery','manual')", name="ck_checks_source"),
        sa.CheckConstraint(
            "status IN ('draft','in_review','active','rejected','retired')", name="ck_checks_status"
        ),
    )

    op.create_table(
        "check_versions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("check_id", UUID(as_uuid=True), sa.ForeignKey("checks.id"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("definition", JSONB(), nullable=False),
        sa.Column("definition_hash", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("affected_views", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("params_schema", JSONB(), nullable=False),
        sa.Column("reviewed_by", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("check_id", "version_number", name="uq_check_versions_check_version"),
    )

    op.create_table(
        "action_library",
        sa.Column("code", sa.Text(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "check_actions",
        sa.Column("check_id", UUID(as_uuid=True), sa.ForeignKey("checks.id"), primary_key=True),
        sa.Column(
            "action_code", sa.Text(), sa.ForeignKey("action_library.code"), primary_key=True
        ),
    )

    # ---- Per-practice config & learning -------------------------------
    op.create_table(
        "practices",
        sa.Column("practice_id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "practice_check_config",
        sa.Column("practice_id", sa.Text(), sa.ForeignKey("practices.practice_id"), primary_key=True),
        sa.Column("check_id", UUID(as_uuid=True), sa.ForeignKey("checks.id"), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("demoted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("params", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("params_source", sa.Text(), nullable=False, server_default="default"),
        sa.CheckConstraint(
            "params_source IN ('default','calibrated','manual')", name="ck_practice_check_config_params_source"
        ),
    )

    op.create_table(
        "calibration_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("practice_id", sa.Text(), sa.ForeignKey("practices.practice_id"), nullable=False),
        sa.Column("check_id", UUID(as_uuid=True), sa.ForeignKey("checks.id"), nullable=False),
        sa.Column("run_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("params_before", JSONB(), nullable=True),
        sa.Column("params_after", JSONB(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
    )

    op.create_table(
        "precision_stats",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("practice_id", sa.Text(), sa.ForeignKey("practices.practice_id"), nullable=False),
        sa.Column("check_id", UUID(as_uuid=True), sa.ForeignKey("checks.id"), nullable=False),
        sa.Column("computed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("window_size", sa.Integer(), nullable=False),
        sa.Column("genuine_issue_count", sa.Integer(), nullable=False),
        sa.Column("total_feedback_count", sa.Integer(), nullable=False),
        sa.Column("precision", sa.Numeric(), nullable=False),
    )

    # ---- Execution & findings -----------------------------------------
    op.create_table(
        "catalog_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("catalog_version_id", sa.Integer(), sa.ForeignKey("catalog_versions.id"), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="running"),
        sa.Column("triggered_by", sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('running','completed','failed')", name="ck_runs_status"),
    )

    op.create_table(
        "watermarks",
        sa.Column("view_name", sa.Text(), primary_key=True),
        sa.Column("column_name", sa.Text(), primary_key=True),
        sa.Column("last_value", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "check_executions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("check_id", UUID(as_uuid=True), sa.ForeignKey("checks.id"), nullable=False),
        sa.Column("check_version_id", UUID(as_uuid=True), sa.ForeignKey("check_versions.id"), nullable=False),
        sa.Column("practice_id", sa.Text(), sa.ForeignKey("practices.practice_id"), nullable=False),
        sa.Column("sql_hash", sa.Text(), nullable=False),
        sa.Column("watermark_from", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("watermark_to", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("rows_examined", sa.Integer(), nullable=False),
        sa.Column("n_pass", sa.Integer(), nullable=False),
        sa.Column("n_fail", sa.Integer(), nullable=False),
        sa.Column("n_indeterminate", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="ok"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('ok','skipped_drift','error')", name="ck_check_executions_status"),
    )

    op.create_table(
        "findings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("check_id", UUID(as_uuid=True), sa.ForeignKey("checks.id"), nullable=False),
        sa.Column(
            "check_version_id", UUID(as_uuid=True), sa.ForeignKey("check_versions.id"), nullable=False
        ),
        sa.Column("practice_id", sa.Text(), sa.ForeignKey("practices.practice_id"), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("entity_key", JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("evidence", JSONB(), nullable=False),
        sa.Column("first_seen_run_id", UUID(as_uuid=True), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("last_seen_run_id", UUID(as_uuid=True), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("snoozed_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("check_id", "dedupe_key", name="uq_findings_check_dedupe_key"),
        sa.CheckConstraint(
            "status IN ('open','acknowledged','dismissed','resolved','resolved_system')",
            name="ck_findings_status",
        ),
        sa.CheckConstraint(
            "severity IN ('low','medium','high','critical')", name="ck_findings_severity"
        ),
    )

    op.create_table(
        "finding_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("finding_id", UUID(as_uuid=True), sa.ForeignKey("findings.id"), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("actor", sa.Text(), nullable=True),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("runs.id"), nullable=True),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "event IN ('created','reseen','acknowledged','dismissed','resolved_system',"
            "'resolved_manual','snoozed','unsnoozed')",
            name="ck_finding_events_event",
        ),
        sa.CheckConstraint(
            "event <> 'dismissed' OR reason_code IS NOT NULL", name="ck_finding_events_reason_code_required"
        ),
    )

    op.create_table(
        "narratives",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("finding_id", UUID(as_uuid=True), sa.ForeignKey("findings.id"), nullable=False),
        sa.Column("template_text", sa.Text(), nullable=False),
        sa.Column("rendered_text", sa.Text(), nullable=False),
        sa.Column("validation_status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "validation_status IN ('valid','blocked_fallback')", name="ck_narratives_validation_status"
        ),
    )

    # ---- Discovery & governance -----------------------------------------
    op.create_table(
        "discovery_signals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("practice_id", sa.Text(), sa.ForeignKey("practices.practice_id"), nullable=False),
        sa.Column("signal_type", sa.Text(), nullable=False),
        sa.Column("computed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("payload", JSONB(), nullable=False),
    )

    op.create_table(
        "discovery_candidates",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "discovery_signal_id", UUID(as_uuid=True), sa.ForeignKey("discovery_signals.id"), nullable=True
        ),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("draft_check_id", UUID(as_uuid=True), sa.ForeignKey("checks.id"), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status IN ('pending','promoted','rejected')", name="ck_discovery_candidates_status"
        ),
    )

    op.create_table(
        "source_audit_log",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("runs.id"), nullable=True),
        sa.Column("component", sa.Text(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("params", JSONB(), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "schema_drift_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("view_name", sa.Text(), nullable=False),
        sa.Column("catalog_version_id", sa.Integer(), sa.ForeignKey("catalog_versions.id"), nullable=False),
        sa.Column("detail", JSONB(), nullable=False),
        sa.Column("detected_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    # ---- finding_events append-only guard (F7): no UPDATE/DELETE, ever ----
    op.execute(
        """
        CREATE FUNCTION finding_events_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'finding_events is append-only: % not permitted', TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER finding_events_no_update
        BEFORE UPDATE ON finding_events
        FOR EACH ROW EXECUTE FUNCTION finding_events_append_only();
        """
    )
    op.execute(
        """
        CREATE TRIGGER finding_events_no_delete
        BEFORE DELETE ON finding_events
        FOR EACH ROW EXECUTE FUNCTION finding_events_append_only();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS finding_events_no_delete ON finding_events")
    op.execute("DROP TRIGGER IF EXISTS finding_events_no_update ON finding_events")
    op.execute("DROP FUNCTION IF EXISTS finding_events_append_only()")

    for table in (
        "schema_drift_events",
        "source_audit_log",
        "discovery_candidates",
        "discovery_signals",
        "narratives",
        "finding_events",
        "findings",
        "check_executions",
        "watermarks",
        "runs",
        "catalog_versions",
        "precision_stats",
        "calibration_runs",
        "practice_check_config",
        "practices",
        "check_actions",
        "action_library",
        "check_versions",
        "checks",
    ):
        op.drop_table(table)
