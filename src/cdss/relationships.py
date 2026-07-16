"""Phase 1 step 4 (D-021): pair/containment analysis.

Covers both within-view composite candidate-key pairs and cross-view
relationship pairs under one cost-budgeted design:
  (a) pruning -- a pair is only considered when both columns are
      type-compatible and at least one side is key-like; free-text and
      `measure`-classified columns (D-020) are never a pair candidate;
  (b) containment computed on the same sampled population step 2 already
      profiled (D-018's predicate, reused deterministically) -- never a
      fresh full scan issued just for pair analysis;
  (c) a per-pair query budget, skip-and-log (`status: "skipped_cost"`) on
      breach rather than guessed (F10);
  (d) a hard cap on total pairs evaluated, with pruning stats reported
      (pairs considered -> pruned -> evaluated);
  (e) resumable checkpointing -- `detect_relationships()` accepts an
      `already_evaluated` set of pair keys so a resumed run does not
      re-score pairs an interrupted prior run already scored.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from cdss.profiler import (
    DEFAULT_SAMPLE_FRACTION,
    LARGE_VIEW_ROW_THRESHOLD,
    NUMERIC_ID_TYPES,
    STRING_TYPES,
    ColumnProfile,
    _is_timeout_error,
    determine_sampling,
)
from cdss.source import AuditedSourceConnection

RelationshipStatus = Literal["evaluated", "skipped_cost"]
RelationshipCostStatus = Literal["ok", "timeout"]

DEFAULT_MAX_PAIRS_EVALUATED = 50
DEFAULT_PER_PAIR_TIMEOUT_SECONDS = 15

# D-021 pruning signal: name-pattern hint only, combined with type/cardinality
# signals in _is_key_like -- never sufficient evidence on its own.
KEY_LIKE_NAME_PATTERNS: frozenset[str] = frozenset({"id", "code", "key"})
# D-021: "cardinality above a floor" -- a column whose distinct/row_count
# ratio clears this is key-like even without a matching name or numeric type
# (e.g. a fixed-format high-cardinality string code).
KEY_LIKE_CARDINALITY_RATIO_FLOOR = 0.5


@dataclass(frozen=True)
class ViewContext:
    """Everything pair analysis needs for one already-profiled view --
    reuses step 2's output, never re-profiles."""

    qualified_name: str
    row_count: int
    raw_columns: list[tuple[str, str, int | None]]
    profiles: list[ColumnProfile]
    watermark_column: tuple[str, str, str] | None = None
    force_full_scan: bool = False
    """Bypass D-018 sampling for this view's side of a containment query.
    Evidence-gathering use only (e.g. isolating a fact-side sampling effect
    from a reference-side one) -- normal profiling always leaves this False
    and goes through determine_sampling() like every other step."""


@dataclass(frozen=True)
class PairCandidate:
    from_view: str
    from_column: str
    to_view: str
    to_column: str


@dataclass(frozen=True)
class RelationshipEdge:
    from_view: str
    from_column: str
    to_view: str
    to_column: str
    status: RelationshipStatus
    containment_a_to_b: float | None
    containment_b_to_a: float | None
    orphan_count_a: int | None
    orphan_count_b: int | None


@dataclass(frozen=True)
class PruningReport:
    pairs_considered: int
    pairs_pruned: int
    pairs_evaluated: int
    pairs_skipped_cost: int


@dataclass(frozen=True)
class RelationshipCost:
    view: str
    operation: Literal["containment"]
    duration_ms: float
    status: RelationshipCostStatus


def _pair_key(candidate: PairCandidate) -> tuple[str, str, str, str]:
    return (candidate.from_view, candidate.from_column, candidate.to_view, candidate.to_column)


def _type_family(data_type: str) -> str:
    lowered = data_type.lower()
    if lowered in NUMERIC_ID_TYPES:
        return "numeric"
    if lowered in STRING_TYPES:
        return "string"
    return "other"


def _is_key_like(profile: ColumnProfile, *, row_count: int) -> bool:
    if profile.is_free_text or profile.column_class == "measure":
        return False
    if profile.column_class == "key":
        return True
    if any(pattern in profile.column_name.lower() for pattern in KEY_LIKE_NAME_PATTERNS):
        return True
    if profile.data_type.lower() in NUMERIC_ID_TYPES:
        return True
    return (
        profile.distinct_count is not None
        and row_count > 0
        and profile.distinct_count / row_count >= KEY_LIKE_CARDINALITY_RATIO_FLOOR
    )


