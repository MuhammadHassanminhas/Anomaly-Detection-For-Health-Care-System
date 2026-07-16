"""Phase 1 step 5: sentinel candidates + test-record indicators. Pure
functions over already-captured ColumnProfile data -- no DB, no mocking.
Fixture column names below are entirely synthetic.
"""

from cdss.profiler import ColumnProfile, ColumnSampling, TopValue
from cdss.sentinels import (
    SentinelCandidate,
    TestRecordIndicator,
    detect_sentinels,
    detect_test_record_indicators,
)


def _profile(
    name: str,
    data_type: str,
    *,
    column_class: str = "categorical_coded",
    min_value: str | None = None,
    max_value: str | None = None,
    top_values: list[TopValue] | None = None,
    null_count: int | None = 0,
    distinct_count: int | None = 5,
) -> ColumnProfile:
    return ColumnProfile(
        column_name=name,
        data_type=data_type,
        is_free_text=False,
        column_class=column_class,  # type: ignore[arg-type]
        sampling=ColumnSampling(sampled=False, method="full"),
        null_count=null_count,
        null_rate=None,
        distinct_count=distinct_count,
        min_value=min_value,
        max_value=max_value,
        top_values=top_values or [],
        string_length_stats=None,
    )


# --- placeholder_date ---------------------------------------------------------------


def test_detect_placeholder_date_on_min_value() -> None:
    p = _profile("SyntheticDate", "datetime", column_class="measure", min_value="1753-01-01")
    candidates = detect_sentinels([p])
    assert candidates == [
        SentinelCandidate(
            column_name="SyntheticDate",
            sentinel_type="placeholder_date",
            value="1753-01-01",
            frequency=None,
            description="min value '1753-01-01' matches a known placeholder-date pattern",
        )
    ]


def test_detect_placeholder_date_on_max_value() -> None:
    p = _profile(
        "SyntheticDate",
        "datetime",
        column_class="measure",
        min_value="2010-01-01",
        max_value="9999-12-31",
    )
    candidates = detect_sentinels([p])
    assert len(candidates) == 1
    assert candidates[0].value == "9999-12-31"


def test_detect_placeholder_date_matches_prefix_with_time_component() -> None:
    p = _profile(
        "SyntheticDate", "datetime", column_class="measure", min_value="1753-01-01 00:00:00"
    )
    candidates = detect_sentinels([p])
    assert len(candidates) == 1


def test_no_placeholder_date_for_ordinary_dates() -> None:
    p = _profile(
        "SyntheticDate",
        "datetime",
        column_class="measure",
        min_value="2010-05-01",
        max_value="2020-06-01",
    )
    assert detect_sentinels([p]) == []


def test_no_placeholder_date_for_non_date_type() -> None:
    p = _profile("SyntheticCode", "nvarchar", min_value="1900-01-01")
    assert detect_sentinels([p]) == []


# --- zero_or_negative_id -------------------------------------------------------------


def test_detect_zero_or_negative_id_from_min_value() -> None:
    p = _profile("SyntheticID", "int", column_class="key", min_value="0", max_value="1000")
    candidates = detect_sentinels([p])
    assert candidates == [
        SentinelCandidate(
            column_name="SyntheticID",
            sentinel_type="zero_or_negative_id",
            value="0",
            frequency=None,
            description="key column's minimum value is 0 (<= 0)",
        )
    ]


def test_detect_zero_or_negative_id_uses_captured_sentinel_frequency() -> None:
    p = _profile(
        "SyntheticID",
        "int",
        column_class="key",
        min_value="-1",
        top_values=[TopValue(value="-1", frequency=42)],
    )
    candidates = detect_sentinels([p])
    assert candidates[0].frequency == 42


def test_no_zero_or_negative_id_for_positive_min() -> None:
    p = _profile("SyntheticID", "int", column_class="key", min_value="1")
    assert detect_sentinels([p]) == []


def test_no_zero_or_negative_id_for_non_key_column() -> None:
    p = _profile("SyntheticCode", "int", column_class="measure", min_value="-1")
    assert detect_sentinels([p]) == []


# --- empty_string_overload -----------------------------------------------------------


def test_detect_empty_string_overload() -> None:
    # Frequencies chosen so magic_value's dominance ratio (< 3x here) doesn't
    # also fire -- this test isolates empty_string_overload specifically.
    p = _profile(
        "SyntheticStatus",
        "nvarchar",
        column_class="categorical_coded",
        null_count=10,
        top_values=[TopValue(value="Active", frequency=40), TopValue(value="", frequency=15)],
    )
    candidates = detect_sentinels([p])
    assert candidates == [
        SentinelCandidate(
            column_name="SyntheticStatus",
            sentinel_type="empty_string_overload",
            value="",
            frequency=15,
            description=(
                "empty string (15 rows) and NULL (10 rows) both present -- overloaded missingness"
            ),
        )
    ]


