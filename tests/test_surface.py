from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cdss.source import AuditedSourceConnection
from cdss.surface import SELECTABLE_QUERY, TABLES_QUERY, enumerate_surface


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

    def cursor(self) -> ScriptedCursor:
        return self._cursor


RESPONSES: dict[str, list[tuple[Any, ...]]] = {
    TABLES_QUERY: [
        ("dbo", "Appointments", "BASE TABLE"),
        ("dbo", "vw_ActiveAppointments", "VIEW"),
        ("AIFinanceAssistant", "VU_PracticeTotalExpenses", "VIEW"),
    ],
    SELECTABLE_QUERY: [
        ("dbo", "vw_ActiveAppointments"),
        ("AIFinanceAssistant", "VU_PracticeTotalExpenses"),
    ],
}


def _make_audited(
    tmp_path: Path, responses: dict[str, list[tuple[Any, ...]]] = RESPONSES
) -> AuditedSourceConnection:
    return AuditedSourceConnection(
        ScriptedConnection(responses),  # type: ignore[arg-type]
        component="test",
        audit_dir=tmp_path,
        clock=lambda: datetime(2026, 7, 14, tzinfo=UTC),
    )


def test_enumerate_surface_tags_object_types(tmp_path: Path) -> None:
    objects = enumerate_surface(_make_audited(tmp_path))
    by_name = {obj.qualified_name: obj for obj in objects}

    assert by_name["dbo.Appointments"].object_type == "table"
    assert by_name["dbo.Appointments"].can_select is False

    assert by_name["dbo.vw_ActiveAppointments"].object_type == "view"
    assert by_name["dbo.vw_ActiveAppointments"].can_select is True

    assert by_name["AIFinanceAssistant.VU_PracticeTotalExpenses"].object_type == "view"
    assert by_name["AIFinanceAssistant.VU_PracticeTotalExpenses"].can_select is True


def test_enumerate_surface_preserves_count(tmp_path: Path) -> None:
    objects = enumerate_surface(_make_audited(tmp_path))
    assert len(objects) == 3


def test_enumerate_surface_unknown_table_type_is_other(tmp_path: Path) -> None:
    responses: dict[str, list[tuple[Any, ...]]] = {
        TABLES_QUERY: [("dbo", "weird_object", "FOREIGN TABLE")],
        SELECTABLE_QUERY: [],
    }
    objects = enumerate_surface(_make_audited(tmp_path, responses))
    assert objects[0].object_type == "other"
    assert objects[0].can_select is False


def test_enumerate_surface_audits_both_statements(tmp_path: Path) -> None:
    enumerate_surface(_make_audited(tmp_path))
    audit_file = tmp_path / "source-audit-20260714.jsonl"
    lines = audit_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
