"""Phase 3 step 1: app-DB migration + constraint tests.

Require CDSS_APP_DB_URL to point at a reachable, disposable PostgreSQL
database and skip (never fail) otherwise -- same "required for exit, not
start" pattern as tests/execution (D-009.1) so the main scripts/check.ps1
gate stays green without a local Postgres.

The session-scoped `migrated_db` fixture applies migration 0001 once (via
the real alembic upgrade/downgrade commands, not hand-written DDL) and tears
the schema back down to base when the whole test session ends, leaving the
target database empty again.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from cdss.app_db import MissingAppDbConfigError, load_app_db_url

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def alembic_cfg() -> Config:
    return Config(str(_REPO_ROOT / "alembic.ini"))


@pytest.fixture(scope="session")
def app_db_url() -> str:
    try:
        url = load_app_db_url()
    except MissingAppDbConfigError:
        pytest.skip("CDSS_APP_DB_URL not set; run against a local Postgres to exercise this suite")
    engine = sa.create_engine(url, connect_args={"connect_timeout": 3})
    try:
        with engine.connect():
            pass
    except sa.exc.OperationalError as exc:
        pytest.skip(f"app DB (CDSS_APP_DB_URL) not reachable ({exc})")
    finally:
        engine.dispose()
    return url


@pytest.fixture(scope="session")
def pg_engine(app_db_url: str) -> Iterator[Engine]:
    engine = sa.create_engine(app_db_url)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def migrated_db(alembic_cfg: Config, pg_engine: Engine) -> Iterator[Engine]:
    command.upgrade(alembic_cfg, "head")
    yield pg_engine
    command.downgrade(alembic_cfg, "base")


@pytest.fixture
def conn(migrated_db: Engine) -> Iterator[sa.Connection]:
    with migrated_db.connect() as connection:
        trans = connection.begin()
        try:
            yield connection
        finally:
            trans.rollback()
