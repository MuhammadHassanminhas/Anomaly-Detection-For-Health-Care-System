"""Phase 1 step 2: per-column profiling (SQL type, null rate, distinct count,
typed min/max, top-K frequent values, string length stats) for one view.

All reads are set-based SQL through the audited connection: one batched
aggregate statement per group of columns (D-018's
`DEFAULT_AGGREGATE_BATCH_SIZE`, not one query per column), plus one grouped
top-K or sentinel-check query per column depending on its class. Large views
are profiled on a deterministic sample -- a `WHERE`-filter derived table,
never `TABLESAMPLE` (rejected by SQL Server on views, error 494) or a bare
`ORDER BY`/`TOP-N` scan (forces a full sort) -- per D-018's three-tier
strategy: modulo on a numeric `*ID` column, else a watermark-range cutoff,
else `CHECKSUM(NEWID(), col)`. Every sampled statistic carries its sampling
method on the column itself (`ColumnSampling`, D-018), never presented as
exact. On a per-batch timeout, the batch's columns are left indeterminate
(F6) rather than guessed, and the cost entry is marked `timeout` (F10).

D-020 (profiling-stage PHI policy): every column is classified into one of
four capture tiers *before* any value-bearing query is built, from its name
and SQL type alone -- `classify_column()` finalizes the tier once
`distinct_count` is known. Real values (`MIN`/`MAX`, top-K) are only ever
issued to the database for columns confirmed low-cardinality-coded or a
detected ID sentinel; for identifier-name-matched, free-text, and
high-cardinality-unclassified string columns, the value-bearing SQL is never
built at all, not fetched then discarded -- see `_capture_range()`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from cdss.source import AuditedSourceConnection

DEFAULT_TOP_K = 20
DEFAULT_AGGREGATE_BATCH_SIZE = 20  # D-018: wide-view aggregate fan-out timed out unbatched.
LARGE_VIEW_ROW_THRESHOLD = 100_000
DEFAULT_SAMPLE_FRACTION = 0.10
FREE_TEXT_LENGTH_THRESHOLD = 400

# D-020: a column with <= this many distinct values is coded/categorical
# (status, type, code, flag, unit, ...) -- real values are not identifying at
# this cardinality, so this is also the top-K query's firing threshold.
CATEGORICAL_CARDINALITY_CEILING = 200
# D-020: a key column's single most-frequent value is only ever recorded when
# it covers this share of rows or more -- by definition a placeholder/
# sentinel, never an individual record.
SENTINEL_FREQUENCY_THRESHOLD = 0.05

# D-020/D-022: case-insensitive substring match against the column name.
# Matching forces Class A (identifier_or_freetext) regardless of SQL type or
# measured cardinality -- e.g. a `DOB` (date) column is Class A, never
# `measure`, because this check runs before any type- or cardinality-based
# branching. D-022 narrowed the original bare `"name"` pattern to specific
# person-name compounds only -- it was catching clinical-vocabulary columns
# (e.g. `DiseaseName`) that aren't person-identifying.
IDENTIFIER_NAME_PATTERNS: frozenset[str] = frozenset(
    {
        "firstname",
        "lastname",
        "surname",
        "fullname",
        "preferredname",
        "nhi",
        "dob",
        "birth",
        "address",
        "phone",
        "mobile",
        "email",
        "note",
        "comment",
        "description",
        "postcode",
        "ssn",
        "passport",
    }
)

# D-022: a column name matching one of these is clinical vocabulary (disease/
# condition/medication/allergy/vaccine labels) -- categorical_coded regardless
# of measured cardinality (bypasses CATEGORICAL_CARDINALITY_CEILING), made
# safe by MIN_TOP_VALUE_FREQUENCY_RATE/_COUNT: only values common enough that
# no individual's rare diagnosis can surface are ever captured.
CLINICAL_VOCABULARY_NAME_PATTERNS: frozenset[str] = frozenset(
    {"disease", "condition", "medication", "allergy", "vaccine"}
)

# D-022: a captured top-K value must cover at least this share of the
# profiled population, or this many rows, whichever is larger -- applied to
# every categorical_coded capture (not just clinical vocabulary; see D-022's
# scoping note). A value seen by only a handful of patients is never recorded.
MIN_TOP_VALUE_FREQUENCY_RATE = 0.001
MIN_TOP_VALUE_FREQUENCY_COUNT = 50

NUMERIC_ID_TYPES: frozenset[str] = frozenset(
    {"int", "bigint", "smallint", "tinyint", "decimal", "numeric"}
)
STRING_TYPES: frozenset[str] = frozenset({"char", "varchar", "nchar", "nvarchar", "text", "ntext"})
FREE_TEXT_EXPLICIT_TYPES: frozenset[str] = frozenset({"text", "ntext", "xml"})

QueryCostStatus = Literal["ok", "timeout", "skipped_cost"]
# D-023: "reference_vocabulary" is assigned by src/cdss/archetype.py after
# profile_view() runs, for the descriptive column of a view detected
# "reference" -- never assigned by classify_column() itself.
ColumnClass = Literal[
    "identifier_or_freetext", "categorical_coded", "measure", "key", "reference_vocabulary"
]

COLUMN_METADATA_QUERY = (
    "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH "
    "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? "
    "ORDER BY ORDINAL_POSITION"
)


def _is_timeout_error(exc: Exception) -> bool:
    message = str(exc)
    return "HYT00" in message or "timeout" in message.lower()


@dataclass(frozen=True)
class ColumnSampling:
    sampled: bool
    method: str


@dataclass(frozen=True)
class TopValue:
    value: str | None
    frequency: int


@dataclass(frozen=True)
class StringLengthStats:
    min_length: int
    max_length: int
    avg_length: float


@dataclass(frozen=True)
class ReferenceSamples:
    """D-023: a bounded, deterministic (key-ordered, not random) sample of a
    reference view's vocabulary column -- illustrative only, `sample_only` is
    always true, and this is never the full vocabulary (which stays in the
    source DB and is queried live when a check actually needs it)."""

    values: list[str]
    sample_only: Literal[True] = True


@dataclass(frozen=True)
class ValuePatternStats:
    """D-023: coarse, bounded syntactic-category counts (e.g. trailing
    "(disorder)"/"(procedure)" tags) -- categories shared across thousands of
    terms, not patient data, so no rare-value floor applies here."""

    trailing_tag_counts: dict[str, int]


@dataclass(frozen=True)
class ColumnProfile:
    column_name: str
    data_type: str
    is_free_text: bool
    column_class: ColumnClass
    sampling: ColumnSampling
    null_count: int | None
    null_rate: float | None
    distinct_count: int | None
    min_value: str | None
    max_value: str | None
    top_values: list[TopValue]
    string_length_stats: StringLengthStats | None
    reference_samples: ReferenceSamples | None = None
    value_pattern_stats: ValuePatternStats | None = None


@dataclass(frozen=True)
class QueryCost:
    view: str
    operation: Literal["column_profile"]
    duration_ms: float
    status: QueryCostStatus


@dataclass
class _RawColumnStats:
    total_count: int | None = None
    null_count: int | None = None
    distinct_count: int | None = None
    min_value: str | None = None
    max_value: str | None = None
    string_length_stats: StringLengthStats | None = None


def fetch_columns(
    audited: AuditedSourceConnection, qualified_name: str
) -> list[tuple[str, str, int | None]]:
    """One statement per view: column name, SQL type, char max length
    (INFORMATION_SCHEMA.COLUMNS, D-015), in ordinal order."""
    schema, _, name = qualified_name.partition(".")
    rows = audited.execute_query(COLUMN_METADATA_QUERY, params=(schema, name))
    return [
        (str(col_name), str(data_type), None if char_len is None else int(char_len))
        for col_name, data_type, char_len in rows
    ]


def _is_free_text(data_type: str, char_max_length: int | None) -> bool:
    lowered = data_type.lower()
    if lowered in FREE_TEXT_EXPLICIT_TYPES:
        return True
    return char_max_length is not None and (
        char_max_length == -1 or char_max_length > FREE_TEXT_LENGTH_THRESHOLD
    )


def _matches_identifier_pattern(column_name: str) -> bool:
    lowered = column_name.lower()
    return any(pattern in lowered for pattern in IDENTIFIER_NAME_PATTERNS)


def _matches_clinical_vocabulary_pattern(column_name: str) -> bool:
    lowered = column_name.lower()
    return any(pattern in lowered for pattern in CLINICAL_VOCABULARY_NAME_PATTERNS)


def _is_key_column(column_name: str, data_type: str) -> bool:
    return column_name.lower().endswith("id") and data_type.lower() in NUMERIC_ID_TYPES


def _capture_range(column_name: str, data_type: str, *, is_free_text: bool) -> bool:
    """D-020: whether `MIN`/`MAX(value)` may appear in the batched aggregate
    SQL at all, decided from name/type alone before any query runs. False for
    every string column and every identifier-name-matched column -- for those,
    the real value is never issued to the database, not fetched then
    discarded. Also false for `bit` -- SQL Server rejects MIN/MAX on it
    outright (error 8117, found live profiling fqb.Diagnosis: mocks never
    caught this because no fixture used a boolean-typed column)."""
    if is_free_text or data_type.lower() in STRING_TYPES or data_type.lower() == "bit":
        return False
    return not _matches_identifier_pattern(column_name)


def classify_column(
    column_name: str,
    data_type: str,
    *,
    is_free_text: bool,
    distinct_count: int | None,
    categorical_ceiling: int = CATEGORICAL_CARDINALITY_CEILING,
) -> ColumnClass:
    """D-020's tiered PHI policy. `distinct_count` may be `None` (e.g. a
    timed-out batch, F6) -- treated as unsure, resolves to the safest class
    (`identifier_or_freetext`, capture nothing)."""
    if _is_key_column(column_name, data_type):
        return "key"
    if _matches_identifier_pattern(column_name) or is_free_text:
        return "identifier_or_freetext"
    if _matches_clinical_vocabulary_pattern(column_name):
        return "categorical_coded"
    if distinct_count is None:
        return "identifier_or_freetext"
    if distinct_count <= categorical_ceiling:
        return "categorical_coded"
    return "identifier_or_freetext" if data_type.lower() in STRING_TYPES else "measure"


def _compute_watermark_cutoff(min_iso: str, max_iso: str, sample_fraction: float) -> str | None:
    try:
        min_dt = datetime.fromisoformat(min_iso)
        max_dt = datetime.fromisoformat(max_iso)
    except ValueError:
        return None
    if max_dt <= min_dt:
        return None
    cutoff = max_dt - (max_dt - min_dt) * sample_fraction
    return cutoff.isoformat(sep=" ")


def determine_sampling(
    *,
    row_count: int,
    numeric_id_columns: list[str],
    watermark_column: tuple[str, str, str] | None,
    anchor_column: str | None,
    large_view_threshold: int = LARGE_VIEW_ROW_THRESHOLD,
    sample_fraction: float = DEFAULT_SAMPLE_FRACTION,
) -> tuple[bool, str, str | None]:
    """D-018's three-tier decision. Returns (sampled, method, WHERE predicate
    or None). `method` always carries the strategy + predicate as free text
    (D-018: reproducibility lives in the string, not a new schema field)."""
    if row_count <= large_view_threshold:
        return False, "full", None

    k = max(round(1 / sample_fraction), 1)

    if numeric_id_columns:
        col = numeric_id_columns[0]
        return True, f"modulo:{col}%{k}=0", f"[{col}] % {k} = 0"

    if watermark_column is not None:
        name, min_iso, max_iso = watermark_column
        cutoff = _compute_watermark_cutoff(min_iso, max_iso, sample_fraction)
        if cutoff is not None:
            return True, f"watermark_range:{name}>='{cutoff}'", f"[{name}] >= '{cutoff}'"

    if anchor_column is not None:
        return (
            True,
            f"random:CHECKSUM(NEWID(),{anchor_column})%{k}=0",
            f"ABS(CHECKSUM(NEWID(), [{anchor_column}])) % {k} = 0",
        )

    return False, "full", None


def _build_batch_statement(
    source: str, batch: list[tuple[str, str, int | None]]
) -> tuple[str, list[bool], list[bool]]:
    """Returns (sql, is_string_flags, capture_range_flags) -- per-column flags
    recording whether string-length and/or value-range exprs were appended,
    so the caller can slice the result row positionally. `capture_range` is
    decided per D-020 before this statement is ever sent."""
    select_parts = ["COUNT(*)"]
    is_string_flags: list[bool] = []
    capture_range_flags: list[bool] = []
    for name, data_type, char_len in batch:
        is_string = data_type.lower() in STRING_TYPES
        capture_range = _capture_range(
            name, data_type, is_free_text=_is_free_text(data_type, char_len)
        )
        select_parts.append(f"COUNT(*) - COUNT([{name}])")
        select_parts.append(f"COUNT(DISTINCT [{name}])")
        if capture_range:
            select_parts.append(f"MIN([{name}])")
            select_parts.append(f"MAX([{name}])")
        if is_string:
            select_parts.append(f"MIN(LEN([{name}]))")
            select_parts.append(f"MAX(LEN([{name}]))")
            select_parts.append(f"AVG(CAST(LEN([{name}]) AS FLOAT))")
        is_string_flags.append(is_string)
        capture_range_flags.append(capture_range)
    sql = f"SELECT {', '.join(select_parts)} FROM {source}"
    return sql, is_string_flags, capture_range_flags


def _run_value_query(
    audited: AuditedSourceConnection,
    sql: str,
    *,
    qualified_name: str,
    timeout_seconds: int,
    costs: list[QueryCost],
) -> list[tuple[Any, ...]] | None:
    """Runs one value-bearing query (top-K or sentinel check), appending its
    cost entry. Returns `None` on timeout (F6/F10) rather than raising."""
    began = time.perf_counter()
    try:
        rows = audited.execute_query(sql, timeout_seconds=timeout_seconds)
    except Exception as exc:
        if not _is_timeout_error(exc):
            raise
        duration_ms = (time.perf_counter() - began) * 1000
        costs.append(
            QueryCost(
                view=qualified_name,
                operation="column_profile",
                duration_ms=round(duration_ms, 3),
                status="timeout",
            )
        )
        return None
    duration_ms = (time.perf_counter() - began) * 1000
    costs.append(
        QueryCost(
            view=qualified_name,
            operation="column_profile",
            duration_ms=round(duration_ms, 3),
            status="ok",
        )
    )
    return rows


def profile_view(
    audited: AuditedSourceConnection,
    *,
    qualified_name: str,
    row_count: int,
    columns: list[tuple[str, str, int | None]],
    watermark_column: tuple[str, str, str] | None = None,
    top_k: int = DEFAULT_TOP_K,
    batch_size: int = DEFAULT_AGGREGATE_BATCH_SIZE,
    cardinality_ceiling: int = CATEGORICAL_CARDINALITY_CEILING,
    large_view_threshold: int = LARGE_VIEW_ROW_THRESHOLD,
    sample_fraction: float = DEFAULT_SAMPLE_FRACTION,
    timeout_seconds: int = 30,
) -> tuple[list[ColumnProfile], list[QueryCost]]:
    """Profile every column of one view. `columns` is `(name, data_type,
    char_max_length)` in ordinal order, e.g. from `fetch_columns()`.
    `cardinality_ceiling` is D-020's categorical-vs-unclassified boundary
    (also gates the top-K query, since only `categorical_coded` columns ever
    get one)."""
    numeric_id_columns = [
        name for name, data_type, _char_len in columns if _is_key_column(name, data_type)
    ]
    anchor_column = columns[0][0] if columns else None
    sampled, method, predicate = determine_sampling(
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

    stats_by_name: dict[str, _RawColumnStats] = {}
    costs: list[QueryCost] = []

    for start in range(0, len(columns), batch_size):
        batch = columns[start : start + batch_size]
        sql, is_string_flags, capture_range_flags = _build_batch_statement(source, batch)
        began = time.perf_counter()
        try:
            (row,) = audited.execute_query(sql, timeout_seconds=timeout_seconds)
        except Exception as exc:
            if not _is_timeout_error(exc):
                raise
            duration_ms = (time.perf_counter() - began) * 1000
            costs.append(
                QueryCost(
                    view=qualified_name,
                    operation="column_profile",
                    duration_ms=round(duration_ms, 3),
                    status="timeout",
                )
            )
            for name, _data_type, _char_len in batch:
                stats_by_name[name] = _RawColumnStats()
            continue

        duration_ms = (time.perf_counter() - began) * 1000
        costs.append(
            QueryCost(
                view=qualified_name,
                operation="column_profile",
                duration_ms=round(duration_ms, 3),
                status="ok",
            )
        )

        total_count = row[0]
        idx = 1
        for (name, _data_type, _char_len), is_string, capture_range in zip(
            batch, is_string_flags, capture_range_flags, strict=True
        ):
            null_count, distinct_count = row[idx : idx + 2]
            idx += 2
            min_value: str | None = None
            max_value: str | None = None
            if capture_range:
                raw_min, raw_max = row[idx : idx + 2]
                idx += 2
                min_value = None if raw_min is None else str(raw_min)
                max_value = None if raw_max is None else str(raw_max)
            length_stats = None
            if is_string:
                min_len, max_len, avg_len = row[idx : idx + 3]
                idx += 3
                if min_len is not None:
                    length_stats = StringLengthStats(
                        min_length=int(min_len),
                        max_length=int(max_len),
                        avg_length=float(avg_len),
                    )
            stats_by_name[name] = _RawColumnStats(
                total_count=int(total_count) if total_count is not None else None,
                null_count=int(null_count) if null_count is not None else None,
                distinct_count=int(distinct_count) if distinct_count is not None else None,
                min_value=min_value,
                max_value=max_value,
                string_length_stats=length_stats,
            )

    profiles: list[ColumnProfile] = []
    for name, data_type, char_len in columns:
        raw = stats_by_name.get(name, _RawColumnStats())
        is_free_text = _is_free_text(data_type, char_len)
        null_rate = (
            raw.null_count / raw.total_count
            if raw.null_count is not None and raw.total_count
            else None
        )
        column_class = classify_column(
            name,
            data_type,
            is_free_text=is_free_text,
            distinct_count=raw.distinct_count,
            categorical_ceiling=cardinality_ceiling,
        )

        top_values: list[TopValue] = []
        if column_class == "categorical_coded":
            top_sql = (
                f"SELECT TOP ({top_k}) [{name}], COUNT(*) FROM {source} "
                f"GROUP BY [{name}] ORDER BY COUNT(*) DESC"
            )
            rows = _run_value_query(
                audited,
                top_sql,
                qualified_name=qualified_name,
                timeout_seconds=timeout_seconds,
                costs=costs,
            )
            if rows is not None:
                population = raw.total_count or 0
                floor = max(
                    population * MIN_TOP_VALUE_FREQUENCY_RATE, MIN_TOP_VALUE_FREQUENCY_COUNT
                )
                top_values = [
                    TopValue(value=None if value is None else str(value), frequency=int(freq))
                    for value, freq in rows
                    if int(freq) >= floor
                ]
        elif column_class == "key" and raw.total_count:
            sentinel_sql = (
                f"SELECT TOP (1) [{name}], COUNT(*) FROM {source} "
                f"GROUP BY [{name}] ORDER BY COUNT(*) DESC"
            )
            rows = _run_value_query(
                audited,
                sentinel_sql,
                qualified_name=qualified_name,
                timeout_seconds=timeout_seconds,
                costs=costs,
            )
            if rows:
                value, freq = rows[0]
                if freq / raw.total_count >= SENTINEL_FREQUENCY_THRESHOLD:
                    top_values = [
                        TopValue(value=None if value is None else str(value), frequency=int(freq))
                    ]

        profiles.append(
            ColumnProfile(
                column_name=name,
                data_type=data_type,
                is_free_text=is_free_text,
                column_class=column_class,
                sampling=ColumnSampling(sampled=sampled, method=method),
                null_count=raw.null_count,
                null_rate=null_rate,
                distinct_count=raw.distinct_count,
                min_value=raw.min_value,
                max_value=raw.max_value,
                top_values=top_values,
                string_length_stats=raw.string_length_stats,
            )
        )
    return profiles, costs
