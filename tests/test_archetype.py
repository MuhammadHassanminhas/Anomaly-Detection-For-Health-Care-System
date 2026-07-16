"""D-023: table-archetype classification and reference-vocabulary capture
tests. All statements are answered by a scripted fake connection -- no live
database involved. Fixture view/column names below are entirely synthetic.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cdss.archetype import (
    ArchetypeResult,
    apply_reference_capture,
    capture_reference_vocabulary,
    detect_view_archetype,
)
from cdss.profiler import ColumnProfile, ColumnSampling, ReferenceSamples, ValuePatternStats
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
        clock=lambda: datetime(2026, 7, 16, tzinfo=UTC),
    )


# --- detect_view_archetype: reference ---------------------------------------------


def test_detect_view_archetype_reference_dictionary_shape(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        # population=27048, DiseaseName distinct=26195 -> ~96.8% unique
        return [(27_048, 26_195)]

    result, costs = detect_view_archetype(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=270_481,
        columns=[("DiseaseID", "int", None), ("DiseaseName", "nvarchar", 244)],
    )
    assert result.archetype == "reference"
    assert result.signals.has_key_column is True
    assert result.signals.name_column == "DiseaseName"
    assert result.signals.name_column_uniqueness is not None
    assert result.signals.name_column_uniqueness > 0.95
    assert result.signals.has_patient_linkage is False
    assert result.signals.has_clinical_event_date is False
    assert all(cost.status == "ok" for cost in costs)


# --- detect_view_archetype: fact (each disqualifying signal individually) --------


def test_detect_view_archetype_fact_no_key_column(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        return [(100, 95)]

    result, _costs = detect_view_archetype(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=100,
        columns=[("SomeLabel", "nvarchar", 50)],
    )
    assert result.archetype == "fact"
    assert result.signals.has_key_column is False
    assert "no key column" in result.reason


def test_detect_view_archetype_fact_no_near_unique_column(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        # distinct=5 out of population=100 -- far from unique (categorical, not vocabulary)
        return [(100, 5)]

    result, _costs = detect_view_archetype(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=100,
        columns=[("SyntheticID", "int", None), ("StatusLabel", "nvarchar", 50)],
    )
    assert result.archetype == "fact"
    assert result.signals.name_column is None


def test_detect_view_archetype_fact_patient_linkage_disqualifies(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        return [(100, 98)]

    result, _costs = detect_view_archetype(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=100,
        columns=[
            ("SyntheticID", "int", None),
            ("SyntheticLabel", "nvarchar", 50),
            ("PatientID", "int", None),
        ],
    )
    assert result.archetype == "fact"
    assert result.signals.has_patient_linkage is True
    assert "linkage" in result.reason


def test_detect_view_archetype_fact_clinical_event_date_disqualifies(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        return [(100, 98)]

    result, _costs = detect_view_archetype(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=100,
        columns=[
            ("SyntheticID", "int", None),
            ("SyntheticLabel", "nvarchar", 50),
            ("OnsetDate", "date", None),
        ],
    )
    assert result.archetype == "fact"
    assert result.signals.has_clinical_event_date is True


def test_detect_view_archetype_audit_timestamps_do_not_disqualify(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        return [(100, 98)]

    result, _costs = detect_view_archetype(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=100,
        columns=[
            ("SyntheticID", "int", None),
            ("SyntheticLabel", "nvarchar", 50),
            ("InsertedAt", "datetime", None),
            ("UpdatedAt", "datetime", None),
        ],
    )
    assert result.archetype == "reference"
    assert result.signals.has_clinical_event_date is False


def test_detect_view_archetype_identifier_matched_column_never_counts_as_name_column(
    tmp_path: Path,
) -> None:
    # A patient table where FirstName happens to be near-unique must never
    # trigger "reference" -- identifier-matched columns are excluded from
    # the name-column candidate pool entirely (safety property, D-023).
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        raise AssertionError(
            f"no archetype-detection query should be issued when there are no "
            f"eligible name-column candidates: {statement}"
        )

    result, costs = detect_view_archetype(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=100,
        columns=[("PatientID", "int", None), ("FirstName", "nvarchar", 50)],
    )
    assert result.archetype == "fact"
    assert result.signals.name_column is None
    assert costs == []


def test_detect_view_archetype_no_candidates_defaults_to_fact(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        raise AssertionError(f"unexpected statement: {statement}")

    result, costs = detect_view_archetype(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=100,
        columns=[("SyntheticID", "int", None)],
    )
    assert result.archetype == "fact"
    assert costs == []


def test_detect_view_archetype_query_timeout_defaults_to_fact(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]] | Exception:
        return FakeTimeoutError()

    result, costs = detect_view_archetype(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        row_count=100,
        columns=[("SyntheticID", "int", None), ("SyntheticLabel", "nvarchar", 50)],
    )
    assert result.archetype == "fact"
    assert result.signals.name_column is None
    assert costs[0].status == "timeout"


# --- capture_reference_vocabulary --------------------------------------------------


def test_capture_reference_vocabulary_returns_bounded_deterministic_sample_and_tags(
    tmp_path: Path,
) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        if statement.startswith("SELECT TOP (100) [DiseaseName]"):
            assert "ORDER BY [DiseaseID]" in statement
            return [("Hypertension (disorder)",), ("Common cold (disorder)",)]
        if statement.startswith("SELECT TOP (20) tag"):
            return [("(disorder)", 12_000), ("(procedure)", 8_000)]
        raise AssertionError(f"unexpected statement: {statement}")

    samples, tag_stats, costs = capture_reference_vocabulary(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        name_column="DiseaseName",
        key_column="DiseaseID",
    )
    assert samples == ReferenceSamples(values=["Hypertension (disorder)", "Common cold (disorder)"])
    assert samples.sample_only is True
    assert tag_stats == ValuePatternStats(
        trailing_tag_counts={"(disorder)": 12_000, "(procedure)": 8_000}
    )
    assert all(cost.status == "ok" for cost in costs)


def test_capture_reference_vocabulary_sample_timeout_yields_empty_samples(
    tmp_path: Path,
) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]] | Exception:
        if statement.startswith("SELECT TOP (100)"):
            return FakeTimeoutError()
        return [("(disorder)", 10)]

    samples, tag_stats, costs = capture_reference_vocabulary(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        name_column="DiseaseName",
        key_column="DiseaseID",
    )
    assert samples == ReferenceSamples(values=[])
    assert tag_stats.trailing_tag_counts == {"(disorder)": 10}
    assert costs[0].status == "timeout"
    assert costs[1].status == "ok"


def test_capture_reference_vocabulary_tag_timeout_yields_empty_tag_stats(
    tmp_path: Path,
) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]] | Exception:
        if statement.startswith("SELECT TOP (20)"):
            return FakeTimeoutError()
        return [("Example",)]

    samples, tag_stats, costs = capture_reference_vocabulary(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        name_column="DiseaseName",
        key_column="DiseaseID",
    )
    assert samples.values == ["Example"]
    assert tag_stats == ValuePatternStats(trailing_tag_counts={})
    assert costs[1].status == "timeout"


# --- apply_reference_capture --------------------------------------------------------


def test_apply_reference_capture_overrides_class_and_clears_ranked_values() -> None:
    profile = ColumnProfile(
        column_name="DiseaseName",
        data_type="nvarchar",
        is_free_text=False,
        column_class="categorical_coded",
        sampling=ColumnSampling(sampled=True, method="modulo:DiseaseID%10=0"),
        null_count=0,
        null_rate=0.0,
        distinct_count=26_195,
        min_value=None,
        max_value=None,
        top_values=[],
        string_length_stats=None,
    )
    samples = ReferenceSamples(values=["Example (disorder)"])
    tag_stats = ValuePatternStats(trailing_tag_counts={"(disorder)": 100})

    updated = apply_reference_capture(profile, samples, tag_stats)

    assert updated.column_class == "reference_vocabulary"
    assert updated.min_value is None
    assert updated.max_value is None
    assert updated.top_values == []
    assert updated.reference_samples == samples
    assert updated.value_pattern_stats == tag_stats
    # Untouched fields preserved.
    assert updated.column_name == "DiseaseName"
    assert updated.distinct_count == 26_195
    # Original profile is unchanged (frozen dataclass, dataclasses.replace).
    assert profile.column_class == "categorical_coded"


def test_archetype_result_is_frozen() -> None:
    from cdss.archetype import ArchetypeSignals

    signals = ArchetypeSignals(
        has_key_column=True,
        name_column="X",
        name_column_uniqueness=0.99,
        has_patient_linkage=False,
        has_clinical_event_date=False,
    )
    result = ArchetypeResult(archetype="reference", signals=signals, reason="test")
    assert result.archetype == "reference"
