"""Phase 1 step 5: sentinel candidates and test-record indicators.

Operates entirely on `ColumnProfile` data step 2 (D-020/D-022) already
captured -- no new live queries, and no new value exposure: a column's
sentinel candidacy can only be judged from values D-020's tiered policy
already permitted to be captured (min/max for `measure`/`key`, top-K for
`categorical_coded`/`reference_vocabulary`). Identifier-matched (Class A)
columns carry no captured value at all, so cannot be sentinel-checked by
value -- a structural consequence of the PHI policy, not an oversight: a
placeholder-DOB sentinel, for example, is undetectable under this design,
by design.

"Domains" (enumerated value + frequency for low-cardinality columns) are
`ColumnProfile.top_values` itself -- already produced by step 2, not
recomputed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from cdss.profiler import ColumnProfile

SentinelType = Literal[
    "placeholder_date", "zero_or_negative_id", "empty_string_overload", "magic_value"
]

# Known accidental/placeholder date sentinels. 1753-01-01 is SQL Server's own
# DATETIME minimum -- a frequent unintentional default, not a real date.
PLACEHOLDER_DATE_VALUES: frozenset[str] = frozenset(
    {"1900-01-01", "1753-01-01", "1970-01-01", "9999-12-31", "2099-12-31"}
)
DATE_TYPES: frozenset[str] = frozenset({"date", "datetime", "datetime2", "smalldatetime"})

# Exact match, not substring -- these are conventional flag-column names
# (spec-enumerated), not a pattern broad enough to risk false positives.
TEST_RECORD_NAME_PATTERNS: frozenset[str] = frozenset(
    {"istestrecord", "isdummy", "isdeleted", "isactive"}
)
TRUE_VALUE_TOKENS: frozenset[str] = frozenset({"1", "true"})

MAGIC_VALUE_DOMINANCE_RATIO = 3.0
MAGIC_VALUE_MIN_FREQUENCY = 20


@dataclass(frozen=True)
class SentinelCandidate:
    column_name: str
    sentinel_type: SentinelType
    value: str
    frequency: int | None
    description: str


@dataclass(frozen=True)
class TestRecordIndicator:
    column_name: str
    prevalence_count: int
    prevalence_rate: float


def _date_prefix(value: str) -> str:
    return value[:10]


def _detect_placeholder_dates(profile: ColumnProfile) -> list[SentinelCandidate]:
    if profile.data_type.lower() not in DATE_TYPES:
        return []
    candidates: list[SentinelCandidate] = []
    for label, value in (("min", profile.min_value), ("max", profile.max_value)):
        if value is not None and _date_prefix(value) in PLACEHOLDER_DATE_VALUES:
            candidates.append(
                SentinelCandidate(
                    column_name=profile.column_name,
                    sentinel_type="placeholder_date",
                    value=value,
                    frequency=None,
                    description=(
                        f"{label} value '{value}' matches a known placeholder-date pattern"
                    ),
                )
            )
    return candidates


def _detect_zero_or_negative_id(profile: ColumnProfile) -> list[SentinelCandidate]:
    if profile.column_class != "key" or profile.min_value is None:
        return []
    try:
        min_int = int(profile.min_value)
    except ValueError:
        return []
    if min_int > 0:
        return []
    frequency = None
    if profile.top_values and profile.top_values[0].value == profile.min_value:
        frequency = profile.top_values[0].frequency
    return [
        SentinelCandidate(
            column_name=profile.column_name,
            sentinel_type="zero_or_negative_id",
            value=profile.min_value,
            frequency=frequency,
            description=f"key column's minimum value is {profile.min_value} (<= 0)",
        )
    ]


def _detect_empty_string_overload(profile: ColumnProfile) -> list[SentinelCandidate]:
    if profile.column_class not in ("categorical_coded", "reference_vocabulary"):
        return []
    if not profile.null_count:
        return []
    for top_value in profile.top_values:
        if top_value.value == "":
            return [
                SentinelCandidate(
                    column_name=profile.column_name,
                    sentinel_type="empty_string_overload",
                    value="",
                    frequency=top_value.frequency,
                    description=(
                        f"empty string ({top_value.frequency} rows) and NULL "
                        f"({profile.null_count} rows) both present -- overloaded missingness"
                    ),
                )
            ]
    return []


def _detect_magic_value(profile: ColumnProfile) -> list[SentinelCandidate]:
    if profile.column_class != "categorical_coded" or len(profile.top_values) < 2:
        return []
    top, second = profile.top_values[0], profile.top_values[1]
    if top.value == "" or top.frequency < MAGIC_VALUE_MIN_FREQUENCY:
        return []
    if second.frequency == 0 or top.frequency / second.frequency < MAGIC_VALUE_DOMINANCE_RATIO:
        return []
    top_display = top.value if top.value is not None else "NULL"
    second_display = second.value if second.value is not None else "NULL"
    return [
        SentinelCandidate(
            column_name=profile.column_name,
            sentinel_type="magic_value",
            value=top_display,
            frequency=top.frequency,
            description=(
                f"'{top_display}' ({top.frequency}) dominates the next most frequent value "
                f"('{second_display}', {second.frequency}) by >= {MAGIC_VALUE_DOMINANCE_RATIO:g}x"
            ),
        )
    ]


def detect_sentinels(profiles: list[ColumnProfile]) -> list[SentinelCandidate]:
    """Runs every heuristic against every column's already-captured data --
    a no-op for columns with nothing captured (Class A/`identifier_or_freetext`,
    by design)."""
    candidates: list[SentinelCandidate] = []
    for profile in profiles:
        candidates.extend(_detect_placeholder_dates(profile))
        candidates.extend(_detect_zero_or_negative_id(profile))
        candidates.extend(_detect_empty_string_overload(profile))
        candidates.extend(_detect_magic_value(profile))
    return candidates


def _matches_test_record_pattern(column_name: str) -> bool:
    return column_name.lower() in TEST_RECORD_NAME_PATTERNS


def detect_test_record_indicators(
    profiles: list[ColumnProfile], row_count: int
) -> list[TestRecordIndicator]:
    """Prevalence of each test-record-indicator flag column -- these become
    standard `base_filters` defaults in Phase 2."""
    if row_count <= 0:
        return []
    indicators: list[TestRecordIndicator] = []
    for profile in profiles:
        if not _matches_test_record_pattern(profile.column_name):
            continue
        true_frequency = sum(
            top_value.frequency
            for top_value in profile.top_values
            if top_value.value is not None and top_value.value.lower() in TRUE_VALUE_TOKENS
        )
        indicators.append(
            TestRecordIndicator(
                column_name=profile.column_name,
                prevalence_count=true_frequency,
                prevalence_rate=round(true_frequency / row_count, 6),
            )
        )
    return indicators
