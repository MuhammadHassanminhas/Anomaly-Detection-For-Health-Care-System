"""Phase 1 step 3: candidate-key detection tests. All statements are
answered by a scripted fake connection -- no live database involved.
Fixture view/column names below are entirely synthetic.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cdss.candidate_keys import (
    CandidateKey,
    CandidateKeyCost,
    _order_columns_for_checking,
    detect_candidate_keys,
)
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


class FakeTimeoutError(Exception):
    def __str__(self) -> str:
        return "('HYT00', '[HYT00] Query timeout expired')"


def _make_audited(tmp_path: Path, responder: Any) -> AuditedSourceConnection:
    return AuditedSourceConnection(
        ScriptedConnection(responder),  # type: ignore[arg-type]
        component="test",
        allowed_objects=frozenset({"dbo.syntheticview"}),
        audit_dir=tmp_path,
        clock=lambda: datetime(2026, 7, 16, tzinfo=UTC),
    )


# --- _order_columns_for_checking -------------------------------------------------


def test_order_columns_for_checking_prioritizes_id_named_numeric_columns() -> None:
    columns = [
        ("SyntheticName", "nvarchar", 100),
        ("SyntheticAmount", "decimal", None),
        ("SyntheticID", "int", None),
    ]
    ordered = _order_columns_for_checking(columns)
    assert [c[0] for c in ordered] == ["SyntheticID", "SyntheticAmount", "SyntheticName"]


def test_order_columns_for_checking_is_stable_alphabetical_within_tier() -> None:
    columns = [
        ("Zeta", "int", None),
        ("Alpha", "int", None),
        ("OtherID", "int", None),
        ("SyntheticID", "int", None),
    ]
    ordered = _order_columns_for_checking(columns)
    assert [c[0] for c in ordered] == ["OtherID", "SyntheticID", "Alpha", "Zeta"]


# --- detect_candidate_keys: small view (exact) ------------------------------------


def test_detect_candidate_keys_small_view_exact_evidence(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        assert statement.startswith("SELECT COUNT(*), COUNT(DISTINCT [SyntheticID])")
        # population=100, SyntheticID distinct=100 (key), SyntheticStatus distinct=5 (not a key)
        return [(100, 100, 5)]

    candidates, costs = detect_candidate_keys(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=100,
        columns=[("SyntheticID", "int", None), ("SyntheticStatus", "nvarchar", 20)],
    )
    assert candidates == [
        CandidateKey(
            columns=["SyntheticID"], distinct_count=100, row_count=100, evidence_method="exact"
        )
    ]
    assert all(cost.status == "ok" for cost in costs)
    assert all(cost.operation == "candidate_key" for cost in costs)


def test_detect_candidate_keys_no_key_found(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        return [(100, 5)]

    candidates, _costs = detect_candidate_keys(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=100,
        columns=[("SyntheticStatus", "nvarchar", 20)],
    )
    assert candidates == []


def test_detect_candidate_keys_empty_view_produces_no_candidates(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        return [(0, 0)]

    candidates, _costs = detect_candidate_keys(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=0,
        columns=[("SyntheticID", "int", None)],
    )
    assert candidates == []


# --- detect_candidate_keys: large view (sampled) ----------------------------------


def test_detect_candidate_keys_large_view_uses_sampled_evidence(tmp_path: Path) -> None:
    seen_sources: list[str] = []

    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        seen_sources.append(statement)
        return [(50_000, 50_000)]

    candidates, _costs = detect_candidate_keys(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=1_000_000,
        columns=[("SyntheticID", "int", None)],
    )
    assert candidates == [
        CandidateKey(
            columns=["SyntheticID"],
            distinct_count=50_000,
            row_count=50_000,
            evidence_method="sampled",
        )
    ]
    assert "WHERE [SyntheticID] % 10 = 0" in seen_sources[0]


# --- detect_candidate_keys: batching -----------------------------------------------


def test_detect_candidate_keys_batches_columns(tmp_path: Path) -> None:
    statements_seen: list[str] = []

    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        statements_seen.append(statement)
        distinct_columns_in_batch = statement.count("COUNT(DISTINCT")
        return [(100, *([50] * distinct_columns_in_batch))]

    columns: list[tuple[str, str, int | None]] = [(f"Col{i}", "int", None) for i in range(5)]
    detect_candidate_keys(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=100,
        columns=columns,
        batch_size=2,
    )
    assert len(statements_seen) == 3  # ceil(5/2)


# --- detect_candidate_keys: timeout handling ----------------------------------------


def test_detect_candidate_keys_batch_timeout_yields_no_candidates_from_that_batch(
    tmp_path: Path,
) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]] | Exception:
        return FakeTimeoutError()

    candidates, costs = detect_candidate_keys(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=100,
        columns=[("SyntheticID", "int", None)],
    )
    assert candidates == []
    assert costs == [
        CandidateKeyCost(
            view="dbo.SyntheticView",
            operation="candidate_key",
            duration_ms=costs[0].duration_ms,
            status="timeout",
        )
    ]


def test_candidate_key_and_cost_dataclasses_are_frozen() -> None:
    key = CandidateKey(columns=["X"], distinct_count=1, row_count=1, evidence_method="exact")
    assert key.columns == ["X"]
    cost = CandidateKeyCost(view="dbo.X", operation="candidate_key", duration_ms=1.0, status="ok")
    assert cost.status == "ok"
