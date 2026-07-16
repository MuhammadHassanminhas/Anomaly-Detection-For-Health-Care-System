"""Deliverable 1 of Phase 3 step 1: alembic upgrade head from an empty DB,
and a downgrade/upgrade round-trip. Each test forces its own known starting
state (rather than relying on execution order) and always ends at head, so
sibling tests in tests/migrations that assume a migrated schema still work
regardless of run order.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

_EXPECTED_TABLES = {
    "checks",
    "check_versions",
    "action_library",
    "check_actions",
    "practices",
    "practice_check_config",
    "calibration_runs",
    "precision_stats",
    "catalog_versions",
    "runs",
    "watermarks",
    "check_executions",
    "findings",
    "finding_events",
    "narratives",
    "discovery_signals",
    "discovery_candidates",
    "source_audit_log",
    "schema_drift_events",
}


def _table_names(engine: Engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def test_upgrade_head_from_empty_db(alembic_cfg: Config, pg_engine: Engine) -> None:
    command.downgrade(alembic_cfg, "base")
    assert _table_names(pg_engine).isdisjoint(_EXPECTED_TABLES)

    command.upgrade(alembic_cfg, "head")
    assert _table_names(pg_engine) >= _EXPECTED_TABLES


def test_downgrade_upgrade_roundtrip(alembic_cfg: Config, pg_engine: Engine) -> None:
    command.upgrade(alembic_cfg, "head")
    assert _table_names(pg_engine) >= _EXPECTED_TABLES

    command.downgrade(alembic_cfg, "base")
    assert _table_names(pg_engine).isdisjoint(_EXPECTED_TABLES)

    command.upgrade(alembic_cfg, "head")
    assert _table_names(pg_engine) >= _EXPECTED_TABLES
