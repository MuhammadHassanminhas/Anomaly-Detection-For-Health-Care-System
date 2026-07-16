"""Phase 1 step 8: end-to-end orchestration test. A single scripted
connection drives `run_profiling()` through every step (2-7) for one
synthetic view, proving the wiring end-to-end -- each individual step's
internal logic is already unit-tested in its own module (test_profiler.py,
test_archetype.py, test_candidate_keys.py, test_sentinels.py,
test_watermarks.py, test_relationships.py, test_export_reconciliation.py).
Fixture view/column names are entirely synthetic.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cdss.catalog import validate_catalog_dict
from cdss.profile import DEFAULT_CATALOG_DIR, run_profiling
from cdss.profiler import COLUMN_METADATA_QUERY
from cdss.source import AuditedSourceConnection


class ScriptedCursor:
    def __init__(self, responder: Any, connection: "ScriptedConnection") -> None:
        self._responder = responder
        self._connection = connection
        self._last_statement = ""

    def execute(self, statement: str, _params: Any = None) -> None:
        self._last_statement = statement

    def fetchall(self) -> list[tuple[Any, ...]]:
        result = self._responder(self._last_statement, self._connection.timeout)
        if isinstance(result, Exception):
            raise result
        return result  # type: ignore[no-any-return]


class ScriptedConnection:
    def __init__(self, responder: Any) -> None:
        self._responder = responder
        self.timeout = 0

    def cursor(self) -> ScriptedCursor:
        return ScriptedCursor(self._responder, self)


def _make_audited(tmp_path: Path, responder: Any) -> AuditedSourceConnection:
    return AuditedSourceConnection(
        ScriptedConnection(responder),  # type: ignore[arg-type]
        component="test",
        allowed_objects=frozenset({"dbo.syntheticview"}),
        audit_dir=tmp_path,
        clock=lambda: datetime(2026, 7, 16, tzinfo=UTC),
    )


PROFILE_SQL = (
    "SELECT COUNT(*), COUNT(*) - COUNT([SyntheticID]), COUNT(DISTINCT [SyntheticID]), "
    "MIN([SyntheticID]), MAX([SyntheticID]), COUNT(*) - COUNT([SyntheticName]), "
    "COUNT(DISTINCT [SyntheticName]), MIN(LEN([SyntheticName])), MAX(LEN([SyntheticName])), "
    "AVG(CAST(LEN([SyntheticName]) AS FLOAT)) FROM dbo.SyntheticView"
)
SENTINEL_SQL = (
    "SELECT TOP (1) [SyntheticID], COUNT(*) FROM dbo.SyntheticView "
    "GROUP BY [SyntheticID] ORDER BY COUNT(*) DESC"
)
TOP_K_SQL = (
    "SELECT TOP (20) [SyntheticName], COUNT(*) FROM dbo.SyntheticView "
    "GROUP BY [SyntheticName] ORDER BY COUNT(*) DESC"
)
ARCHETYPE_SQL = "SELECT COUNT(*), COUNT(DISTINCT [SyntheticName]) FROM dbo.SyntheticView"
CANDIDATE_KEY_SQL = (
    "SELECT COUNT(*), COUNT(DISTINCT [SyntheticID]), COUNT(DISTINCT [SyntheticName]) "
    "FROM dbo.SyntheticView"
)


def _responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
    responses: dict[str, list[tuple[Any, ...]]] = {
        COLUMN_METADATA_QUERY: [("SyntheticID", "int", None), ("SyntheticName", "nvarchar", 50)],
        "SELECT COUNT(*) FROM dbo.SyntheticView": [(10,)],
        PROFILE_SQL: [(10, 0, 10, 1, 10, 0, 8, 3, 12, 7.5)],
        SENTINEL_SQL: [(1, 1)],
        TOP_K_SQL: [("Alpha", 5), ("Beta", 3), ("Gamma", 2)],
        ARCHETYPE_SQL: [(10, 8)],
        CANDIDATE_KEY_SQL: [(10, 10, 8)],
    }
    if statement not in responses:
        raise AssertionError(f"unexpected statement: {statement}")
    return responses[statement]


def test_run_profiling_end_to_end_single_view(tmp_path: Path) -> None:
    export_path = tmp_path / "export.txt"
    export_path.write_text(
        json.dumps(
            [
                {
                    "table": "dbo.SyntheticView",
                    "columns": [],
                    "tablerelations": "",
                    "columnsinformation": "",
                }
            ]
        ),
        encoding="utf-8",
    )
    env_report_path = tmp_path / "env-report.json"
    env_report_path.write_text(json.dumps({"row_stats": []}), encoding="utf-8")
    checkpoint_path = tmp_path / ".profile-checkpoint.json"

    audited = _make_audited(tmp_path, _responder)

    catalog_dict, discrepancy_reports = run_profiling(
        audited,
        views=("dbo.SyntheticView",),
        export_hypotheses_path=export_path,
        env_report_path=env_report_path,
        checkpoint_path=checkpoint_path,
        catalog_version=1,
        produced_at="2026-07-16T00:00:00+00:00",
        source_database="SyntheticDB",
    )

    validate_catalog_dict(catalog_dict)  # must not raise

    assert catalog_dict["catalog_version"] == 1
    assert catalog_dict["source_database"] == "SyntheticDB"
    assert [v["qualified_name"] for v in catalog_dict["views"]] == ["dbo.SyntheticView"]

    view = catalog_dict["views"][0]
    assert view["row_count"] == 10
    assert view["row_count_status"] == "exact"
    assert view["archetype"] == "fact"
    assert {c["column_name"] for c in view["columns"]} == {"SyntheticID", "SyntheticName"}
    assert view["candidate_keys"] == [
        {
            "columns": ["SyntheticID"],
            "distinct_count": 10,
            "row_count": 10,
            "evidence_method": "exact",
        }
    ]
    assert view["watermark_classification"] == {"status": "fallback_needed", "columns": []}

    # The only within-view pair (SyntheticID x SyntheticName) is type-incompatible
    # (numeric vs. string) -- pruned, never issuing a containment query.
    assert catalog_dict["pruning_report"] == {
        "pairs_considered": 1,
        "pairs_pruned": 1,
        "pairs_evaluated": 0,
        "pairs_skipped_cost": 0,
    }
    assert catalog_dict["relationships"] == []

    assert len(discrepancy_reports) == 1
    assert discrepancy_reports[0].qualified_name == "dbo.SyntheticView"


def test_default_catalog_dir_is_under_artifacts() -> None:
    assert Path("artifacts/catalog") == DEFAULT_CATALOG_DIR
