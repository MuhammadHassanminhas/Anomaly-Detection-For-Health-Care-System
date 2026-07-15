"""Phase 0 step 7: row counts + watermark candidates for in-scope objects.

Exact COUNT(*) per object with a timeout. On timeout: base tables fall back
to an approximate count from sys.partitions, explicitly marked approximate
(F10); views have no such physical-storage fallback, so a timed-out view
count is marked indeterminate rather than fabricated (F6 three-valued
evaluation — missing/uncomputable data is indeterminate, never a guess).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from cdss.source import AuditedSourceConnection
from cdss.surface import ObjectType, SurfaceObject

RowCountStatus = Literal["exact", "approximate", "indeterminate"]

WATERMARK_CANDIDATE_COLUMNS: tuple[str, ...] = ("InsertedAt", "UpdatedAt")

COLUMNS_QUERY = (
    "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS"
)

_PARTITION_STAT_QUERY = (
    "SELECT SUM(p.rows) "
    "FROM sys.partitions p "
    "JOIN sys.objects o ON p.object_id = o.object_id "
    "JOIN sys.schemas s ON o.schema_id = s.schema_id "
    "WHERE s.name = '{schema}' AND o.name = '{name}' AND p.index_id IN (0, 1)"
)


def _is_timeout_error(exc: Exception) -> bool:
    message = str(exc)
    return "HYT00" in message or "timeout" in message.lower()


@dataclass(frozen=True)
class WatermarkColumn:
    column_name: str
    data_type: str
    min_value: str | None
    max_value: str | None


@dataclass(frozen=True)
class ObjectRowStats:
    qualified_name: str
    object_type: ObjectType
    row_count: int | None
    row_count_status: RowCountStatus
    duration_ms: float
    watermark_columns: list[WatermarkColumn]


def _fetch_columns_by_object(
    audited: AuditedSourceConnection,
) -> dict[tuple[str, str], list[tuple[str, str]]]:
    by_object: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for schema, name, column_name, data_type in audited.execute_query(COLUMNS_QUERY):
        by_object.setdefault((str(schema), str(name)), []).append(
            (str(column_name), str(data_type))
        )
    return by_object


def _count_rows(
    audited: AuditedSourceConnection,
    obj: SurfaceObject,
    *,
    timeout_seconds: int,
) -> tuple[int | None, RowCountStatus]:
    try:
        (count,) = audited.execute_query(
            f"SELECT COUNT(*) FROM {obj.qualified_name}", timeout_seconds=timeout_seconds
        )[0]
        return int(count), "exact"
    except Exception as exc:
        if not _is_timeout_error(exc):
            raise
        if obj.object_type != "table":
            return None, "indeterminate"
        (approx,) = audited.execute_query(
            _PARTITION_STAT_QUERY.format(schema=obj.schema, name=obj.name),
            timeout_seconds=timeout_seconds,
        )[0]
        return (int(approx) if approx is not None else None), "approximate"


def _fetch_watermark_columns(
    audited: AuditedSourceConnection,
    obj: SurfaceObject,
    candidate_columns: list[tuple[str, str]],
    *,
    timeout_seconds: int,
) -> list[WatermarkColumn]:
    present = [
        (name, data_type)
        for name, data_type in candidate_columns
        if name.lower() in {c.lower() for c in WATERMARK_CANDIDATE_COLUMNS}
    ]
    if not present:
        return []

    select_list = ", ".join(f"MIN([{name}]), MAX([{name}])" for name, _ in present)
    row = audited.execute_query(
        f"SELECT {select_list} FROM {obj.qualified_name}", timeout_seconds=timeout_seconds
    )[0]

    columns = []
    for index, (name, data_type) in enumerate(present):
        min_value, max_value = row[index * 2], row[index * 2 + 1]
        columns.append(
            WatermarkColumn(
                column_name=name,
                data_type=data_type,
                min_value=None if min_value is None else str(min_value),
                max_value=None if max_value is None else str(max_value),
            )
        )
    return columns


def compute_row_stats(
    audited: AuditedSourceConnection,
    objects: list[SurfaceObject],
    *,
    timeout_seconds: int,
) -> list[ObjectRowStats]:
    columns_by_object = _fetch_columns_by_object(audited)

    stats = []
    for obj in objects:
        start = time.perf_counter()
        row_count, status = _count_rows(audited, obj, timeout_seconds=timeout_seconds)
        candidate_columns = columns_by_object.get((obj.schema, obj.name), [])
        watermark_columns = _fetch_watermark_columns(
            audited, obj, candidate_columns, timeout_seconds=timeout_seconds
        )
        duration_ms = (time.perf_counter() - start) * 1000
        stats.append(
            ObjectRowStats(
                qualified_name=obj.qualified_name,
                object_type=obj.object_type,
                row_count=row_count,
                row_count_status=status,
                duration_ms=round(duration_ms, 3),
                watermark_columns=watermark_columns,
            )
        )
    return stats
