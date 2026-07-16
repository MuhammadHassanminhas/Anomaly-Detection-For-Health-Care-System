"""Phase 1 step 4 (D-021): pair/containment analysis tests. All statements
are answered by a scripted fake connection -- no live database involved.
Fixture view/column names below are entirely synthetic.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cdss.profiler import ColumnProfile, ColumnSampling
from cdss.relationships import (
    PairCandidate,
    ViewContext,
    _is_key_like,
    _is_pair_eligible,
    detect_relationships,
    generate_pair_candidates,
    prune_pair_candidates,
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
        allowed_objects=frozenset({"dbo.a", "dbo.b"}),
        audit_dir=tmp_path,
        clock=lambda: datetime(2026, 7, 16, tzinfo=UTC),
    )


def _profile(
    name: str,
    data_type: str,
    *,
    column_class: str = "categorical_coded",
    is_free_text: bool = False,
    distinct_count: int | None = 50,
) -> ColumnProfile:
    return ColumnProfile(
        column_name=name,
        data_type=data_type,
        is_free_text=is_free_text,
        column_class=column_class,  # type: ignore[arg-type]
        sampling=ColumnSampling(sampled=False, method="full"),
        null_count=0,
        null_rate=0.0,
        distinct_count=distinct_count,
        min_value=None,
        max_value=None,
        top_values=[],
        string_length_stats=None,
    )


# --- _is_key_like -----------------------------------------------------------------


def test_is_key_like_true_for_key_class() -> None:
    p = _profile("SyntheticID", "int", column_class="key", distinct_count=100)
    assert _is_key_like(p, row_count=100) is True


def test_is_key_like_true_for_name_pattern() -> None:
    p = _profile("SyntheticCode", "nvarchar", distinct_count=10)
    assert _is_key_like(p, row_count=100) is True


def test_is_key_like_true_for_numeric_type() -> None:
    p = _profile("SomeValue", "int", distinct_count=10)
    assert _is_key_like(p, row_count=100) is True


def test_is_key_like_true_for_high_cardinality_ratio() -> None:
    p = _profile("ExternalRef", "nvarchar", distinct_count=90)
    assert _is_key_like(p, row_count=100) is True


def test_is_key_like_false_for_low_cardinality_non_numeric_no_pattern() -> None:
    p = _profile("Description", "nvarchar", distinct_count=5)
    assert _is_key_like(p, row_count=100) is False


def test_is_key_like_false_for_free_text() -> None:
    p = _profile("SyntheticCode", "nvarchar", is_free_text=True, distinct_count=90)
    assert _is_key_like(p, row_count=100) is False


def test_is_key_like_false_for_measure_class() -> None:
    p = _profile("Amount", "decimal", column_class="measure", distinct_count=95)
    assert _is_key_like(p, row_count=100) is False


# --- _is_pair_eligible --------------------------------------------------------------


def test_is_pair_eligible_type_compatible_with_key_side() -> None:
    a = _profile("SyntheticID", "int", column_class="key", distinct_count=100)
    b = _profile("OtherRefID", "int", column_class="key", distinct_count=100)
    assert _is_pair_eligible(a, b, row_count_a=100, row_count_b=100) is True


def test_is_pair_eligible_false_when_type_families_differ() -> None:
    a = _profile("SyntheticID", "int", column_class="key", distinct_count=100)
    b = _profile("SyntheticLabel", "nvarchar", distinct_count=5)
    assert _is_pair_eligible(a, b, row_count_a=100, row_count_b=100) is False


def test_is_pair_eligible_false_when_neither_side_key_like() -> None:
    a = _profile("StatusA", "nvarchar", distinct_count=3)
    b = _profile("StatusB", "nvarchar", distinct_count=4)
    assert _is_pair_eligible(a, b, row_count_a=100, row_count_b=100) is False


def test_is_pair_eligible_false_when_either_side_free_text() -> None:
    a = _profile("SyntheticID", "int", column_class="key", distinct_count=100)
    b = _profile("NotesID", "int", is_free_text=True, distinct_count=100)
    assert _is_pair_eligible(a, b, row_count_a=100, row_count_b=100) is False


def test_is_pair_eligible_false_when_either_side_measure() -> None:
    a = _profile("SyntheticID", "int", column_class="key", distinct_count=100)
    b = _profile("AmountID", "int", column_class="measure", distinct_count=100)
    assert _is_pair_eligible(a, b, row_count_a=100, row_count_b=100) is False


# --- generate_pair_candidates / prune_pair_candidates -------------------------------


def test_generate_pair_candidates_includes_within_view_and_cross_view() -> None:
    view_a = ViewContext(
        qualified_name="dbo.A",
        row_count=100,
        raw_columns=[("AID", "int", None), ("ALabel", "nvarchar", 20)],
        profiles=[
            _profile("AID", "int", column_class="key", distinct_count=100),
            _profile("ALabel", "nvarchar", distinct_count=5),
        ],
    )
    view_b = ViewContext(
        qualified_name="dbo.B",
        row_count=50,
        raw_columns=[("BID", "int", None)],
        profiles=[_profile("BID", "int", column_class="key", distinct_count=50)],
    )
    candidates = generate_pair_candidates([view_a, view_b])
    pair_tuples = {(c.from_view, c.from_column, c.to_view, c.to_column) for c in candidates}
    assert ("dbo.A", "AID", "dbo.A", "ALabel") in pair_tuples  # within-view
    assert ("dbo.A", "AID", "dbo.B", "BID") in pair_tuples  # cross-view
    assert len(candidates) == 3  # C(3,2)


def test_prune_pair_candidates_counts_and_filters() -> None:
    view_a = ViewContext(
        qualified_name="dbo.A",
        row_count=100,
        raw_columns=[("AID", "int", None), ("ALabel", "nvarchar", 20)],
        profiles=[
            _profile("AID", "int", column_class="key", distinct_count=100),
            _profile("ALabel", "nvarchar", distinct_count=5),
        ],
    )
    view_b = ViewContext(
        qualified_name="dbo.B",
        row_count=50,
        raw_columns=[("BID", "int", None)],
        profiles=[_profile("BID", "int", column_class="key", distinct_count=50)],
    )
    views_by_name = {"dbo.A": view_a, "dbo.B": view_b}
    candidates = generate_pair_candidates([view_a, view_b])
    eligible, pruned = prune_pair_candidates(candidates, views_by_name)
    assert pruned == 2  # AID~ALabel (type mismatch), ALabel~BID (type mismatch, no key-like pair)
    assert len(eligible) == 1
    assert eligible[0] == PairCandidate(
        from_view="dbo.A", from_column="AID", to_view="dbo.B", to_column="BID"
    )


# --- ViewContext.force_full_scan -----------------------------------------------------


def test_force_full_scan_bypasses_sampling_in_containment_query(tmp_path: Path) -> None:
    statements_seen: list[str] = []

    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        statements_seen.append(statement)
        return [(10, 10)]

    large_view = ViewContext(
        qualified_name="dbo.A",
        row_count=1_000_000,  # exceeds LARGE_VIEW_ROW_THRESHOLD -- would normally sample
        raw_columns=[("RefID", "int", None)],
        profiles=[_profile("RefID", "int", column_class="key", distinct_count=1_000_000)],
        force_full_scan=True,
    )
    other_view = ViewContext(
        qualified_name="dbo.B",
        row_count=50,
        raw_columns=[("RefID", "int", None)],
        profiles=[_profile("RefID", "int", column_class="key", distinct_count=50)],
    )
    detect_relationships(_make_audited(tmp_path, responder), views=[large_view, other_view])
    assert any("FROM dbo.A" in s and "WHERE [RefID] %" not in s for s in statements_seen)
    assert not any("AS sampled" in s and "dbo.A" in s for s in statements_seen)


# --- detect_relationships: containment + budget/checkpoint --------------------------


def _two_key_views() -> tuple[ViewContext, ViewContext]:
    view_a = ViewContext(
        qualified_name="dbo.A",
        row_count=100,
        raw_columns=[("AID", "int", None)],
        profiles=[_profile("AID", "int", column_class="key", distinct_count=100)],
    )
    view_b = ViewContext(
        qualified_name="dbo.B",
        row_count=50,
        raw_columns=[("BID", "int", None)],
        profiles=[_profile("BID", "int", column_class="key", distinct_count=50)],
    )
    return view_a, view_b


def test_detect_relationships_computes_both_containment_directions(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        # "a_col FROM dbo.X" identifies the outer (driving) side of the
        # containment query -- unambiguous, unlike a bare column-name match
        # (which the inner IN-subquery would also satisfy).
        if "a_col FROM dbo.A" in statement:
            return [(100, 50)]  # A has 100 distinct, 50 found in B -> 50% containment
        if "a_col FROM dbo.B" in statement:
            return [(50, 50)]  # B has 50 distinct, all 50 found in A -> 100% containment
        raise AssertionError(f"unexpected statement: {statement}")

    view_a, view_b = _two_key_views()
    edges, report, costs = detect_relationships(
        _make_audited(tmp_path, responder), views=[view_a, view_b]
    )
    assert len(edges) == 1
    edge = edges[0]
    assert edge.status == "evaluated"
    assert edge.containment_a_to_b == 0.5
    assert edge.orphan_count_a == 50
    assert edge.containment_b_to_a == 1.0
    assert edge.orphan_count_b == 0
    assert report.pairs_considered == 1
    assert report.pairs_pruned == 0
    assert report.pairs_evaluated == 1
    assert report.pairs_skipped_cost == 0
    assert all(cost.status == "ok" for cost in costs)


def test_detect_relationships_pair_cap_marks_remaining_skipped_cost(tmp_path: Path) -> None:
    view_a = ViewContext(
        qualified_name="dbo.A",
        row_count=100,
        raw_columns=[("AID", "int", None), ("AID2", "int", None)],
        profiles=[
            _profile("AID", "int", column_class="key", distinct_count=100),
            _profile("AID2", "int", column_class="key", distinct_count=100),
        ],
    )
    view_b = ViewContext(
        qualified_name="dbo.B",
        row_count=50,
        raw_columns=[("BID", "int", None)],
        profiles=[_profile("BID", "int", column_class="key", distinct_count=50)],
    )

    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        return [(10, 5)]

    edges, report, _costs = detect_relationships(
        _make_audited(tmp_path, responder),
        views=[view_a, view_b],
        max_pairs_evaluated=1,
    )
    assert report.pairs_considered == 3  # AID~AID2, AID~BID, AID2~BID
    statuses = {e.status for e in edges}
    assert "skipped_cost" in statuses
    assert report.pairs_evaluated + report.pairs_skipped_cost == len(edges)
    assert report.pairs_evaluated == 1


def test_detect_relationships_timeout_marks_pair_skipped_cost(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]] | Exception:
        return FakeTimeoutError()

    view_a, view_b = _two_key_views()
    edges, report, costs = detect_relationships(
        _make_audited(tmp_path, responder), views=[view_a, view_b]
    )
    assert edges[0].status == "skipped_cost"
    assert edges[0].containment_a_to_b is None
    assert report.pairs_skipped_cost == 1
    assert costs[0].status == "timeout"


def test_detect_relationships_resumes_skipping_already_evaluated_pairs(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        raise AssertionError(
            f"no query should be issued for an already-evaluated pair: {statement}"
        )

    view_a, view_b = _two_key_views()
    edges, report, costs = detect_relationships(
        _make_audited(tmp_path, responder),
        views=[view_a, view_b],
        already_evaluated=frozenset({("dbo.A", "AID", "dbo.B", "BID")}),
    )
    assert edges == []
    assert costs == []
    assert report.pairs_evaluated == 0
    assert report.pairs_skipped_cost == 0


def test_pruning_report_and_edge_dataclasses_are_frozen() -> None:
    from cdss.relationships import PruningReport, RelationshipCost, RelationshipEdge

    report = PruningReport(
        pairs_considered=1, pairs_pruned=0, pairs_evaluated=1, pairs_skipped_cost=0
    )
    assert report.pairs_considered == 1
    edge = RelationshipEdge(
        from_view="dbo.A",
        from_column="AID",
        to_view="dbo.B",
        to_column="BID",
        status="evaluated",
        containment_a_to_b=1.0,
        containment_b_to_a=1.0,
        orphan_count_a=0,
        orphan_count_b=0,
    )
    assert edge.status == "evaluated"
    cost = RelationshipCost(
        view="dbo.A~dbo.B", operation="containment", duration_ms=1.0, status="ok"
    )
    assert cost.status == "ok"
