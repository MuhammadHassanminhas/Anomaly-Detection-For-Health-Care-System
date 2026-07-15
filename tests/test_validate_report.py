import json
from pathlib import Path

from cdss.validate_report import main, validate_file

VALID_REPORT = {
    "generated_at": "2026-07-15T00:00:00+00:00",
    "database": {
        "version_string": "Microsoft SQL Server 2019",
        "product_version": "15.0.4460.4",
        "edition": "Developer Edition",
        "engine_edition": "3",
        "database_name": "INDICI_BI_Full",
    },
    "surface": {"total_objects": 1, "views": 1, "tables": 0, "other": 0},
    "reconciliation": {
        "export_object_count": 1,
        "entries": [
            {
                "export_name": "dbo.Patient",
                "status": "found_as_view",
                "matched_object": "dbo.Patient",
            }
        ],
        "extra_objects": [],
    },
    "row_stats": [],
}


def test_validate_file_returns_none_for_valid_report(tmp_path: Path) -> None:
    path = tmp_path / "env-report.json"
    path.write_text(json.dumps(VALID_REPORT), encoding="utf-8")
    assert validate_file(path) is None


def test_validate_file_returns_error_for_invalid_report(tmp_path: Path) -> None:
    path = tmp_path / "env-report.json"
    path.write_text(json.dumps({"generated_at": "now"}), encoding="utf-8")
    error = validate_file(path)
    assert error is not None
    assert "database" in error


def test_main_returns_0_for_valid_file(tmp_path: Path) -> None:
    path = tmp_path / "env-report.json"
    path.write_text(json.dumps(VALID_REPORT), encoding="utf-8")
    assert main([str(path)]) == 0


def test_main_returns_1_for_invalid_file(tmp_path: Path) -> None:
    path = tmp_path / "env-report.json"
    path.write_text(json.dumps({"generated_at": "now"}), encoding="utf-8")
    assert main([str(path)]) == 1
