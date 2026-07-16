"""Phase 2 step 5: fixture-DB connection plumbing. Tests in this directory
require `scripts/fixture_db.ps1 -Recreate` to have been run first -- they
skip (never fail) when the instance isn't reachable, so the main
`scripts/check.ps1` gate stays green without it (D-009.1: a fixture SQL
Server is required for Phase 2 exit, not for starting the work).

Connects to SQL Server Express LocalDB (`(localdb)\\MSSQLLocalDB`), not
Docker: D-009 amendment, 2026-07-16 -- this dev machine has no
virtualization access, so the spec's Docker option is unusable here; LocalDB
is the spec's own named fallback. Windows-integrated auth, no password.
"""

from __future__ import annotations

from collections.abc import Iterator

import pyodbc
import pytest

_CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=(localdb)\\MSSQLLocalDB;DATABASE=cdss_fixture;"
    "Trusted_Connection=yes;"
)


@pytest.fixture(scope="session")
def fixture_conn() -> Iterator[pyodbc.Connection]:
    try:
        conn = pyodbc.connect(_CONN_STR, timeout=3, autocommit=True)
    except pyodbc.Error as exc:
        pytest.skip(
            f"fixture SQL Server (LocalDB) not reachable ({exc}); "
            "run scripts/fixture_db.ps1 -Recreate first"
        )
    yield conn
    conn.close()
