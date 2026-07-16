"""Phase 1 step 8: per-view profiling checkpoint.

Lets a crashed mid-run resume without re-querying a view an earlier attempt
already finished, with no manual steps. Each successfully profiled view's
catalog-shaped dict (the exact `viewProfile` shape from
`schemas/semantic-catalog.schema.json`) is persisted after it completes;
`column_profile_from_dict()`/`view_context_from_view_dict()` reconstruct the
`ColumnProfile`/`relationships.ViewContext` objects step 4's pair analysis
needs from that same dict, so a resumed run never re-issues the (expensive)
per-column profiling queries for a view already checkpointed.

Scoping note: the cross-view relationship-analysis phase (step 4) is *not*
itself incrementally checkpointed within a single call to
`relationships.detect_relationships()` -- doing so would mean modifying that
already-approved module to persist partial edges mid-loop, out of scope
here. It only runs after every view is checkpointed and is fast (proven live
at step 4: tens of ms to ~1.3s per pair, capped at 50 pairs), so this is a
low-cost, explicitly flagged gap rather than a silent one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cdss.profiler import (
    ColumnProfile,
    ColumnSampling,
    ReferenceSamples,
    StringLengthStats,
    TopValue,
    ValuePatternStats,
)
from cdss.relationships import ViewContext

_EMPTY_CHECKPOINT: dict[str, Any] = {"views": {}, "already_evaluated_pairs": []}


def load_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return dict(_EMPTY_CHECKPOINT, views={}, already_evaluated_pairs=[])
    result: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return result


def save_checkpoint(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def clear_checkpoint(path: Path) -> None:
    if path.exists():
        path.unlink()


def column_profile_from_dict(data: dict[str, Any]) -> ColumnProfile:
    """Reconstructs a `ColumnProfile` from its catalog-shaped dict -- the
    same shape `semantic-catalog.schema.json`'s `columnProfile` def
    requires, so this round-trips whatever a prior run already wrote to the
    checkpoint (or the final catalog)."""
    sampling = data["sampling"]
    string_length_stats = data["string_length_stats"]
    reference_samples = data["reference_samples"]
    value_pattern_stats = data["value_pattern_stats"]
    return ColumnProfile(
        column_name=data["column_name"],
        data_type=data["data_type"],
        is_free_text=data["is_free_text"],
        column_class=data["column_class"],
        sampling=ColumnSampling(sampled=sampling["sampled"], method=sampling["method"]),
        null_count=data["null_count"],
        null_rate=data["null_rate"],
        distinct_count=data["distinct_count"],
        min_value=data["min_value"],
        max_value=data["max_value"],
        top_values=[
            TopValue(value=t["value"], frequency=t["frequency"]) for t in data["top_values"]
        ],
        string_length_stats=(
            StringLengthStats(**string_length_stats) if string_length_stats is not None else None
        ),
        reference_samples=(
            ReferenceSamples(values=reference_samples["values"])
            if reference_samples is not None
            else None
        ),
        value_pattern_stats=(
            ValuePatternStats(trailing_tag_counts=value_pattern_stats["trailing_tag_counts"])
            if value_pattern_stats is not None
            else None
        ),
    )


def view_context_from_view_dict(view_dict: dict[str, Any]) -> ViewContext:
    """Reconstructs a `relationships.ViewContext` (needed for step 4's pair
    analysis) from a checkpointed view dict. `raw_columns`' `char_max_length`
    is always `None` here -- the catalog schema doesn't carry it, and
    `relationships.py` never reads it (only `_sampled_source()`'s anchor/
    numeric-ID selection does, which only needs name + data_type)."""
    profiles = [column_profile_from_dict(c) for c in view_dict["columns"]]
    raw_columns: list[tuple[str, str, int | None]] = [
        (p.column_name, p.data_type, None) for p in profiles
    ]
    return ViewContext(
        qualified_name=view_dict["qualified_name"],
        row_count=view_dict["row_count"] or 0,
        raw_columns=raw_columns,
        profiles=profiles,
    )
