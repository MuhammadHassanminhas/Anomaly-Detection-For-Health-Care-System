"""pyodbc connection factory for the source (INDICI_BI_Full) database.

Builds the ODBC connection string from a SourceConfig — never logs it, since
it carries CDSS_SOURCE_PASSWORD in cleartext when auth=sql (D-002).
"""

from __future__ import annotations

import pyodbc

from cdss.config import SourceConfig

DRIVER = "{ODBC Driver 18 for SQL Server}"


def build_connection_string(config: SourceConfig) -> str:
    parts = [
        f"DRIVER={DRIVER}",
        f"SERVER={config.host},{config.port}",
        f"DATABASE={config.database}",
        f"Encrypt={'yes' if config.encrypt else 'no'}",
        f"TrustServerCertificate={'yes' if config.trust_server_certificate else 'no'}",
    ]
    if config.auth == "windows":
        parts.append("Trusted_Connection=yes")
    else:
        parts.append(f"UID={config.user}")
        parts.append(f"PWD={config.password}")
    return ";".join(parts) + ";"


def connect(config: SourceConfig, *, timeout: int = 10) -> pyodbc.Connection:
    """Open a read-only-intent connection. Callers must route every statement
    through cdss.source.AuditedSourceConnection — this factory only opens
    the raw DB-API connection."""
    return pyodbc.connect(build_connection_string(config), timeout=timeout, autocommit=True)
