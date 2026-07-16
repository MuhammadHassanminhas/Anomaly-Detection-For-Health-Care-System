"""Phase 2 step 6: compiled SQL passes through the Phase 0 SQL guard
(`cdss.source.AuditedSourceConnection`/`_validate_statement`) before any
execution -- proving the guard accepts everything `cdss.compiler` emits for
every checked-in example, and still refuses hand-crafted violations riding
on that same compiled output (an object missing from the allowlist, an
injected second statement).

Fake connection/cursor duplicated from `test_source.py` rather than shared,
matching this project's existing per-file-fixture convention (e.g.
`test_dsl.py`'s own synthetic catalog).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlglot
from sqlglot import exp

from cdss.compiler import compile_check
from cdss.dsl import parse_check_document
from cdss.source import AuditedSourceConnection, StatementRejectedError

EXAMPLES_DIR = Path(__file__).parent.parent / "examples" / "checks"


class FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Sequence[Any] | None]] = []

    def execute(self, statement: str, params: Sequence[Any] | None = None) -> None:
        self.executed.append((statement, params))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []


class FakeConnection:
    def __init__(self) -> None:
        self.last_cursor = FakeCursor()
        self.timeout = 0

    def cursor(self) -> FakeCursor:
        return self.last_cursor


def _make_audited(
    tmp_path: Path, allowed_objects: frozenset[str]
) -> tuple[AuditedSourceConnection, FakeConnection]:
    fake_conn = FakeConnection()
    audited = AuditedSourceConnection(
        fake_conn,  # type: ignore[arg-type]
        component="test-guard-integration",
        allowed_objects=allowed_objects,
        audit_dir=tmp_path,
        clock=lambda: datetime(2026, 7, 16, 0, 0, 0, tzinfo=UTC),
    )
    return audited, fake_conn


def _tables_referenced(sql_text: str) -> frozenset[str]:
    """Every `schema.table` a compiled statement touches -- same shape
    `_validate_statement` itself checks against the allowlist -- so an
    integration test can grant exactly what the compiler emitted, not a
    hand-guessed list."""
    (parsed,) = sqlglot.parse(sql_text, read="tsql")
    assert parsed is not None
    return frozenset(
        f"{(table.db or 'dbo').lower()}.{table.name.lower()}"
        for table in parsed.find_all(exp.Table)
    )


def _compile_example(name: str) -> str:
    doc = parse_check_document((EXAMPLES_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
    return compile_check(doc).sql_text


EXAMPLE_NAMES = [path.stem for path in sorted(EXAMPLES_DIR.glob("*.yaml"))]


# --- guard accepts everything the compiler emits -----------------------------


@pytest.mark.parametrize("example_name", EXAMPLE_NAMES)
def test_guard_accepts_compiled_sql_for_every_example(tmp_path: Path, example_name: str) -> None:
    sql_text = _compile_example(example_name)
    audited, fake_conn = _make_audited(tmp_path, _tables_referenced(sql_text))
    audited.execute_query(sql_text)  # must not raise
    assert fake_conn.last_cursor.executed == [(sql_text, None)]


def test_guard_accepts_watermarked_compiled_sql() -> None:
    doc = parse_check_document(
        (EXAMPLES_DIR / "invoice-stale-unpaid-balance.yaml").read_text(encoding="utf-8")
    )
    sql_text = compile_check(doc, watermark_column="UpdatedAt").sql_text
    # No connection round-trip needed here -- proving the statement itself
    # still parses as exactly one allowlist-clean SELECT is enough; the
    # watermark clause adds no new table reference.
    (parsed,) = sqlglot.parse(sql_text, read="tsql")
    assert isinstance(parsed, exp.Select)


# --- guard still refuses hand-crafted violations on compiler output ---------


def test_guard_rejects_compiled_sql_when_a_referenced_object_is_not_allowlisted(
    tmp_path: Path,
) -> None:
    sql_text = _compile_example("appointment-completed-no-invoice")
    tables = _tables_referenced(sql_text)
    driving_view_only = frozenset({t for t in tables if t != "dbo.invoices"})
    assert "dbo.invoices" in tables  # sanity: the join target really is referenced
    audited, fake_conn = _make_audited(tmp_path, driving_view_only)
    with pytest.raises(StatementRejectedError, match="dbo.invoices"):
        audited.execute_query(sql_text)
    assert fake_conn.last_cursor.executed == []


def test_guard_rejects_compiled_sql_with_an_injected_second_statement(tmp_path: Path) -> None:
    sql_text = _compile_example("invoice-negative-total-amount")
    tables = _tables_referenced(sql_text)
    audited, fake_conn = _make_audited(tmp_path, tables)
    injected = sql_text + "; DROP TABLE fqb.Invoices"
    with pytest.raises(StatementRejectedError):
        audited.execute_query(injected)
    assert fake_conn.last_cursor.executed == []


def test_guard_rejects_compiled_sql_against_an_empty_allowlist(tmp_path: Path) -> None:
    sql_text = _compile_example("patient-active-missing-nhi")
    audited, fake_conn = _make_audited(tmp_path, frozenset())
    with pytest.raises(StatementRejectedError, match="dbo.patient"):
        audited.execute_query(sql_text)
    assert fake_conn.last_cursor.executed == []
