"""Phase 1 step 6: watermark verification.

Confirms each view's Phase-0-hypothesized watermark candidate columns
(`InsertedAt`/`UpdatedAt`, `cdss.rowstats.WATERMARK_CANDIDATE_COLUMNS`) live:
correct date/time SQL type, zero NULLs, and a monotonicity spot-check
against the Phase 0 baseline MAX (`env-report.json`) -- a live MAX that fell
*below* the recorded baseline would mean the column moved backward in time
since Phase 0, unsafe for incremental "WHERE col > last_watermark"
extraction. Also flags an implausibly far-future MAX (placeholder/sentinel
data, not a genuine recent event) as disqualifying rather than trusting it.

Classifies each view `watermarkable` (>=1 candidate column passes every
check) or `fallback_needed`. Per-column diagnostic detail (null rate,
monotonicity result, failure reasons) lives on `WatermarkColumnCheck` for the
human profiling report (step 8); the catalog schema itself (fixed at step 1)
only carries `status` + the list of passing column names per view.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from cdss.profiler import _is_timeout_error
from cdss.rowstats import WATERMARK_CANDIDATE_COLUMNS
from cdss.source import AuditedSourceConnection

WATERMARK_DATE_TYPES: frozenset[str] = frozenset(
    {"datetime", "datetime2", "smalldatetime", "date", "datetimeoffset"}
)

# A watermark MAX this far beyond "now" is a placeholder/sentinel, not a
# genuine recent event (found live: dbo.PatientAlerts' InsertedAt/UpdatedAt
# MAX is 2061-09-21) -- disqualifying, never silently trusted.
FUTURE_DATE_ANOMALY_THRESHOLD_DAYS = 1

WatermarkStatus = Literal["watermarkable", "fallback_needed"]
WatermarkCostStatus = Literal["ok", "timeout"]


@dataclass(frozen=True)
class WatermarkColumnCheck:
    column_name: str
    data_type: str
    null_count: int
    total_count: int
    null_rate: float
    min_value: str | None
    max_value: str | None
    monotonic_vs_baseline: bool | None
    passed: bool
    reasons: list[str]


@dataclass(frozen=True)
class ViewWatermarkClassification:
    qualified_name: str
    status: WatermarkStatus
    columns: list[str]
    checks: list[WatermarkColumnCheck]


@dataclass(frozen=True)
class WatermarkCost:
    view: str
    operation: Literal["watermark_check"]
    duration_ms: float
    status: WatermarkCostStatus


def _is_watermark_candidate(column_name: str, data_type: str) -> bool:
    return (
        column_name.lower() in {c.lower() for c in WATERMARK_CANDIDATE_COLUMNS}
        and data_type.lower() in WATERMARK_DATE_TYPES
    )


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def classify_view_watermarks(
    audited: AuditedSourceConnection,
    *,
    qualified_name: str,
    columns: list[tuple[str, str]],
    baseline_max_by_column: dict[str, str] | None = None,
    now: datetime | None = None,
    timeout_seconds: int = 30,
) -> tuple[ViewWatermarkClassification, list[WatermarkCost]]:
    """`columns` is `(name, data_type)` per column, e.g. from
    `profiler.fetch_columns()`. `baseline_max_by_column` is the Phase 0
    `env-report.json` MAX value per watermark column name, if known --
    absent entirely for a view with no Phase 0 watermark candidate."""
    name_matched = [
        (name, data_type)
        for name, data_type in columns
        if name.lower() in {c.lower() for c in WATERMARK_CANDIDATE_COLUMNS}
    ]
    eligible = [
        (name, data_type)
        for name, data_type in name_matched
        if data_type.lower() in WATERMARK_DATE_TYPES
    ]
    mismatched_checks = [
        WatermarkColumnCheck(
            column_name=name,
            data_type=data_type,
            null_count=0,
            total_count=0,
            null_rate=0.0,
            min_value=None,
            max_value=None,
            monotonic_vs_baseline=None,
            passed=False,
            reasons=["wrong_type"],
        )
        for name, data_type in name_matched
        if data_type.lower() not in WATERMARK_DATE_TYPES
    ]

    if not eligible:
        return (
            ViewWatermarkClassification(
                qualified_name=qualified_name,
                status="fallback_needed",
                columns=[],
                checks=mismatched_checks,
            ),
            [],
        )

    baseline_max_by_column = baseline_max_by_column or {}
    now = now or datetime.now()
    select_parts = ["COUNT(*)"]
    for name, _data_type in eligible:
        select_parts.append(f"COUNT(*) - COUNT([{name}])")
        select_parts.append(f"MIN([{name}])")
        select_parts.append(f"MAX([{name}])")
    sql = f"SELECT {', '.join(select_parts)} FROM {qualified_name}"

    began = time.perf_counter()
    try:
        (row,) = audited.execute_query(sql, timeout_seconds=timeout_seconds)
    except Exception as exc:
        if not _is_timeout_error(exc):
            raise
        duration_ms = (time.perf_counter() - began) * 1000
        return (
            ViewWatermarkClassification(
                qualified_name=qualified_name,
                status="fallback_needed",
                columns=[],
                checks=mismatched_checks,
            ),
            [
                WatermarkCost(
                    view=qualified_name,
                    operation="watermark_check",
                    duration_ms=round(duration_ms, 3),
                    status="timeout",
                )
            ],
        )
    duration_ms = (time.perf_counter() - began) * 1000

    total_count = int(row[0])
    checks: list[WatermarkColumnCheck] = list(mismatched_checks)
    passing_columns: list[str] = []
    idx = 1
    for name, data_type in eligible:
        null_count, min_value, max_value = row[idx], row[idx + 1], row[idx + 2]
        idx += 3
        null_count = int(null_count)
        null_rate = null_count / total_count if total_count else 0.0
        min_str = None if min_value is None else str(min_value)
        max_str = None if max_value is None else str(max_value)

        reasons: list[str] = []
        if null_count > 0:
            reasons.append("null_values_present")

        monotonic: bool | None = None
        baseline_str = baseline_max_by_column.get(name)
        if baseline_str is not None and max_str is not None:
            baseline_dt = _parse_datetime(baseline_str)
            live_dt = _parse_datetime(max_str)
            if baseline_dt is not None and live_dt is not None:
                monotonic = live_dt >= baseline_dt
                if not monotonic:
                    reasons.append("non_monotonic_vs_baseline")

        live_dt_for_future_check = _parse_datetime(max_str)
        if live_dt_for_future_check is not None and live_dt_for_future_check > now + timedelta(
            days=FUTURE_DATE_ANOMALY_THRESHOLD_DAYS
        ):
            reasons.append("future_dated_max")

        passed = not reasons
        checks.append(
            WatermarkColumnCheck(
                column_name=name,
                data_type=data_type,
                null_count=null_count,
                total_count=total_count,
                null_rate=null_rate,
                min_value=min_str,
                max_value=max_str,
                monotonic_vs_baseline=monotonic,
                passed=passed,
                reasons=reasons,
            )
        )
        if passed:
            passing_columns.append(name)

    status: WatermarkStatus = "watermarkable" if passing_columns else "fallback_needed"
    return (
        ViewWatermarkClassification(
            qualified_name=qualified_name,
            status=status,
            columns=passing_columns,
            checks=checks,
        ),
        [
            WatermarkCost(
                view=qualified_name,
                operation="watermark_check",
                duration_ms=round(duration_ms, 3),
                status="ok",
            )
        ],
    )
