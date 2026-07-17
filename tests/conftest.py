"""Shared external-DB test fixtures: require the relevant DB to be reachable
and skip (never fail) otherwise -- D-009.1's "required for exit, not start"
pattern -- so the main scripts/check.ps1 gate stays green without either DB
running.

App-DB (PostgreSQL, CDSS_APP_DB_URL) fixtures promoted from
tests/migrations/conftest.py (Phase 3 step 1) to tests/conftest.py (Phase 3
step 2). Source-fixture (SQL Server LocalDB) fixture promoted from
tests/execution/conftest.py (Phase 2 step 5) to here (Phase 3 step 5) -- one
place for every DB-gated suite (migrations, app-DB repositories, check
registry, watermark manager, executor, ...) to share instead of duplicating
skip/rollback plumbing per directory.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pyodbc
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from cdss.app_db import MissingAppDbConfigError, load_app_db_url

_REPO_ROOT = Path(__file__).resolve().parents[1]


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


# --- source fixture DB (SQL Server LocalDB, D-026) --------------------------

_FIXTURE_CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=(localdb)\\MSSQLLocalDB;DATABASE=cdss_fixture;"
    "Trusted_Connection=yes;"
)


@pytest.fixture(scope="session")
def fixture_conn() -> Iterator[pyodbc.Connection]:
    try:
        connection = pyodbc.connect(_FIXTURE_CONN_STR, timeout=3, autocommit=True)
    except pyodbc.Error as exc:
        pytest.skip(
            f"fixture SQL Server (LocalDB) not reachable ({exc}); "
            "run scripts/fixture_db.ps1 -Recreate first"
        )
    yield connection
    connection.close()
