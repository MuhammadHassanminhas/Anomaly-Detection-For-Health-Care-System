"""D-023: table-archetype classification (reference/dictionary vs.
fact/event) and reference-view capture policy.

Runs after column-level profiling (D-020/D-022), using the same D-018
sampled population -- no fresh full scan. A view detected `"reference"` has
its identified descriptive column re-captured under a different policy
(bounded deterministic sample + coarse tag stats, `apply_reference_capture`)
instead of D-022's categorical top-K-with-floor, which is the wrong
instrument for a table where distinct == row count *by design* (each row is
a vocabulary entry, not an individual's record).

All four `"reference"` signals must hold; any signal that can't be verified
defaults to `"fact"` -- never guessed toward `"reference"`.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from cdss.profiler import (
    DEFAULT_SAMPLE_FRACTION,
    LARGE_VIEW_ROW_THRESHOLD,
    STRING_TYPES,
    ColumnProfile,
    ReferenceSamples,
    ValuePatternStats,
    _is_free_text,
    _is_key_column,
    _is_timeout_error,
    _matches_identifier_pattern,
    determine_sampling,
)
from cdss.source import AuditedSourceConnection

ViewArchetype = Literal["reference", "fact"]
ArchetypeCostStatus = Literal["ok", "timeout"]

REFERENCE_NAME_UNIQUENESS_RATIO = 0.95
MAX_REFERENCE_SAMPLES = 100
MAX_TRAILING_TAGS = 20

# D-023: presence of any of these disqualifies "reference" outright --
# verified explicitly against column names, never assumed.
PATIENT_LINKAGE_PATTERNS: frozenset[str] = frozenset(
    {
        "patientid",
        "appointmentid",
        "encounterid",
        "visitid",
        "episodeid",
        "consultid",
        "consultationid",
    }
)
# D-023: date/datetime columns matching these are audit timestamps, not
# clinical-event dates -- allowed on a reference view.
AUDIT_TIMESTAMP_PATTERNS: frozenset[str] = frozenset(
    {"insertedat", "updatedat", "createdat", "modifiedat", "createddate", "modifieddate"}
)
DATE_TYPES: frozenset[str] = frozenset({"date", "datetime", "datetime2", "smalldatetime"})


@dataclass(frozen=True)
class ArchetypeSignals:
    has_key_column: bool
    name_column: str | None
    name_column_uniqueness: float | None
    has_patient_linkage: bool
    has_clinical_event_date: bool


@dataclass(frozen=True)
class ArchetypeResult:
    archetype: ViewArchetype
    signals: ArchetypeSignals
    reason: str


@dataclass(frozen=True)
class ArchetypeCost:
    view: str
    operation: Literal["archetype_detection", "reference_capture"]
    duration_ms: float
    status: ArchetypeCostStatus


def _has_patient_linkage(columns: list[tuple[str, str, int | None]]) -> bool:
    return any(
        any(pattern in name.lower() for pattern in PATIENT_LINKAGE_PATTERNS)
        for name, _dt, _cl in columns
    )


def _has_clinical_event_date(columns: list[tuple[str, str, int | None]]) -> bool:
    for name, data_type, _char_len in columns:
        if data_type.lower() not in DATE_TYPES:
            continue
        if any(pattern in name.lower() for pattern in AUDIT_TIMESTAMP_PATTERNS):
            continue
        return True
    return False


def _name_column_candidates(
    columns: list[tuple[str, str, int | None]],
) -> list[tuple[str, str, int | None]]:
    """Non-key, non-identifier-matched, non-free-text string columns -- the
    only pool eligible for the "near-unique descriptive column" signal.
    Excluding identifier-matched columns is a safety property, not an
    optimization: a `FirstName` column with near-unique values on a patient
    table must never itself trigger `"reference"` for that view."""
    candidates = []
    for name, data_type, char_len in columns:
        if _is_key_column(name, data_type):
            continue
        if _matches_identifier_pattern(name):
            continue
        if data_type.lower() not in STRING_TYPES:
            continue
        if _is_free_text(data_type, char_len):
            continue
        candidates.append((name, data_type, char_len))
    return candidates


def _run_query(
    audited: AuditedSourceConnection,
    sql: str,
    *,
    qualified_name: str,
    operation: Literal["archetype_detection", "reference_capture"],
    timeout_seconds: int,
    costs: list[ArchetypeCost],
) -> Sequence[tuple[Any, ...]] | None:
    began = time.perf_counter()
    try:
        rows = audited.execute_query(sql, timeout_seconds=timeout_seconds)
    except Exception as exc:
        if not _is_timeout_error(exc):
            raise
        costs.append(
            ArchetypeCost(
                view=qualified_name,
                operation=operation,
                duration_ms=round((time.perf_counter() - began) * 1000, 3),
                status="timeout",
            )
        )
        return None
    costs.append(
        ArchetypeCost(
            view=qualified_name,
            operation=operation,
            duration_ms=round((time.perf_counter() - began) * 1000, 3),
            status="ok",
        )
    )
    return rows


def detect_view_archetype(
    audited: AuditedSourceConnection,
    *,
    qualified_name: str,
    row_count: int,
    columns: list[tuple[str, str, int | None]],
    watermark_column: tuple[str, str, str] | None = None,
    uniqueness_ratio_threshold: float = REFERENCE_NAME_UNIQUENESS_RATIO,
    large_view_threshold: int = LARGE_VIEW_ROW_THRESHOLD,
    sample_fraction: float = DEFAULT_SAMPLE_FRACTION,
    timeout_seconds: int = 30,
) -> tuple[ArchetypeResult, list[ArchetypeCost]]:
    has_key = any(_is_key_column(name, dt) for name, dt, _cl in columns)
    has_linkage = _has_patient_linkage(columns)
    has_event_date = _has_clinical_event_date(columns)

    candidates = _name_column_candidates(columns)
    costs: list[ArchetypeCost] = []
    name_column: str | None = None
    name_uniqueness: float | None = None

    if candidates:
        numeric_id_columns = [
            name for name, data_type, _char_len in columns if _is_key_column(name, data_type)
        ]
        anchor_column = columns[0][0] if columns else None
        _sampled, _method, predicate = determine_sampling(
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
        select_parts = ["COUNT(*)"] + [f"COUNT(DISTINCT [{name}])" for name, _dt, _cl in candidates]
        sql = f"SELECT {', '.join(select_parts)} FROM {source}"

        rows = _run_query(
            audited,
            sql,
            qualified_name=qualified_name,
            operation="archetype_detection",
            timeout_seconds=timeout_seconds,
            costs=costs,
        )
        if rows is not None:
            (row,) = rows
            population = int(row[0])
            if population > 0:
                for (name, _dt, _cl), distinct in zip(candidates, row[1:], strict=True):
                    ratio = int(distinct) / population
                    if ratio >= uniqueness_ratio_threshold and (
                        name_uniqueness is None or ratio > name_uniqueness
                    ):
                        name_column = name
                        name_uniqueness = ratio

    signals = ArchetypeSignals(
        has_key_column=has_key,
        name_column=name_column,
        name_column_uniqueness=name_uniqueness,
        has_patient_linkage=has_linkage,
        has_clinical_event_date=has_event_date,
    )

    if has_key and name_column is not None and not has_linkage and not has_event_date:
        assert name_uniqueness is not None
        return (
            ArchetypeResult(
                archetype="reference",
                signals=signals,
                reason=(
                    f"key column present, '{name_column}' near-unique "
                    f"({name_uniqueness:.1%} of sampled rows), no patient/encounter "
                    f"linkage, no non-audit clinical-event date"
                ),
            ),
            costs,
        )

    reasons = []
    if not has_key:
        reasons.append("no key column")
    if name_column is None:
        reasons.append("no near-unique non-identifier-matched descriptive column")
    if has_linkage:
        reasons.append("patient/encounter linkage column present")
    if has_event_date:
        reasons.append("non-audit clinical-event date column present")
    return (
        ArchetypeResult(archetype="fact", signals=signals, reason="; ".join(reasons)),
        costs,
    )


def capture_reference_vocabulary(
    audited: AuditedSourceConnection,
    *,
    qualified_name: str,
    name_column: str,
    key_column: str,
    max_samples: int = MAX_REFERENCE_SAMPLES,
    max_tags: int = MAX_TRAILING_TAGS,
    timeout_seconds: int = 30,
) -> tuple[ReferenceSamples, ValuePatternStats, list[ArchetypeCost]]:
    """D-023: bounded, deterministic (key-ordered, not random) sample plus
    coarse trailing-tag stats for a reference view's vocabulary column.
    Never the full vocabulary -- that stays in the source DB, queried live
    via the audited layer whenever a check actually needs it (D-023's
    Phase 4 forward note)."""
    costs: list[ArchetypeCost] = []

    sample_sql = (
        f"SELECT TOP ({max_samples}) [{name_column}] FROM {qualified_name} "
        f"WHERE [{name_column}] IS NOT NULL ORDER BY [{key_column}]"
    )
    sample_rows = _run_query(
        audited,
        sample_sql,
        qualified_name=qualified_name,
        operation="reference_capture",
        timeout_seconds=timeout_seconds,
        costs=costs,
    )
    samples = ReferenceSamples(
        values=[str(value) for (value,) in sample_rows] if sample_rows is not None else []
    )

    tag_sql = (
        f"SELECT TOP ({max_tags}) tag, COUNT(*) FROM ("
        f"SELECT CASE WHEN [{name_column}] LIKE '%(%)' "
        f"THEN RIGHT([{name_column}], CHARINDEX('(', REVERSE([{name_column}])) + 1) "
        f"ELSE NULL END AS tag "
        f"FROM {qualified_name}"
        f") AS tagged WHERE tag IS NOT NULL GROUP BY tag ORDER BY COUNT(*) DESC"
    )
    tag_rows = _run_query(
        audited,
        tag_sql,
        qualified_name=qualified_name,
        operation="reference_capture",
        timeout_seconds=timeout_seconds,
        costs=costs,
    )
    tag_stats = ValuePatternStats(
        trailing_tag_counts=(
            {str(tag): int(count) for tag, count in tag_rows} if tag_rows is not None else {}
        )
    )
    return samples, tag_stats, costs


def apply_reference_capture(
    profile: ColumnProfile, samples: ReferenceSamples, tag_stats: ValuePatternStats
) -> ColumnProfile:
    """Overrides a column profile with D-023's reference-vocabulary capture
    -- clears the frequency-ranked top_values/min/max (wrong semantics for a
    bounded deterministic sample) in favor of reference_samples/
    value_pattern_stats."""
    return replace(
        profile,
        column_class="reference_vocabulary",
        min_value=None,
        max_value=None,
        top_values=[],
        reference_samples=samples,
        value_pattern_stats=tag_stats,
    )
