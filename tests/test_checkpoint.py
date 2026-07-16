"""Phase 1 step 8: per-view profiling checkpoint tests. Pure functions --
no DB access. Fixture data is entirely synthetic.
"""

from pathlib import Path

from cdss.checkpoint import (
    clear_checkpoint,
    column_profile_from_dict,
    load_checkpoint,
    save_checkpoint,
    view_context_from_view_dict,
)
from cdss.profiler import (
    ColumnProfile,
    ColumnSampling,
    ReferenceSamples,
    StringLengthStats,
    TopValue,
    ValuePatternStats,
)


def test_load_checkpoint_missing_file_returns_empty_defaults(tmp_path: Path) -> None:
    checkpoint = load_checkpoint(tmp_path / "does-not-exist.json")
    assert checkpoint == {
        "views": {},
        "already_evaluated_pairs": [],
    }


def test_save_and_load_checkpoint_round_trips(tmp_path: Path) -> None:
    path = tmp_path / ".profile-checkpoint.json"
    data = {
        "views": {"dbo.SyntheticView": {"qualified_name": "dbo.SyntheticView"}},
        "already_evaluated_pairs": [],
    }
    save_checkpoint(path, data)
    assert load_checkpoint(path) == data


def test_clear_checkpoint_removes_file(tmp_path: Path) -> None:
    path = tmp_path / ".profile-checkpoint.json"
    save_checkpoint(path, {"views": {}, "already_evaluated_pairs": []})
    clear_checkpoint(path)
    assert not path.exists()


def test_clear_checkpoint_missing_file_is_a_no_op(tmp_path: Path) -> None:
    clear_checkpoint(tmp_path / "does-not-exist.json")  # must not raise


def test_column_profile_from_dict_round_trips_full_profile() -> None:
    profile = ColumnProfile(
        column_name="SyntheticID",
        data_type="int",
        is_free_text=False,
        column_class="key",
        sampling=ColumnSampling(sampled=True, method="modulo:SyntheticID%10=0"),
        null_count=0,
        null_rate=0.0,
        distinct_count=100,
        min_value="1",
        max_value="100",
        top_values=[TopValue(value="1", frequency=5)],
        string_length_stats=StringLengthStats(min_length=1, max_length=3, avg_length=2.0),
        reference_samples=ReferenceSamples(values=["Alpha", "Beta"]),
        value_pattern_stats=ValuePatternStats(trailing_tag_counts={"(disorder)": 3}),
    )
    as_dict = {
        "column_name": profile.column_name,
        "data_type": profile.data_type,
        "is_free_text": profile.is_free_text,
        "column_class": profile.column_class,
        "sampling": {"sampled": profile.sampling.sampled, "method": profile.sampling.method},
        "null_count": profile.null_count,
        "null_rate": profile.null_rate,
        "distinct_count": profile.distinct_count,
        "min_value": profile.min_value,
        "max_value": profile.max_value,
        "top_values": [{"value": t.value, "frequency": t.frequency} for t in profile.top_values],
        "string_length_stats": {
            "min_length": profile.string_length_stats.min_length,
            "max_length": profile.string_length_stats.max_length,
            "avg_length": profile.string_length_stats.avg_length,
        },
        "reference_samples": {"values": profile.reference_samples.values, "sample_only": True},
        "value_pattern_stats": {
            "trailing_tag_counts": profile.value_pattern_stats.trailing_tag_counts
        },
    }
    assert column_profile_from_dict(as_dict) == profile


def test_column_profile_from_dict_handles_null_optional_fields() -> None:
    as_dict = {
        "column_name": "Notes",
        "data_type": "nvarchar",
        "is_free_text": True,
        "column_class": "identifier_or_freetext",
        "sampling": {"sampled": False, "method": "full"},
        "null_count": None,
        "null_rate": None,
        "distinct_count": None,
        "min_value": None,
        "max_value": None,
        "top_values": [],
        "string_length_stats": None,
        "reference_samples": None,
        "value_pattern_stats": None,
    }
    profile = column_profile_from_dict(as_dict)
    assert profile.string_length_stats is None
    assert profile.reference_samples is None
    assert profile.value_pattern_stats is None
    assert profile.top_values == []


def test_view_context_from_view_dict_reconstructs_row_count_and_columns() -> None:
    view_dict = {
        "qualified_name": "dbo.SyntheticView",
        "row_count": 100,
        "columns": [
            {
                "column_name": "SyntheticID",
                "data_type": "int",
                "is_free_text": False,
                "column_class": "key",
                "sampling": {"sampled": False, "method": "full"},
                "null_count": 0,
                "null_rate": 0.0,
                "distinct_count": 100,
                "min_value": "1",
                "max_value": "100",
                "top_values": [],
                "string_length_stats": None,
                "reference_samples": None,
                "value_pattern_stats": None,
            }
        ],
    }
    context = view_context_from_view_dict(view_dict)
    assert context.qualified_name == "dbo.SyntheticView"
    assert context.row_count == 100
    assert context.raw_columns == [("SyntheticID", "int", None)]
    assert len(context.profiles) == 1
    assert context.profiles[0].column_name == "SyntheticID"


def test_view_context_from_view_dict_handles_indeterminate_row_count() -> None:
    view_dict = {"qualified_name": "dbo.SyntheticView", "row_count": None, "columns": []}
    context = view_context_from_view_dict(view_dict)
    assert context.row_count == 0
    assert context.raw_columns == []
    assert context.profiles == []
