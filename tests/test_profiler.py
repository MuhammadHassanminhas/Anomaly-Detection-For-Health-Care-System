"""Phase 1 step 2: column profiler tests. All statements are answered by a
scripted fake connection -- no live database involved. Fixture view/column
names below are entirely synthetic.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cdss.profiler import (
    COLUMN_METADATA_QUERY,
    ColumnSampling,
    QueryCost,
    TopValue,
    _capture_range,
    _is_free_text,
    _matches_clinical_vocabulary_pattern,
    _matches_identifier_pattern,
    classify_column,
    determine_sampling,
    fetch_columns,
    profile_view,
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
        allowed_objects=frozenset({"dbo.syntheticview"}),
        audit_dir=tmp_path,
        clock=lambda: datetime(2026, 7, 15, tzinfo=UTC),
    )


# --- fetch_columns ------------------------------------------------------------


def test_fetch_columns_filters_and_orders_by_ordinal(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        assert statement == COLUMN_METADATA_QUERY
        return [
            ("SyntheticID", "int", None),
            ("SyntheticNotes", "nvarchar", -1),
        ]

    columns = fetch_columns(_make_audited(tmp_path, responder), "dbo.SyntheticView")
    assert columns == [("SyntheticID", "int", None), ("SyntheticNotes", "nvarchar", -1)]


# --- _is_free_text -------------------------------------------------------------


def test_is_free_text_explicit_type() -> None:
    assert _is_free_text("text", None) is True
    assert _is_free_text("xml", None) is True


def test_is_free_text_max_length_sentinel() -> None:
    assert _is_free_text("nvarchar", -1) is True


def test_is_free_text_long_varchar() -> None:
    assert _is_free_text("nvarchar", 4000) is True


def test_is_free_text_short_code_column() -> None:
    assert _is_free_text("nvarchar", 20) is False
    assert _is_free_text("int", None) is False


# --- D-020: identifier pattern matching -----------------------------------------


def test_matches_identifier_pattern_direct_match() -> None:
    assert _matches_identifier_pattern("PatientFirstName") is True
    assert _matches_identifier_pattern("PatientSurname") is True
    assert _matches_identifier_pattern("FullName") is True
    assert _matches_identifier_pattern("PreferredName") is True
    assert _matches_identifier_pattern("DOB") is True
    assert _matches_identifier_pattern("HomeAddress") is True
    assert _matches_identifier_pattern("EmailAddress") is True


def test_matches_identifier_pattern_no_match() -> None:
    assert _matches_identifier_pattern("StatusCode") is False
    assert _matches_identifier_pattern("Amount") is False
    assert _matches_identifier_pattern("PatientID") is False


def test_matches_identifier_pattern_generic_name_no_longer_matches() -> None:
    # D-022: a bare "name" substring is too broad -- it caught clinical
    # vocabulary columns like DiseaseName that aren't person-identifying.
    # Only specific person-name compounds match now.
    assert _matches_identifier_pattern("DiseaseName") is False
    assert _matches_identifier_pattern("MedicationName") is False
    assert _matches_identifier_pattern("TypeName") is False


# --- D-022: clinical vocabulary pattern matching --------------------------------


def test_matches_clinical_vocabulary_pattern_direct_match() -> None:
    assert _matches_clinical_vocabulary_pattern("DiseaseName") is True
    assert _matches_clinical_vocabulary_pattern("ConditionName") is True
    assert _matches_clinical_vocabulary_pattern("MedicationName") is True
    assert _matches_clinical_vocabulary_pattern("AllergyName") is True
    assert _matches_clinical_vocabulary_pattern("VaccineName") is True


def test_matches_clinical_vocabulary_pattern_no_match() -> None:
    assert _matches_clinical_vocabulary_pattern("PatientFirstName") is False
    assert _matches_clinical_vocabulary_pattern("StatusCode") is False


# --- D-020: classify_column ------------------------------------------------------


def test_classify_column_key_takes_priority_over_everything() -> None:
    # Ends in "id" + numeric type -> key, regardless of cardinality.
    assert classify_column("PatientID", "int", is_free_text=False, distinct_count=5) == "key"


def test_classify_column_identifier_name_match_forces_class_a_even_when_low_cardinality() -> None:
    # DOB has low cardinality (few distinct birth dates in a small sample) but
    # is still Class A -- name-pattern matching runs before the cardinality
    # check, and before any type-based branching (DOB is a date, not a key).
    assert (
        classify_column("DOB", "date", is_free_text=False, distinct_count=5)
        == "identifier_or_freetext"
    )


def test_classify_column_free_text_is_class_a() -> None:
    assert (
        classify_column("Notes", "nvarchar", is_free_text=True, distinct_count=None)
        == "identifier_or_freetext"
    )


def test_classify_column_low_cardinality_is_categorical() -> None:
    assert (
        classify_column("StatusCode", "nvarchar", is_free_text=False, distinct_count=5)
        == "categorical_coded"
    )
    # Cardinality-based, not type-based -- a small-int code column qualifies too.
    assert (
        classify_column("TypeFlag", "int", is_free_text=False, distinct_count=3)
        == "categorical_coded"
    )


def test_classify_column_high_cardinality_string_defaults_to_class_a() -> None:
    assert (
        classify_column("ExternalCode", "nvarchar", is_free_text=False, distinct_count=5000)
        == "identifier_or_freetext"
    )


def test_classify_column_high_cardinality_numeric_is_measure() -> None:
    assert (
        classify_column("Amount", "decimal", is_free_text=False, distinct_count=5000) == "measure"
    )


def test_classify_column_unknown_distinct_count_defaults_safely() -> None:
    # A timed-out batch leaves distinct_count None (F6) -- treated as unsure,
    # resolves to the safest class even for a numeric column.
    assert (
        classify_column("Amount", "decimal", is_free_text=False, distinct_count=None)
        == "identifier_or_freetext"
    )


def test_classify_column_clinical_vocabulary_bypasses_cardinality_ceiling() -> None:
    # D-022: DiseaseName-style columns are categorical (top-K + frequencies)
    # even at very high cardinality -- the rare-value floor in profile_view
    # is what makes this safe, not a cardinality ceiling.
    assert (
        classify_column("DiseaseName", "nvarchar", is_free_text=False, distinct_count=26_195)
        == "categorical_coded"
    )


def test_classify_column_clinical_vocabulary_still_free_text_stays_class_a() -> None:
    assert (
        classify_column("ConditionNotes", "nvarchar", is_free_text=True, distinct_count=5000)
        == "identifier_or_freetext"
    )


def test_classify_column_respects_custom_ceiling() -> None:
    assert (
        classify_column(
            "Code", "nvarchar", is_free_text=False, distinct_count=50, categorical_ceiling=10
        )
        == "identifier_or_freetext"
    )


# --- D-020: _capture_range -------------------------------------------------------


def test_capture_range_false_for_string_types() -> None:
    assert _capture_range("StatusCode", "nvarchar", is_free_text=False) is False


def test_capture_range_false_for_free_text() -> None:
    assert _capture_range("Notes", "text", is_free_text=True) is False


def test_capture_range_false_for_identifier_matched_even_when_numeric_or_date() -> None:
    assert _capture_range("DOB", "date", is_free_text=False) is False


def test_capture_range_true_for_plain_measure_column() -> None:
    assert _capture_range("Amount", "decimal", is_free_text=False) is True


def test_capture_range_false_for_bit_type() -> None:
    # SQL Server rejects MIN/MAX on bit outright (error 8117) -- found live
    # profiling fqb.Diagnosis; no mocked fixture had a boolean-typed column.
    assert _capture_range("IsActive", "bit", is_free_text=False) is False


# --- determine_sampling ---------------------------------------------------------


def test_determine_sampling_small_view_is_full_scan() -> None:
    sampled, method, predicate = determine_sampling(
        row_count=100_000,
        numeric_id_columns=["SyntheticID"],
        watermark_column=None,
        anchor_column="SyntheticID",
    )
    assert (sampled, method, predicate) == (False, "full", None)


def test_determine_sampling_large_view_prefers_modulo_on_id_column() -> None:
    sampled, method, predicate = determine_sampling(
        row_count=1_000_000,
        numeric_id_columns=["SyntheticID"],
        watermark_column=("InsertedAt", "2020-01-01", "2026-01-01"),
        anchor_column="SyntheticID",
    )
    assert sampled is True
    assert method == "modulo:SyntheticID%10=0"
    assert predicate == "[SyntheticID] % 10 = 0"


def test_determine_sampling_falls_back_to_watermark_range() -> None:
    sampled, method, predicate = determine_sampling(
        row_count=1_000_000,
        numeric_id_columns=[],
        watermark_column=("InsertedAt", "2020-01-01", "2030-01-01"),
        anchor_column="SyntheticID",
    )
    assert sampled is True
    assert method.startswith("watermark_range:InsertedAt>=")
    assert predicate is not None and predicate.startswith("[InsertedAt] >= '")


def test_determine_sampling_falls_back_to_random_when_no_key_or_watermark() -> None:
    sampled, method, predicate = determine_sampling(
        row_count=1_000_000,
        numeric_id_columns=[],
        watermark_column=None,
        anchor_column="SomeColumn",
    )
    assert sampled is True
    assert method == "random:CHECKSUM(NEWID(),SomeColumn)%10=0"
    assert predicate == "ABS(CHECKSUM(NEWID(), [SomeColumn])) % 10 = 0"


def test_determine_sampling_no_usable_column_stays_full() -> None:
    sampled, method, predicate = determine_sampling(
        row_count=1_000_000,
        numeric_id_columns=[],
        watermark_column=None,
        anchor_column=None,
    )
    assert (sampled, method, predicate) == (False, "full", None)


def test_determine_sampling_ignores_unparseable_watermark_and_falls_back_to_random() -> None:
    sampled, method, _predicate = determine_sampling(
        row_count=1_000_000,
        numeric_id_columns=[],
        watermark_column=("InsertedAt", "not-a-date", "also-not-a-date"),
        anchor_column="SomeColumn",
    )
    assert sampled is True
    assert method.startswith("random:")


# --- profile_view: small view, no sampling --------------------------------------

SMALL_VIEW_COLUMNS = [
    ("SyntheticID", "int", None),
    ("SyntheticStatus", "nvarchar", 20),
]


def test_profile_view_small_view_populates_stats_without_sampling(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        if statement.startswith("SELECT COUNT(*), COUNT(*) - COUNT([SyntheticID])"):
            # total, [ID: null,distinct,min,max], [Status: null,distinct,minlen,maxlen,avglen]
            # (Status is a string -> no MIN/MAX value ever queried, D-020.)
            return [(100, 0, 100, 1, 100, 5, 95, 3, 12, 6.5)]
        if statement.startswith("SELECT TOP (1) [SyntheticID]"):
            return [(1, 1)]  # 1% of rows -- below the 5% sentinel threshold
        if statement.startswith("SELECT TOP (20) [SyntheticStatus]"):
            # floor = max(100 * 0.001, 50) = 50 -- "Rare" (frequency 2) is dropped (D-022)
            return [("Alpha", 60), ("Beta", 50), ("Rare", 2)]
        raise AssertionError(f"unexpected statement: {statement}")

    profiles, costs = profile_view(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=100,
        columns=SMALL_VIEW_COLUMNS,
    )

    by_name = {p.column_name: p for p in profiles}
    id_profile = by_name["SyntheticID"]
    assert id_profile.column_class == "key"
    assert id_profile.sampling == ColumnSampling(sampled=False, method="full")
    assert id_profile.null_count == 0
    assert id_profile.null_rate == 0.0
    assert id_profile.distinct_count == 100
    assert id_profile.min_value == "1"
    assert id_profile.max_value == "100"
    assert id_profile.string_length_stats is None
    assert id_profile.top_values == []  # sentinel frequency (1%) below threshold

    status_profile = by_name["SyntheticStatus"]
    assert status_profile.column_class == "categorical_coded"
    assert status_profile.null_count == 5
    assert status_profile.null_rate == 0.05
    assert status_profile.distinct_count == 95
    assert status_profile.min_value is None
    assert status_profile.max_value is None
    assert status_profile.string_length_stats is not None
    assert status_profile.string_length_stats.min_length == 3
    assert status_profile.string_length_stats.max_length == 12
    assert status_profile.string_length_stats.avg_length == 6.5
    assert status_profile.top_values == [
        TopValue(value="Alpha", frequency=60),
        TopValue(value="Beta", frequency=50),
    ]

    assert all(cost.status == "ok" for cost in costs)
    assert any(cost.operation == "column_profile" for cost in costs)


def test_profile_view_key_column_records_sentinel_above_threshold(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        if statement.startswith("SELECT COUNT(*)"):
            return [(100, 0, 50, 1, 100)]
        if statement.startswith("SELECT TOP (1) [SyntheticID]"):
            return [("0", 10)]  # 10% of rows -- a placeholder/sentinel, not a record
        raise AssertionError(f"unexpected statement: {statement}")

    profiles, _costs = profile_view(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=100,
        columns=[("SyntheticID", "int", None)],
    )
    assert profiles[0].column_class == "key"
    assert profiles[0].top_values == [TopValue(value="0", frequency=10)]


def test_profile_view_key_sentinel_query_timeout_leaves_top_values_empty(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]] | Exception:
        if statement.startswith("SELECT COUNT(*)"):
            return [(100, 0, 50, 1, 100)]
        if statement.startswith("SELECT TOP (1)"):
            return FakeTimeoutError()
        raise AssertionError(f"unexpected statement: {statement}")

    profiles, costs = profile_view(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=100,
        columns=[("SyntheticID", "int", None)],
    )
    assert profiles[0].column_class == "key"
    assert profiles[0].top_values == []
    assert costs[-1].status == "timeout"


def test_profile_view_identifier_matched_column_never_issues_value_query(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        if statement.startswith("SELECT COUNT(*)"):
            # No MIN/MAX in the SELECT list at all -- D-020: a real value is
            # never issued to the database for an identifier-matched column.
            return [(100, 2, 80, 4, 40, 12.0)]
        raise AssertionError(
            f"a value-bearing query must never be issued for an identifier-matched column: "
            f"{statement}"
        )

    profiles, _costs = profile_view(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=100,
        columns=[("SyntheticFirstName", "nvarchar", 100)],
    )
    p = profiles[0]
    assert p.column_class == "identifier_or_freetext"
    assert p.min_value is None
    assert p.max_value is None
    assert p.top_values == []
    assert p.string_length_stats is not None


def test_profile_view_high_cardinality_unmatched_string_defaults_to_identifier_class(
    tmp_path: Path,
) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        if statement.startswith("SELECT COUNT(*)"):
            # total, null, distinct, minlen, maxlen, avglen (no value MIN/MAX -- string)
            return [(100, 0, 2000, 3, 20, 8.5)]
        raise AssertionError(f"unexpected statement: {statement}")

    profiles, _costs = profile_view(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=100,
        columns=[("SyntheticCode", "nvarchar", 20)],
        cardinality_ceiling=1000,
    )
    p = profiles[0]
    assert p.distinct_count == 2000
    assert p.column_class == "identifier_or_freetext"
    assert p.min_value is None
    assert p.max_value is None
    assert p.top_values == []


def test_profile_view_skips_top_k_for_free_text_column(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        if statement.startswith("SELECT COUNT(*)"):
            # total, null, distinct, minlen, maxlen, avglen (no value MIN/MAX -- free text)
            return [(100, 10, 50, 5, 500, 50.0)]
        raise AssertionError(f"unexpected top-K query issued for a free-text column: {statement}")

    profiles, _costs = profile_view(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=100,
        columns=[("SyntheticNotes", "nvarchar", -1)],
    )
    assert profiles[0].is_free_text is True
    assert profiles[0].column_class == "identifier_or_freetext"
    assert profiles[0].min_value is None
    assert profiles[0].max_value is None
    assert profiles[0].top_values == []


# --- profile_view: batching ------------------------------------------------------


def test_profile_view_batches_columns_into_separate_statements(tmp_path: Path) -> None:
    statements_seen: list[str] = []

    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        statements_seen.append(statement)
        # Each batch has up to 2 non-key, non-identifier int columns -> "measure"
        # (distinct_count 100 > cardinality_ceiling 0): 1 (total) + 4 stats per column.
        return [(100, 0, 100, 1, 100, 0, 100, 1, 100)]

    columns: list[tuple[str, str, int | None]] = [(f"Col{i}", "int", None) for i in range(5)]
    profile_view(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=100,
        columns=columns,
        batch_size=2,
        cardinality_ceiling=0,  # forces "measure", skipping top-K entirely to isolate batching
    )
    aggregate_statements = [s for s in statements_seen if s.startswith("SELECT COUNT(*)")]
    assert len(aggregate_statements) == 3  # ceil(5/2)


# --- profile_view: timeout handling ----------------------------------------------


def test_profile_view_batch_timeout_marks_columns_indeterminate(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]] | Exception:
        if statement.startswith("SELECT COUNT(*)"):
            return FakeTimeoutError()
        raise AssertionError(f"unexpected statement: {statement}")

    profiles, costs = profile_view(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=100,
        columns=[("SyntheticID", "int", None)],
    )
    assert profiles[0].column_class == "key"
    assert profiles[0].null_count is None
    assert profiles[0].distinct_count is None
    assert profiles[0].min_value is None
    assert profiles[0].top_values == []
    assert costs[0].status == "timeout"


def test_profile_view_non_timeout_error_propagates(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]] | Exception:
        return ValueError("some unrelated real error")

    with pytest.raises(ValueError, match="unrelated real error"):
        profile_view(
            _make_audited(tmp_path, responder),
            qualified_name="dbo.SyntheticView",
            row_count=100,
            columns=[("SyntheticID", "int", None)],
        )


# --- profile_view: large view sampling wiring ------------------------------------


def test_profile_view_large_view_samples_and_tags_columns(tmp_path: Path) -> None:
    seen_sources: list[str] = []

    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        seen_sources.append(statement)
        if statement.startswith("SELECT TOP (1) [SyntheticID]"):
            return [(1, 100)]  # 0.2% of rows -- well below the 5% sentinel threshold
        return [(50_000, 0, 50_000, 1, 500_000)]

    profiles, _costs = profile_view(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=1_000_000,
        columns=[("SyntheticID", "int", None)],
    )
    assert profiles[0].sampling.sampled is True
    assert profiles[0].sampling.method == "modulo:SyntheticID%10=0"
    assert "WHERE [SyntheticID] % 10 = 0" in seen_sources[0]
    assert profiles[0].top_values == []


def test_profile_view_top_k_timeout_leaves_top_values_empty(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]] | Exception:
        if statement.startswith("SELECT COUNT(*)"):
            # total, null, distinct, minlen, maxlen, avglen -- categorical string, no value MIN/MAX
            return [(100, 0, 5, 3, 10, 5.0)]
        if statement.startswith("SELECT TOP (20)"):
            return FakeTimeoutError()
        raise AssertionError(f"unexpected statement: {statement}")

    profiles, costs = profile_view(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=100,
        columns=[("SyntheticCode", "nvarchar", 20)],
    )
    assert profiles[0].column_class == "categorical_coded"
    assert profiles[0].distinct_count == 5
    assert profiles[0].top_values == []
    assert costs[-1].status == "timeout"


# --- D-022: rare-value floor on categorical top-K captures -----------------------


def test_profile_view_categorical_top_k_drops_values_below_rare_value_floor(
    tmp_path: Path,
) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        if statement.startswith("SELECT COUNT(*)"):
            # population=100_000, distinct=5 -> categorical
            return [(100_000, 0, 5, 3, 10, 5.0)]
        if statement.startswith("SELECT TOP (20)"):
            # floor = max(100_000 * 0.001, 50) = 100 -- "Rare" (frequency 3) is dropped
            return [("Common", 50_000), ("Uncommon", 150), ("Rare", 3)]
        raise AssertionError(f"unexpected statement: {statement}")

    profiles, _costs = profile_view(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=100_000,
        columns=[("SyntheticCode", "nvarchar", 20)],
    )
    assert profiles[0].column_class == "categorical_coded"
    assert profiles[0].top_values == [
        TopValue(value="Common", frequency=50_000),
        TopValue(value="Uncommon", frequency=150),
    ]


def test_profile_view_categorical_top_k_floor_uses_row_count_when_larger(
    tmp_path: Path,
) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        if statement.startswith("SELECT COUNT(*)"):
            # population=100 -> floor = max(100 * 0.001, 50) = 50 (the row-count floor wins)
            return [(100, 0, 3, 3, 10, 5.0)]
        if statement.startswith("SELECT TOP (20)"):
            return [("Common", 60), ("JustUnder", 49)]
        raise AssertionError(f"unexpected statement: {statement}")

    profiles, _costs = profile_view(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=100,
        columns=[("SyntheticCode", "nvarchar", 20)],
    )
    assert profiles[0].top_values == [TopValue(value="Common", frequency=60)]


def test_profile_view_clinical_vocabulary_column_captured_above_cardinality_ceiling(
    tmp_path: Path,
) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        if statement.startswith("SELECT COUNT(*)"):
            # distinct=26_195, well above the 200-distinct categorical ceiling
            return [(270_481, 0, 26_195, 5, 244, 47.3)]
        if statement.startswith("SELECT TOP (20)"):
            return [("Hypertension", 40_000), ("RareCongenitalSyndrome", 1)]
        raise AssertionError(f"unexpected statement: {statement}")

    profiles, _costs = profile_view(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=270_481,
        columns=[("DiseaseName", "nvarchar", 244)],
    )
    p = profiles[0]
    assert p.column_class == "categorical_coded"
    # The common value is captured; the one-off value is not (rare-value floor).
    assert p.top_values == [TopValue(value="Hypertension", frequency=40_000)]


def test_profile_view_bit_column_never_issues_min_max(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        if statement.startswith("SELECT COUNT(*)"):
            assert "MIN([IsActive])" not in statement
            assert "MAX([IsActive])" not in statement
            # total, null, distinct (no MIN/MAX -- bit type)
            return [(200, 0, 2)]
        if statement.startswith("SELECT TOP (20) [IsActive]"):
            # floor = max(200 * 0.001, 50) = 50 -- both values clear it (D-022)
            return [(1, 120), (0, 80)]
        raise AssertionError(f"unexpected statement: {statement}")

    profiles, _costs = profile_view(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=200,
        columns=[("IsActive", "bit", None)],
    )
    p = profiles[0]
    assert p.column_class == "categorical_coded"
    assert p.min_value is None
    assert p.max_value is None
    assert p.top_values == [TopValue(value="1", frequency=120), TopValue(value="0", frequency=80)]


def test_query_cost_is_frozen() -> None:
    cost = QueryCost(view="dbo.X", operation="column_profile", duration_ms=1.0, status="ok")
    assert cost.view == "dbo.X"
