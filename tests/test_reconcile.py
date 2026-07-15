import json
from pathlib import Path

from cdss.reconcile import ReconciliationEntry, load_export_names, reconcile
from cdss.surface import SurfaceObject

SURFACE = [
    SurfaceObject(schema="dbo", name="Patient", object_type="table", can_select=True),
    SurfaceObject(schema="dbo", name="vw_Appointments", object_type="view", can_select=True),
    SurfaceObject(schema="dbo", name="SomeOtherThing", object_type="other", can_select=True),
    SurfaceObject(schema="dbo", name="ExtraObject", object_type="table", can_select=True),
]


def test_load_export_names_extracts_table_field(tmp_path: Path) -> None:
    export_file = tmp_path / "export.json"
    export_file.write_text(
        json.dumps([{"table": "dbo.Foo", "columns": []}, {"table": "dbo.Bar", "columns": []}]),
        encoding="utf-8",
    )
    names = load_export_names(export_file)
    assert names == ["dbo.Foo", "dbo.Bar"]


def test_reconcile_found_as_table() -> None:
    result = reconcile(["dbo.Patient"], SURFACE)
    assert result.entries == [
        ReconciliationEntry(
            export_name="dbo.Patient", status="found_as_table", matched_object="dbo.Patient"
        )
    ]


def test_reconcile_found_as_view() -> None:
    result = reconcile(["dbo.vw_Appointments"], SURFACE)
    assert result.entries[0].status == "found_as_view"


def test_reconcile_found_as_other() -> None:
    result = reconcile(["dbo.SomeOtherThing"], SURFACE)
    assert result.entries[0].status == "found_as_other"


def test_reconcile_missing() -> None:
    result = reconcile(["dbo.DoesNotExist"], SURFACE)
    assert result.entries[0].status == "missing"
    assert result.entries[0].matched_object is None


def test_reconcile_case_insensitive_match() -> None:
    result = reconcile(["DBO.PATIENT"], SURFACE)
    assert result.entries[0].status == "found_as_table"
    assert result.entries[0].matched_object == "dbo.Patient"


def test_reconcile_extra_objects_lists_unmatched_surface() -> None:
    result = reconcile(["dbo.Patient"], SURFACE)
    assert "dbo.ExtraObject" in result.extra_objects
    assert "dbo.vw_Appointments" in result.extra_objects
    assert "dbo.SomeOtherThing" in result.extra_objects
    assert "dbo.Patient" not in result.extra_objects


def test_reconcile_extra_objects_sorted() -> None:
    result = reconcile([], SURFACE)
    assert result.extra_objects == sorted(result.extra_objects)


def test_reconcile_preserves_export_order() -> None:
    result = reconcile(["dbo.vw_Appointments", "dbo.Patient", "dbo.DoesNotExist"], SURFACE)
    assert [entry.export_name for entry in result.entries] == [
        "dbo.vw_Appointments",
        "dbo.Patient",
        "dbo.DoesNotExist",
    ]