def _is_pair_eligible(
    profile_a: ColumnProfile, profile_b: ColumnProfile, *, row_count_a: int, row_count_b: int
) -> bool:
    """D-021 pruning: type-compatible, and at least one side key-like; free
    text and `measure` columns are never eligible on either side."""
    if profile_a.is_free_text or profile_b.is_free_text:
        return False
    if profile_a.column_class == "measure" or profile_b.column_class == "measure":
        return False
    family_a = _type_family(profile_a.data_type)
    family_b = _type_family(profile_b.data_type)
    if family_a != family_b or family_a == "other":
        return False
    return _is_key_like(profile_a, row_count=row_count_a) or _is_key_like(
        profile_b, row_count=row_count_b
    )


def generate_pair_candidates(views: list[ViewContext]) -> list[PairCandidate]:
    """Every unordered (view, column) pair across all profiled views,
    including within a single view (composite-key candidates) -- unfiltered,
    pruning happens separately so the "considered" count is always visible."""
    flat: list[tuple[str, ColumnProfile]] = [
        (view.qualified_name, profile) for view in views for profile in view.profiles
    ]
    candidates: list[PairCandidate] = []
    for i in range(len(flat)):
        for j in range(i + 1, len(flat)):
            view_a, profile_a = flat[i]
            view_b, profile_b = flat[j]
            if view_a == view_b and profile_a.column_name == profile_b.column_name:
                continue
            candidates.append(
                PairCandidate(
                    from_view=view_a,
                    from_column=profile_a.column_name,
                    to_view=view_b,
                    to_column=profile_b.column_name,
                )
            )
    return candidates


def prune_pair_candidates(
    candidates: list[PairCandidate], views_by_name: dict[str, ViewContext]
) -> tuple[list[PairCandidate], int]:
    """Returns (eligible, pruned_count)."""
    eligible: list[PairCandidate] = []
    pruned = 0
    for candidate in candidates:
        view_a = views_by_name[candidate.from_view]
        view_b = views_by_name[candidate.to_view]
        profile_a = next(p for p in view_a.profiles if p.column_name == candidate.from_column)
        profile_b = next(p for p in view_b.profiles if p.column_name == candidate.to_column)
        if _is_pair_eligible(
            profile_a, profile_b, row_count_a=view_a.row_count, row_count_b=view_b.row_count
        ):
            eligible.append(candidate)
        else:
            pruned += 1
    return eligible, pruned


def _sampled_source(view: ViewContext) -> str:
    if view.force_full_scan:
        return view.qualified_name
    numeric_id_columns = [
        name for name, data_type, _cl in view.raw_columns if data_type.lower() in NUMERIC_ID_TYPES
    ]
    anchor_column = view.raw_columns[0][0] if view.raw_columns else None
    _sampled, _method, predicate = determine_sampling(
        row_count=view.row_count,
        numeric_id_columns=numeric_id_columns,
        watermark_column=view.watermark_column,
        anchor_column=anchor_column,
        large_view_threshold=LARGE_VIEW_ROW_THRESHOLD,
        sample_fraction=DEFAULT_SAMPLE_FRACTION,
    )
    if predicate is None:
        return view.qualified_name
    return f"(SELECT * FROM {view.qualified_name} WHERE {predicate}) AS sampled"


def _containment_direction(
    audited: AuditedSourceConnection,
    *,
    source_a: str,
    column_a: str,
    source_b: str,
    column_b: str,
    view_label: str,
    timeout_seconds: int,
    costs: list[RelationshipCost],
) -> tuple[float, int] | None:
    """Fraction of A's distinct sampled values present in B's distinct
    sampled values, plus A's orphan count. `None` on timeout.

    A `LEFT JOIN` against B's distinct values, not `COUNT(DISTINCT CASE WHEN
    x IN (subquery) THEN x END)` -- the latter was tried first and rejected
    live by SQL Server (error 130, "aggregate function on an expression
    containing ... a subquery"); found profiling fqb.Diagnosis, no mocked
    fixture caught it."""
    sql = (
        f"SELECT COUNT(DISTINCT a.a_col), "
        f"COUNT(DISTINCT CASE WHEN b.b_col IS NOT NULL THEN a.a_col END) "
        f"FROM (SELECT DISTINCT [{column_a}] AS a_col FROM {source_a} "
        f"WHERE [{column_a}] IS NOT NULL) AS a "
        f"LEFT JOIN (SELECT DISTINCT [{column_b}] AS b_col FROM {source_b} "
        f"WHERE [{column_b}] IS NOT NULL) AS b ON a.a_col = b.b_col"
    )
    began = time.perf_counter()
    try:
        (row,) = audited.execute_query(sql, timeout_seconds=timeout_seconds)
    except Exception as exc:
        if not _is_timeout_error(exc):
            raise
        costs.append(
            RelationshipCost(
                view=view_label,
                operation="containment",
                duration_ms=round((time.perf_counter() - began) * 1000, 3),
                status="timeout",
            )
        )
        return None
    costs.append(
        RelationshipCost(
            view=view_label,
            operation="containment",
            duration_ms=round((time.perf_counter() - began) * 1000, 3),
            status="ok",
        )
    )
    total_distinct, matched_distinct = int(row[0]), int(row[1])
    if total_distinct == 0:
        return 0.0, 0
    containment = matched_distinct / total_distinct
    orphans = total_distinct - matched_distinct
    return containment, orphans


