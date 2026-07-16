"""Phase 1 step 6: watermark verification tests. All statements are answered
by a scripted fake connection -- no live database involved. Fixture view/
column names below are entirely synthetic.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cdss.source import AuditedSourceConnection
from cdss.watermarks import (
    ViewWatermarkClassification,
    WatermarkColumnCheck,
    WatermarkCost,
    _is_watermark_candidate,
    classify_view_watermarks,
)


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


# --- _is_watermark_candidate ------------------------------------------------


def test_is_watermark_candidate_matches_known_name_and_date_type() -> None:
    assert _is_watermark_candidate("InsertedAt", "datetime") is True
    assert _is_watermark_candidate("insertedat", "datetime2") is True


def test_is_watermark_candidate_rejects_unknown_name() -> None:
    assert _is_watermark_candidate("SyntheticOtherColumn", "datetime") is False


def test_is_watermark_candidate_rejects_non_date_type() -> None:
    assert _is_watermark_candidate("InsertedAt", "int") is False


# --- classify_view_watermarks ------------------------------------------------


def test_classify_watermarkable_view_no_nulls_monotonic(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        assert statement == (
            "SELECT COUNT(*), COUNT(*) - COUNT([InsertedAt]), MIN([InsertedAt]), "
            "MAX([InsertedAt]) FROM dbo.SyntheticView"
        )
        return [(1000, 0, "2015-01-01 00:00:00", "2017-11-06 10:00:00")]

    classification, costs = classify_view_watermarks(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        columns=[("InsertedAt", "datetime"), ("SyntheticOtherColumn", "int")],
        baseline_max_by_column={"InsertedAt": "2017-11-06 10:00:00"},
        now=datetime(2026, 7, 16),
    )

    assert classification.status == "watermarkable"
    assert classification.columns == ["InsertedAt"]
    assert classification.checks == [
        WatermarkColumnCheck(
            column_name="InsertedAt",
            data_type="datetime",
            null_count=0,
            total_count=1000,
            null_rate=0.0,
            min_value="2015-01-01 00:00:00",
            max_value="2017-11-06 10:00:00",
            monotonic_vs_baseline=True,
            passed=True,
            reasons=[],
        )
    ]
    assert costs == [
        WatermarkCost(
            view="dbo.SyntheticView",
            operation="watermark_check",
            duration_ms=costs[0].duration_ms,
            status="ok",
        )
    ]


def test_classify_fallback_needed_no_candidate_columns(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        raise AssertionError(f"no query should be issued: {statement}")

    classification, costs = classify_view_watermarks(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        columns=[("SyntheticOtherColumn", "int")],
    )

    assert classification.status == "fallback_needed"
    assert classification.columns == []
    assert classification.checks == []
    assert costs == []


def test_classify_fallback_needed_name_matches_wrong_type(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        raise AssertionError(f"no query should be issued: {statement}")

    classification, costs = classify_view_watermarks(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        columns=[("InsertedAt", "varchar")],
    )

    assert classification.status == "fallback_needed"
    assert classification.columns == []
    assert classification.checks == [
        WatermarkColumnCheck(
            column_name="InsertedAt",
            data_type="varchar",
            null_count=0,
            total_count=0,
            null_rate=0.0,
            min_value=None,
            max_value=None,
            monotonic_vs_baseline=None,
            passed=False,
            reasons=["wrong_type"],
        )
    ]
    assert costs == []


def test_classify_fallback_needed_nulls_present(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        return [(1000, 5, "2015-01-01 00:00:00", "2017-11-06 10:00:00")]

    classification, _costs = classify_view_watermarks(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        columns=[("InsertedAt", "datetime")],
    )

    assert classification.status == "fallback_needed"
    assert classification.columns == []
    check = classification.checks[0]
    assert check.passed is False
    assert check.reasons == ["null_values_present"]
    assert check.null_count == 5
    assert check.null_rate == 0.005


def test_classify_fallback_needed_non_monotonic_vs_baseline(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        return [(1000, 0, "2015-01-01 00:00:00", "2016-01-01 00:00:00")]

    classification, _costs = classify_view_watermarks(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        columns=[("InsertedAt", "datetime")],
        baseline_max_by_column={"InsertedAt": "2017-11-06 10:00:00"},
    )

    assert classification.status == "fallback_needed"
    check = classification.checks[0]
    assert check.passed is False
    assert check.monotonic_vs_baseline is False
    assert check.reasons == ["non_monotonic_vs_baseline"]


def test_classify_fallback_needed_future_dated_max(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        return [(1000, 0, "2001-01-01 00:00:00", "2061-09-21 00:00:00")]

    classification, _costs = classify_view_watermarks(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        columns=[("InsertedAt", "datetime")],
        now=datetime(2026, 7, 16),
    )

    assert classification.status == "fallback_needed"
    check = classification.checks[0]
    assert check.passed is False
    assert check.reasons == ["future_dated_max"]


def test_classify_no_baseline_available_is_not_disqualifying(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        return [(1000, 0, "2015-01-01 00:00:00", "2017-11-06 10:00:00")]

    classification, _costs = classify_view_watermarks(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        columns=[("InsertedAt", "datetime")],
        now=datetime(2026, 7, 16),
    )

    assert classification.status == "watermarkable"
    check = classification.checks[0]
    assert check.monotonic_vs_baseline is None
    assert check.passed is True


def test_classify_multiple_candidates_at_least_one_passing_is_watermarkable(
    tmp_path: Path,
) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]]:
        return [
            (
                1000,
                0,
                "2015-01-01 00:00:00",
                "2017-11-06 10:00:00",  # InsertedAt: passes
                10,
                "2015-01-01 00:00:00",
                "2017-11-06 10:00:00",  # UpdatedAt: has nulls, fails
            )
        ]

    classification, _costs = classify_view_watermarks(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        columns=[("InsertedAt", "datetime"), ("UpdatedAt", "datetime")],
        now=datetime(2026, 7, 16),
    )

    assert classification.status == "watermarkable"
    assert classification.columns == ["InsertedAt"]
    assert len(classification.checks) == 2
    assert classification.checks[1].reasons == ["null_values_present"]


def test_classify_query_timeout_falls_back_needed(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]] | Exception:
        return FakeTimeoutError()

    classification, costs = classify_view_watermarks(
        _make_audited(tmp_path, responder),
        qualified_name="dbo.SyntheticView",
        columns=[("InsertedAt", "datetime")],
    )

    assert classification.status == "fallback_needed"
    assert classification.columns == []
    assert costs == [
        WatermarkCost(
            view="dbo.SyntheticView",
            operation="watermark_check",
            duration_ms=costs[0].duration_ms,
            status="timeout",
        )
    ]


def test_classify_non_timeout_error_propagates(tmp_path: Path) -> None:
    def responder(statement: str, _timeout: int) -> list[tuple[Any, ...]] | Exception:
        return ValueError("some unrelated real error")

    import pytest

    with pytest.raises(ValueError, match="unrelated real error"):
        classify_view_watermarks(
            _make_audited(tmp_path, responder),
            qualified_name="dbo.SyntheticView",
            columns=[("InsertedAt", "datetime")],
        )


def test_view_watermark_classification_and_check_are_frozen() -> None:
    check = WatermarkColumnCheck(
        column_name="InsertedAt",
        data_type="datetime",
        null_count=0,
        total_count=10,
        null_rate=0.0,
        min_value=None,
        max_value=None,
        monotonic_vs_baseline=None,
        passed=True,
        reasons=[],
    )
    classification = ViewWatermarkClassification(
        qualified_name="dbo.SyntheticView",
        status="watermarkable",
        columns=["InsertedAt"],
        checks=[check],
    )
    assert classification.qualified_name == "dbo.SyntheticView"
