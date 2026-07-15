import json
from pathlib import Path

import jsonschema
import pytest

from cdss.reconcile import ReconciliationEntry, ReconciliationResult
from cdss.report import (
    SCHEMA_PATH,
    EnvironmentReport,
    to_dict,
    validate_report_dict,
    write_json,
    write_markdown,
)
from cdss.rowstats import ObjectRowStats, WatermarkColumn
from cdss.surface import SurfaceObject
from cdss.verify_env import VersionInfo

VERSION = VersionInfo(
    version_string="Microsoft SQL Server 2019 (CU32-GDR)",
    product_version="15.0.4460.4",
    edition="Developer Edition",
    engine_edition="3",
    database_name="INDICI_BI_Full",
)

SURFACE = [
    SurfaceObject(schema="dbo", name="Patient", object_type="table", can_select=True),
    SurfaceObject(schema="dbo", name="vw_Appointments", object_type="view", can_select=True),
]

RECONCILIATION = ReconciliationResult(
    entries=[
        ReconciliationEntry(
            export_name="dbo.Patient", status="found_as_table", matched_object="dbo.Patient"
        ),
        ReconciliationEntry(export_name="dbo.Ghost", status="missing", matched_object=None),
    ],
    extra_objects=["dbo.vw_Appointments"],
)

ROW_STATS = [
    ObjectRowStats(
        qualified_name="dbo.Patient",
        object_type="table",
        row_count=42,
        row_count_status="exact",
        duration_ms=1.5,
        watermark_columns=[
            WatermarkColumn(
                column_name="InsertedAt",
                data_type="datetime",
                min_value="2020-01-01",
                max_value="2026-01-01",
            )
        ],
    ),
    ObjectRowStats(
        qualified_name="dbo.NoWatermark",
        object_type="table",
        row_count=None,
        row_count_status="indeterminate",
        duration_ms=15000.0,
        watermark_columns=[],
    ),
]

REPORT = EnvironmentReport(
    generated_at="2026-07-15T00:00:00+00:00",
    version=VERSION,
    surface=SURFACE,
    reconciliation=RECONCILIATION,
    row_stats=ROW_STATS,
)


def test_to_dict_shape() -> None:
    data = to_dict(REPORT)
    assert data["generated_at"] == "2026-07-15T00:00:00+00:00"
    assert data["database"]["database_name"] == "INDICI_BI_Full"
    assert data["surface"] == {"total_objects": 2, "views": 1, "tables": 1, "other": 0}
    assert data["reconciliation"]["export_object_count"] == 2
    assert data["reconciliation"]["entries"][0] == {
        "export_name": "dbo.Patient",
        "status": "found_as_table",
        "matched_object": "dbo.Patient",
    }
    assert data["reconciliation"]["extra_objects"] == ["dbo.vw_Appointments"]
    assert data["row_stats"][0]["row_count"] == 42
    assert data["row_stats"][0]["watermark_columns"][0]["column_name"] == "InsertedAt"


def test_to_dict_validates_against_schema() -> None:
    validate_report_dict(to_dict(REPORT))


def test_validate_report_dict_raises_on_invalid() -> None:
    with pytest.raises(jsonschema.ValidationError):
        validate_report_dict({"generated_at": "now"})


def test_write_json_creates_parent_dirs_and_is_valid(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "env-report.json"
    write_json(REPORT, out)

    data = json.loads(out.read_text(encoding="utf-8"))
    jsonschema.validate(instance=data, schema=json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    assert data["database"]["database_name"] == "INDICI_BI_Full"


def test_write_json_is_idempotent_overwrite(tmp_path: Path) -> None:
    out = tmp_path / "env-report.json"
    write_json(REPORT, out)
    first = out.read_text(encoding="utf-8")
    write_json(REPORT, out)
    second = out.read_text(encoding="utf-8")
    assert first == second


def test_write_markdown_contains_key_sections(tmp_path: Path) -> None:
    out = tmp_path / "env-report.md"
    write_markdown(REPORT, out)
    text = out.read_text(encoding="utf-8")

    assert "INDICI_BI_Full" in text
    assert "15.0.4460.4" in text
    assert "dbo.Ghost" in text  # missing entry surfaced as a discrepancy
    assert "missing" in text
    assert "dbo.NoWatermark" in text  # watermark-less object called out
    assert "1 objects have no" in text or "1 object" in text
