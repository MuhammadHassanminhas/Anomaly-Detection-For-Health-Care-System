"""Environment-variable configuration for the system-owned app database.

Value is consumed only from CDSS_APP_DB_URL (CLAUDE.md secrets policy) and is
never written to logs or error messages -- only the variable *name* is.
"""

from __future__ import annotations

from collections.abc import Mapping
from os import environ


class MissingAppDbConfigError(RuntimeError):
    """CDSS_APP_DB_URL is not set."""


def load_app_db_url(env: Mapping[str, str] | None = None) -> str:
    """Return the app-DB SQLAlchemy URL from CDSS_APP_DB_URL.

    Raises MissingAppDbConfigError (naming the variable, never a value) if
    unset or empty.
    """
    source = env if env is not None else environ
    url = source.get("CDSS_APP_DB_URL")
    if not url:
        raise MissingAppDbConfigError("Missing required environment variable: CDSS_APP_DB_URL")
    return url