def detect_relationships(
    audited: AuditedSourceConnection,
    *,
    views: list[ViewContext],
    max_pairs_evaluated: int = DEFAULT_MAX_PAIRS_EVALUATED,
    per_pair_timeout_seconds: int = DEFAULT_PER_PAIR_TIMEOUT_SECONDS,
    already_evaluated: frozenset[tuple[str, str, str, str]] = frozenset(),
) -> tuple[list[RelationshipEdge], PruningReport, list[RelationshipCost]]:
    """D-021's cost-budgeted pair/containment step. `already_evaluated` is
    the resumable-checkpoint hook (e): pair keys already scored by an
    interrupted prior run are skipped, not re-evaluated."""
    views_by_name = {view.qualified_name: view for view in views}
    considered = generate_pair_candidates(views)
    eligible, pruned = prune_pair_candidates(considered, views_by_name)

    edges: list[RelationshipEdge] = []
    costs: list[RelationshipCost] = []
    evaluated_count = 0
    skipped_cost_count = 0

    for candidate in eligible:
        if _pair_key(candidate) in already_evaluated:
            continue
        if evaluated_count + skipped_cost_count >= max_pairs_evaluated:
            skipped_cost_count += 1
            edges.append(
                RelationshipEdge(
                    from_view=candidate.from_view,
                    from_column=candidate.from_column,
                    to_view=candidate.to_view,
                    to_column=candidate.to_column,
                    status="skipped_cost",
                    containment_a_to_b=None,
                    containment_b_to_a=None,
                    orphan_count_a=None,
                    orphan_count_b=None,
                )
            )
            continue

        view_a = views_by_name[candidate.from_view]
        view_b = views_by_name[candidate.to_view]
        source_a = _sampled_source(view_a)
        source_b = _sampled_source(view_b)
        view_label = f"{candidate.from_view}~{candidate.to_view}"

        a_to_b = _containment_direction(
            audited,
            source_a=source_a,
            column_a=candidate.from_column,
            source_b=source_b,
            column_b=candidate.to_column,
            view_label=view_label,
            timeout_seconds=per_pair_timeout_seconds,
            costs=costs,
        )
        b_to_a = _containment_direction(
            audited,
            source_a=source_b,
            column_a=candidate.to_column,
            source_b=source_a,
            column_b=candidate.from_column,
            view_label=view_label,
            timeout_seconds=per_pair_timeout_seconds,
            costs=costs,
        )

        if a_to_b is None and b_to_a is None:
            skipped_cost_count += 1
            edges.append(
                RelationshipEdge(
                    from_view=candidate.from_view,
                    from_column=candidate.from_column,
                    to_view=candidate.to_view,
                    to_column=candidate.to_column,
                    status="skipped_cost",
                    containment_a_to_b=None,
                    containment_b_to_a=None,
                    orphan_count_a=None,
                    orphan_count_b=None,
                )
            )
            continue

        evaluated_count += 1
        edges.append(
            RelationshipEdge(
                from_view=candidate.from_view,
                from_column=candidate.from_column,
                to_view=candidate.to_view,
                to_column=candidate.to_column,
                status="evaluated",
                containment_a_to_b=a_to_b[0] if a_to_b else None,
                containment_b_to_a=b_to_a[0] if b_to_a else None,
                orphan_count_a=a_to_b[1] if a_to_b else None,
                orphan_count_b=b_to_a[1] if b_to_a else None,
            )
        )

    report = PruningReport(
        pairs_considered=len(considered),
        pairs_pruned=pruned,
        pairs_evaluated=evaluated_count,
        pairs_skipped_cost=skipped_cost_count,
    )
    return edges, report, costs
