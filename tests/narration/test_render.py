"""Phase 5 step 2: cdss.narrate.render -- typed formatting + the provenance
map. Pure logic, no DB involved (same class as test_render_fallback.py).
"""

from __future__ import annotations

import datetime as dt
import decimal

import pytest

from cdss.narrate import ProvenanceEntry, UnknownPlaceholderError, render


def test_text_matches_render_fallback_style_substitution() -> None:
    result = render(
        "Invoice {{InvoiceTransactionID}} has a negative total.",
        evidence={"InvoiceTransactionID": "INV-1"},
        params={},
    )
    assert result.text == "Invoice INV-1 has a negative total."


def test_datetime_formatted_iso8601() -> None:
    result = render(
        "Dated {{InvoiceDate}}.",
        evidence={"InvoiceDate": dt.datetime(2026, 3, 3, 10, 30)},
        params={},
    )
    assert result.text == "Dated 2026-03-03T10:30:00."


def test_date_formatted_iso8601() -> None:
    result = render("Dated {{d}}.", evidence={"d": dt.date(2026, 3, 3)}, params={})
    assert result.text == "Dated 2026-03-03."


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (decimal.Decimal("123.45"), "123.45"),
        (decimal.Decimal("-10.00"), "-10.00"),
        (decimal.Decimal("1E+3"), "1000"),
        (decimal.Decimal("1E-3"), "0.001"),
    ],
)
def test_decimal_formatted_in_fixed_notation_never_scientific(
    value: decimal.Decimal, expected: str
) -> None:
    result = render("{{amount}}", evidence={"amount": value}, params={})
    assert result.text == expected
    assert "E" not in result.text and "e" not in result.text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (5.0, "5.0"),
        (1e21, "1000000000000000000000"),
        (1e-5, "0.00001"),
    ],
)
def test_float_formatted_in_fixed_notation_never_scientific(value: float, expected: str) -> None:
    result = render("{{amount}}", evidence={"amount": value}, params={})
    assert result.text == expected
    assert "E" not in result.text and "e" not in result.text


def test_unknown_placeholder_raises() -> None:
    with pytest.raises(UnknownPlaceholderError) as exc_info:
        render("{{not_a_real_field}}", evidence={}, params={})
    assert exc_info.value.field == "not_a_real_field"


def test_falls_back_to_params_and_records_params_as_the_source() -> None:
    result = render(
        "Overdue by more than {{stale_days}} days.",
        evidence={},
        params={"stale_days": 60},
    )
    assert result.text == "Overdue by more than 60 days."
    assert result.provenance == [
        ProvenanceEntry(placeholder="stale_days", source="params", start=21, end=23)
    ]


def test_evidence_wins_on_collision_and_is_recorded_as_the_source() -> None:
    result = render("{{stale_days}}", evidence={"stale_days": 5}, params={"stale_days": 60})
    assert result.text == "5"
    assert result.provenance == [
        ProvenanceEntry(placeholder="stale_days", source="evidence", start=0, end=1)
    ]


# --- property-style round-trip: every provenance span traces back to its
# source, for every placeholder, across many template shapes and value
# types (not a single hand-picked case) -----------------------------------

_ROUND_TRIP_CASES: list[tuple[str, dict[str, object], dict[str, object]]] = [
    (
        "Invoice {{InvoiceTransactionID}} at practice {{PracticeID}} has a negative "
        "total amount of {{TotalAmount}}, dated {{InvoiceDate}}.",
        {
            "InvoiceTransactionID": "INV-42",
            "PracticeID": 7,
            "TotalAmount": decimal.Decimal("-12.50"),
            "InvoiceDate": dt.datetime(2026, 1, 5, 9, 0),
        },
        {},
    ),
    ("{{a}}{{b}}{{c}}", {"a": "x", "b": "y"}, {"c": 3}),
    ("no placeholders here", {}, {}),
    ("{{only}}", {}, {"only": 1.5}),
    (
        "{{x}} and {{x}} again",
        {"x": "same"},
        {},
    ),
]


@pytest.mark.parametrize(("template", "evidence", "params"), _ROUND_TRIP_CASES)
def test_every_provenance_span_traces_back_to_its_declared_source(
    template: str, evidence: dict[str, object], params: dict[str, object]
) -> None:
    result = render(template, evidence=evidence, params=params)
    for entry in result.provenance:
        span_text = result.text[entry.start : entry.end]
        source_map = evidence if entry.source == "evidence" else params
        assert entry.placeholder in source_map
        raw_value = source_map[entry.placeholder]
        assert isinstance(raw_value, (str, int, float, decimal.Decimal, dt.date, dt.datetime))
        if isinstance(raw_value, (dt.datetime, dt.date)):
            expected = raw_value.isoformat()
        elif isinstance(raw_value, decimal.Decimal):
            expected = format(raw_value, "f")
        elif isinstance(raw_value, float):
            expected = format(decimal.Decimal(str(raw_value)), "f")
        else:
            expected = str(raw_value)
        assert span_text == expected


@pytest.mark.parametrize(("template", "evidence", "params"), _ROUND_TRIP_CASES)
def test_provenance_spans_never_overlap_and_stay_within_bounds(
    template: str, evidence: dict[str, object], params: dict[str, object]
) -> None:
    result = render(template, evidence=evidence, params=params)
    previous_end = 0
    for entry in result.provenance:
        assert entry.start >= previous_end
        assert entry.end <= len(result.text)
        assert entry.start <= entry.end
        previous_end = entry.end
