"""Environment-variable configuration for the source (INDICI_BI_Full) connection.

Values are consumed only from CDSS_SOURCE_* environment variables (D-002) and
are never written to logs or error messages — only variable *names* are.

Auth is either Windows/Integrated (D-002 default: no SQL login, the process's
Windows identity carries the read-only grant — CDSS_SOURCE_USER/PASSWORD are
not required) or SQL login (CDSS_SOURCE_USER/PASSWORD required).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from os import environ
from typing import Literal

ALWAYS_REQUIRED_VARS: tuple[str, ...] = (
    "CDSS_SOURCE_HOST",
    "CDSS_SOURCE_PORT",
    "CDSS_SOURCE_DB",
    "CDSS_SOURCE_AUTH",
    "CDSS_SOURCE_ENCRYPT",
    "CDSS_SOURCE_TRUST_SERVER_CERTIFICATE",
)
SQL_AUTH_REQUIRED_VARS: tuple[str, ...] = (
    "CDSS_SOURCE_USER",
    "CDSS_SOURCE_PASSWORD",
)

_TRUE_VALUES = frozenset({"true", "1", "yes"})
_FALSE_VALUES = frozenset({"false", "0", "no"})
_AUTH_MODES = frozenset({"windows", "sql"})

AuthMode = Literal["windows", "sql"]


class MissingSourceConfigError(RuntimeError):
    """One or more required CDSS_SOURCE_* environment variables are not set."""


@dataclass(frozen=True)
class SourceConfig:
    host: str
    port: int
    database: str
    auth: AuthMode
    user: str | None
    password: str | None
    encrypt: bool
    trust_server_certificate: bool

    def __repr__(self) -> str:
        return (
            f"SourceConfig(host={self.host!r}, port={self.port!r}, "
            f"database={self.database!r}, auth={self.auth!r}, "
            f"user={self.user!r}, password={'***REDACTED***' if self.password else None!r}, "
            f"encrypt={self.encrypt!r}, "
            f"trust_server_certificate={self.trust_server_certificate!r})"
        )

    __str__ = __repr__


def _parse_port(raw: str) -> int:
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"CDSS_SOURCE_PORT must be an integer, got {raw!r}") from exc


def _parse_bool(name: str, raw: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    allowed = sorted(_TRUE_VALUES | _FALSE_VALUES)
    raise ValueError(f"{name} must be one of {allowed}, got {raw!r}")


def _parse_auth(raw: str) -> AuthMode:
    lowered = raw.strip().lower()
    if lowered not in _AUTH_MODES:
        raise ValueError(f"CDSS_SOURCE_AUTH must be one of {sorted(_AUTH_MODES)}, got {raw!r}")
    return "windows" if lowered == "windows" else "sql"


def load_source_config(env: Mapping[str, str] | None = None) -> SourceConfig:
    """Load and validate the CDSS_SOURCE_* environment variables.

    Raises MissingSourceConfigError listing every missing variable *name*
    (never a value) if any required variable is absent or empty.
    CDSS_SOURCE_USER/CDSS_SOURCE_PASSWORD are required only when
    CDSS_SOURCE_AUTH=sql.
    """
    source = env if env is not None else environ
    missing = [name for name in ALWAYS_REQUIRED_VARS if not source.get(name)]
    if missing:
        raise MissingSourceConfigError(
            "Missing required environment variable(s): " + ", ".join(missing)
        )

    auth = _parse_auth(source["CDSS_SOURCE_AUTH"])

    user: str | None = None
    password: str | None = None
    if auth == "sql":
        sql_missing = [name for name in SQL_AUTH_REQUIRED_VARS if not source.get(name)]
        if sql_missing:
            raise MissingSourceConfigError(
                "Missing required environment variable(s): " + ", ".join(sql_missing)
            )
        user = source["CDSS_SOURCE_USER"]
        password = source["CDSS_SOURCE_PASSWORD"]

    return SourceConfig(
        host=source["CDSS_SOURCE_HOST"],
        port=_parse_port(source["CDSS_SOURCE_PORT"]),
        database=source["CDSS_SOURCE_DB"],
        auth=auth,
        user=user,
        password=password,
        encrypt=_parse_bool("CDSS_SOURCE_ENCRYPT", source["CDSS_SOURCE_ENCRYPT"]),
        trust_server_certificate=_parse_bool(
            "CDSS_SOURCE_TRUST_SERVER_CERTIFICATE",
            source["CDSS_SOURCE_TRUST_SERVER_CERTIFICATE"],
        ),
    )
