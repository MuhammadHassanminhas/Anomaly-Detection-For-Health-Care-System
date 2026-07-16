"""Phase 1 step 3: candidate-key detection.

A column is a candidate key when `COUNT(DISTINCT col) == COUNT(*)` over the
same population `determine_sampling()` (D-018) would choose for this view --
a full scan on small views (`evidence_method: "exact"`), the same
WHERE-filtered sample used for profiling on large ones
(`evidence_method: "sampled"`, never presented as certain). `*ID`-style
naming only orders which columns are checked first (a cost-priority hint,
cheapest to confirm/reject first on a wide view) -- it is never itself
evidence for the judgment; the count comparison is.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from cdss.profiler import (
    DEFAULT_SAMPLE_FRACTION,
    LARGE_VIEW_ROW_THRESHOLD,
    _is_key_column,
    _is_timeout_error,
    determine_sampling,
)
from cdss.source import AuditedSourceConnection

DEFAULT_CANDIDATE_KEY_BATCH_SIZE = 20

EvidenceMethod = Literal["exact", "sampled"]
CandidateKeyCostStatus = Literal["ok", "timeout"]


@dataclass(frozen=True)
class CandidateKey:
    columns: list[str]
    distinct_count: int
    row_count: int
    evidence_method: EvidenceMethod


@dataclass(frozen=True)
class CandidateKeyCost:
    view: str
    operation: Literal["candidate_key"]
    duration_ms: float
    status: CandidateKeyCostStatus


def _order_columns_for_checking(
    columns: list[tuple[str, str, int | None]],
) -> list[tuple[str, str, int | None]]:
    """`*ID`-named numeric columns first -- a cost-priority hint only, never
    evidence for the candidate-key judgment itself."""

    def sort_key(column: tuple[str, str, int | None]) -> tuple[int, str]:
        name, data_type, _char_len = column
        return (0 if _is_key_column(name, data_type) else 1, name)

    return sorted(columns, key=sort_key)


def detect_candidate_keys(
    audited: AuditedSourceConnection,
    *,
    qualified_name: str,
    row_count: int,
    columns: list[tuple[str, str, int | None]],
    watermark_column: tuple[str, str, str] | None = None,
    batch_size: int = DEFAULT_CANDIDATE_KEY_BATCH_SIZE,
    large_view_threshold: int = LARGE_VIEW_ROW_THRESHOLD,
    sample_fraction: float = DEFAULT_SAMPLE_FRACTION,
    timeout_seconds: int = 30,
) -> tuple[list[CandidateKey], list[CandidateKeyCost]]:
    """Single-column candidate keys for one view. Column-*pair* candidates
    (spec: "columns/column-pairs") are deferred -- combinatorial pair
    generation on a wide view needs its own cost-budget design, the same
    concern step 4 (join/containment) already names explicitly; not silently
    folded into this step's single-column deliverable."""
    numeric_id_columns = [
        name for name, data_type, _char_len in columns if _is_key_column(name, data_type)
    ]
    anchor_column = columns[0][0] if columns else None
    sampled, _method, predicate = determine_sampling(
        row_count=row_count,
        numeric_id_columns=numeric_id_columns,
        watermark_column=watermark_column,
        anchor_column=anchor_column,
        large_view_threshold=large_view_threshold,
        sample_fraction=sample_fraction,
    )
    source = (
        f"(SELECT * FROM {qualified_name} WHERE {predicate}) AS sampled"
        if predicate is not None
        else qualified_name
    )
    evidence_method: EvidenceMethod = "sampled" if sampled else "exact"

    ordered = _order_columns_for_checking(columns)
    candidates: list[CandidateKey] = []
    costs: list[CandidateKeyCost] = []

    for start in range(0, len(ordered), batch_size):
        batch = ordered[start : start + batch_size]
        select_parts = ["COUNT(*)"] + [f"COUNT(DISTINCT [{name}])" for name, _dt, _cl in batch]
        sql = f"SELECT {', '.join(select_parts)} FROM {source}"

        began = time.perf_counter()
        try:
            (row,) = audited.execute_query(sql, timeout_seconds=timeout_seconds)
        except Exception as exc:
            if not _is_timeout_error(exc):
                raise
            duration_ms = (time.perf_counter() - began) * 1000
            costs.append(
                CandidateKeyCost(
                    view=qualified_name,
                    operation="candidate_key",
                    duration_ms=round(duration_ms, 3),
                    status="timeout",
                )
            )
            continue

        duration_ms = (time.perf_counter() - began) * 1000
        costs.append(
            CandidateKeyCost(
                view=qualified_name,
                operation="candidate_key",
                duration_ms=round(duration_ms, 3),
                status="ok",
            )
        )

        population = int(row[0])
        for (name, _dt, _cl), distinct in zip(batch, row[1:], strict=True):
            distinct_count = int(distinct)
            if population > 0 and distinct_count == population:
                candidates.append(
                    CandidateKey(
                        columns=[name],
                        distinct_count=distinct_count,
                        row_count=population,
                        evidence_method=evidence_method,
                    )
                )
    return candidates, costs