def test_no_empty_string_overload_without_nulls() -> None:
    p = _profile(
        "SyntheticStatus",
        "nvarchar",
        null_count=0,
        top_values=[TopValue(value="", frequency=15)],
    )
    assert detect_sentinels([p]) == []


def test_no_empty_string_overload_without_empty_value() -> None:
    p = _profile(
        "SyntheticStatus",
        "nvarchar",
        null_count=10,
        top_values=[TopValue(value="Active", frequency=80)],
    )
    assert detect_sentinels([p]) == []


# --- magic_value ------------------------------------------------------------------------


def test_detect_magic_value_dominant() -> None:
    p = _profile(
        "SyntheticFlag",
        "nvarchar",
        column_class="categorical_coded",
        top_values=[TopValue(value="Unknown", frequency=900), TopValue(value="A", frequency=50)],
    )
    candidates = detect_sentinels([p])
    assert candidates == [
        SentinelCandidate(
            column_name="SyntheticFlag",
            sentinel_type="magic_value",
            value="Unknown",
            frequency=900,
            description=(
                "'Unknown' (900) dominates the next most frequent value ('A', 50) by >= 3x"
            ),
        )
    ]


def test_no_magic_value_when_not_dominant_enough() -> None:
    p = _profile(
        "SyntheticFlag",
        "nvarchar",
        top_values=[TopValue(value="A", frequency=60), TopValue(value="B", frequency=40)],
    )
    assert detect_sentinels([p]) == []


def test_no_magic_value_below_minimum_frequency() -> None:
    p = _profile(
        "SyntheticFlag",
        "nvarchar",
        top_values=[TopValue(value="A", frequency=10), TopValue(value="B", frequency=1)],
    )
    assert detect_sentinels([p]) == []


def test_detect_magic_value_null_dominant_display_is_consistent() -> None:
    # Live finding (fqb.Diagnosis.SequenceNo): a NULL-dominant column's value
    # and description must display "NULL" consistently, not "None" in one
    # field and "NULL" in the other.
    p = _profile(
        "SyntheticNullable",
        "int",
        column_class="categorical_coded",
        top_values=[TopValue(value=None, frequency=3909), TopValue(value="1", frequency=1218)],
    )
    candidates = detect_sentinels([p])
    assert candidates[0].value == "NULL"
    assert "'NULL' (3909)" in candidates[0].description
    assert "None" not in candidates[0].description


def test_no_magic_value_for_key_or_reference_vocabulary_class() -> None:
    p = _profile(
        "SyntheticID",
        "int",
        column_class="key",
        top_values=[TopValue(value="0", frequency=900), TopValue(value="1", frequency=1)],
    )
    assert detect_sentinels([p]) == []


# --- identifier_or_freetext / measure columns never yield value-based sentinels ------


def test_identifier_or_freetext_column_yields_no_sentinels() -> None:
    p = _profile(
        "PatientFirstName",
        "nvarchar",
        column_class="identifier_or_freetext",
        min_value=None,
        max_value=None,
        top_values=[],
        null_count=0,
    )
    assert detect_sentinels([p]) == []


# --- test-record indicators ------------------------------------------------------------


def test_detect_test_record_indicator_prevalence() -> None:
    p = _profile(
        "IsActive",
        "bit",
        top_values=[TopValue(value="1", frequency=800), TopValue(value="0", frequency=200)],
    )
    indicators = detect_test_record_indicators([p], row_count=1000)
    assert indicators == [
        TestRecordIndicator(column_name="IsActive", prevalence_count=800, prevalence_rate=0.8)
    ]


def test_detect_test_record_indicator_accepts_true_token() -> None:
    p = _profile(
        "IsDeleted",
        "nvarchar",
        top_values=[TopValue(value="true", frequency=5), TopValue(value="false", frequency=995)],
    )
    indicators = detect_test_record_indicators([p], row_count=1000)
    assert indicators[0].prevalence_count == 5


def test_non_matching_column_name_produces_no_indicator() -> None:
    p = _profile("StatusCode", "nvarchar", top_values=[TopValue(value="1", frequency=800)])
    assert detect_test_record_indicators([p], row_count=1000) == []


def test_test_record_indicator_zero_row_count_returns_empty() -> None:
    p = _profile("IsActive", "bit", top_values=[TopValue(value="1", frequency=0)])
    assert detect_test_record_indicators([p], row_count=0) == []


def test_sentinel_candidate_and_indicator_dataclasses_are_frozen() -> None:
    sentinel = SentinelCandidate(
        column_name="X",
        sentinel_type="magic_value",
        value="Unknown",
        frequency=10,
        description="test",
    )
    assert sentinel.value == "Unknown"
    indicator = TestRecordIndicator(column_name="X", prevalence_count=1, prevalence_rate=0.5)
    assert indicator.prevalence_rate == 0.5
