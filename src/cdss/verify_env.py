"""Environment verification orchestrator (Phase 0 steps 4-8).

Invoked via `python -m cdss.verify_env`, wrapped by scripts/verify_env.ps1.
Connects live (read-only, audited) to the source database, runs steps 4-7
(version capture, surface enumeration, D-001 reconciliation, row counts +
watermark candidates for the in-scope objects), and writes
artifacts/env-report.{json,md}.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cdss.config import load_source_config
from cdss.connection import connect
from cdss.reconcile import ReconciliationResult, load_export_names, reconcile
from cdss.report import EnvironmentReport, write_json, write_markdown
from cdss.rowstats import compute_row_stats
from cdss.source import AuditedSourceConnection
from cdss.surface import SurfaceObject, enumerate_surface

DEFAULT_EXPORT_NAMES_PATH = Path("schema_for_SQL_PROJ.txt")
DEFAULT_ROW_COUNT_TIMEOUT_SECONDS = 15
DEFAULT_JSON_REPORT_PATH = Path("artifacts/env-report.json")
DEFAULT_MARKDOWN_REPORT_PATH = Path("artifacts/env-report.md")


@dataclass(frozen=True)
class VersionInfo:
    version_string: str
    product_version: str
    edition: str
    engine_edition: str
    database_name: str


def _scalar(audited: AuditedSourceConnection, statement: str) -> str:
    (value,) = audited.execute_query(statement)[0]
    return str(value)


def capture_version_info(audited: AuditedSourceConnection) -> VersionInfo:
    """Step 4: SQL Server version/edition + confirm DB_NAME() matches the
    configured database. Every statement here has no FROM clause, so the
    SQL guard's object allowlist is not consulted."""
    return VersionInfo(
        version_string=_scalar(audited, "SELECT @@VERSION"),
        product_version=_scalar(
            audited, "SELECT CAST(SERVERPROPERTY('ProductVersion') AS NVARCHAR(128))"
        ),
        edition=_scalar(audited, "SELECT CAST(SERVERPROPERTY('Edition') AS NVARCHAR(128))"),
        engine_edition=_scalar(
            audited, "SELECT CAST(SERVERPROPERTY('EngineEdition') AS NVARCHAR(128))"
        ),
        database_name=_scalar(audited, "SELECT DB_NAME()"),
    )


def determine_in_scope_objects(
    reconciliation: ReconciliationResult, surface: list[SurfaceObject]
) -> list[SurfaceObject]:
    """Objects that reconciled to a real surface object (view or table) —
    step 7 measures only these, never the surrounding objects the account
    can see but that are outside the CDSS scope."""
    by_qualified = {obj.qualified_name.lower(): obj for obj in surface}
    in_scope = []
    for entry in reconciliation.entries:
        if entry.matched_object is None:
            continue
        obj = by_qualified.get(entry.matched_object.lower())
        if obj is not None:
            in_scope.append(obj)
    return in_scope


def run_verification(
    audited: AuditedSourceConnection,
    export_names_path: Path = DEFAULT_EXPORT_NAMES_PATH,
    *,
    row_count_timeout_seconds: int = DEFAULT_ROW_COUNT_TIMEOUT_SECONDS,
    clock: Callable[[], datetime] | None = None,
) -> EnvironmentReport:
    """Steps 4-7 end to end, returning the assembled (not yet written) report."""
    clock = clock or (lambda: datetime.now(UTC))
    version = capture_version_info(audited)
    surface = enumerate_surface(audited)
    export_names = load_export_names(export_names_path)
    reconciliation = reconcile(export_names, surface)
    in_scope = determine_in_scope_objects(reconciliation, surface)

    # Step 5 only reads catalog metadata (always allowed, D-015); reading the
    # in-scope objects themselves in step 7 requires expanding the allowlist
    # to those specific objects (defense in depth stays intact — nothing
    # outside the reconciled export names is ever unlocked).
    scoped_audited = audited.with_allowed_objects(
        frozenset(obj.qualified_name.lower() for obj in in_scope)
    )
    row_stats = compute_row_stats(
        scoped_audited, in_scope, timeout_seconds=row_count_timeout_seconds
    )
    return EnvironmentReport(
        generated_at=clock().isoformat(),
        version=version,
        surface=surface,
        reconciliation=reconciliation,
        row_stats=row_stats,
    )


def main() -> int:
    config = load_source_config()
    connection = connect(config)
    audited = AuditedSourceConnection(connection, component="verify_env")
    try:
        report = run_verification(audited)
    finally:
        connection.close()

    if report.version.database_name != config.database:
        print(
            "ERROR: DB_NAME() mismatch: expected "
            f"{config.database!r}, got {report.version.database_name!r}"
        )
        return 1

    write_json(report, DEFAULT_JSON_REPORT_PATH)
    write_markdown(report, DEFAULT_MARKDOWN_REPORT_PATH)

    print(
        f"Connected: {report.version.database_name} — "
        f"{report.version.product_version} ({report.version.edition})"
    )
    print(
        f"Surface: {len(report.surface)} objects; "
        f"{len(report.reconciliation.entries)} export names reconciled"
    )
    print(f"Row stats computed for {len(report.row_stats)} in-scope objects")
    print(f"Wrote {DEFAULT_JSON_REPORT_PATH} and {DEFAULT_MARKDOWN_REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
