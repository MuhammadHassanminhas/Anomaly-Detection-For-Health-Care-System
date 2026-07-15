import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cdss.reconcile import ReconciliationEntry, ReconciliationResult
from cdss.rowstats import COLUMNS_QUERY
from cdss.source import AuditedSourceConnection
from cdss.surface import SELECTABLE_QUERY, TABLES_QUERY, SurfaceObject
from cdss.verify_env import (
    VersionInfo,
    capture_version_info,
    determine_in_scope_objects,
    run_verification,
)


class ScriptedCursor:
    def __init__(self, responses: dict[str, list[tuple[Any, ...]]]) -> None:
        self._responses = responses
        self._last_statement = ""

    def execute(self, statement: str, _params: Any = None) -> None:
        self._last_statement = statement

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._responses[self._last_statement]


class ScriptedConnection:
    def __init__(self, responses: dict[str, list[tuple[Any, ...]]]) -> None:
        self._cursor = ScriptedCursor(responses)
        self.timeout = 0

    def cursor(self) -> ScriptedCursor:
        return self._cursor


RESPONSES: dict[str, list[tuple[Any, ...]]] = {
    "SELECT @@VERSION": [("Microsoft SQL Server 2019 (RTM-CU21)",)],
    "SELECT CAST(SERVERPROPERTY('ProductVersion') AS NVARCHAR(128))": [("15.0.2000.5",)],
    "SELECT CAST(SERVERPROPERTY('Edition') AS NVARCHAR(128))": [("Standard Edition",)],
    "SELECT CAST(SERVERPROPERTY('EngineEdition') AS NVARCHAR(128))": [("2",)],
    "SELECT DB_NAME()": [("INDICI_BI_Full",)],
}


def test_capture_version_info(tmp_path: Path) -> None:
    audited = AuditedSourceConnection(
        ScriptedConnection(RESPONSES),  # type: ignore[arg-type]
        component="test",
        audit_dir=tmp_path,
        clock=lambda: datetime(2026, 7, 14, tzinfo=UTC),
    )
    info = capture_version_info(audited)
    assert info == VersionInfo(
        version_string="Microsoft SQL Server 2019 (RTM-CU21)",
        product_version="15.0.2000.5",
        edition="Standard Edition",
        engine_edition="2",
        database_name="INDICI_BI_Full",
    )


def test_capture_version_info_audits_every_statement(tmp_path: Path) -> None:
    audited = AuditedSourceConnection(
        ScriptedConnection(RESPONSES),  # type: ignore[arg-type]
        component="test",
        audit_dir=tmp_path,
        clock=lambda: datetime(2026, 7, 14, tzinfo=UTC),
    )
    capture_version_info(audited)
    audit_file = tmp_path / "source-audit-20260714.jsonl"
    lines = audit_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5


def test_determine_in_scope_objects_filters_to_matched_entries() -> None:
    surface = [
        SurfaceObject(schema="dbo", name="Patient", object_type="table", can_select=True),
        SurfaceObject(schema="dbo", name="Extra", object_type="table", can_select=True),
    ]
    reconciliation = ReconciliationResult(
        entries=[
            ReconciliationEntry(
                export_name="dbo.Patient", status="found_as_table", matched_object="dbo.Patient"
            ),
            ReconciliationEntry(export_name="dbo.Ghost", status="missing", matched_object=None),
        ],
        extra_objects=["dbo.Extra"],
    )
    in_scope = determine_in_scope_objects(reconciliation, surface)
    assert [obj.qualified_name for obj in in_scope] == ["dbo.Patient"]


def test_run_verification_orchestrates_steps_4_through_7(tmp_path: Path) -> None:
    export_path = tmp_path / "export.json"
    export_path.write_text(
        json.dumps(
            [{"table": "dbo.Patient", "columns": []}, {"table": "dbo.Ghost", "columns": []}]
        ),
        encoding="utf-8",
    )

    responses: dict[str, list[tuple[Any, ...]]] = {
        **RESPONSES,
        TABLES_QUERY: [("dbo", "Patient", "BASE TABLE"), ("dbo", "Extra", "VIEW")],
        SELECTABLE_QUERY: [("dbo", "Patient"), ("dbo", "Extra")],
        COLUMNS_QUERY: [("dbo", "Patient", "InsertedAt", "datetime")],
        "SELECT COUNT(*) FROM dbo.Patient": [(42,)],
        "SELECT MIN([InsertedAt]), MAX([InsertedAt]) FROM dbo.Patient": [
            ("2020-01-01", "2026-01-01")
        ],
    }

    audited = AuditedSourceConnection(
        ScriptedConnection(responses),  # type: ignore[arg-type]
        component="test",
        audit_dir=tmp_path,
        clock=lambda: datetime(2026, 7, 15, tzinfo=UTC),
    )

    report = run_verification(audited, export_path, clock=lambda: datetime(2026, 7, 15, tzinfo=UTC))

    assert report.generated_at == datetime(2026, 7, 15, tzinfo=UTC).isoformat()
    assert report.version.database_name == "INDICI_BI_Full"
    assert {obj.qualified_name for obj in report.surface} == {"dbo.Patient", "dbo.Extra"}
    assert [entry.status for entry in report.reconciliation.entries] == [
        "found_as_table",
        "missing",
    ]
    # dbo.Extra is on the surface but not in the export list — it must not be
    # row-counted (step 7 is scoped to reconciled export names only).
    assert [stats.qualified_name for stats in report.row_stats] == ["dbo.Patient"]
    assert report.row_stats[0].row_count == 42
    assert report.row_stats[0].watermark_columns[0].column_name == "InsertedAt"
