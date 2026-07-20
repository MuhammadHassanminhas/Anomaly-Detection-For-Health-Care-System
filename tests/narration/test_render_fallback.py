"""Phase 5 step 1: cdss.narrate.render_fallback -- pure interpolation logic,
no DB involved. DB-gated proof that the wiring (migration + real
check_versions writers + real fixture-DB evidence) actually works lives in
test_fallback_template.py.
"""

from __future__ import annotations

import datetime as dt

import pytest

from cdss.narrate import UnknownPlaceholderError, render_fallback


def test_substitutes_from_evidence() -> None:
    rendered = render_fallback(
        "Invoice {{InvoiceTransactionID}} has a negative total.",
        evidence={"InvoiceTransactionID": "INV-1"},
        params={},
    )
    assert rendered == "Invoice INV-1 has a negative total."


def test_falls_back_to_params_on_evidence_miss() -> None:
    rendered = render_fallback(
        "Overdue by more than {{stale_days}} days.",
        evidence={"InvoiceTransactionID": "INV-1"},
        params={"stale_days": 60},
    )
    assert rendered == "Overdue by more than 60 days."


def test_evidence_wins_on_name_collision() -> None:
    rendered = render_fallback(
        "{{stale_days}}",
        evidence={"stale_days": 5},
        params={"stale_days": 60},
    )
    assert rendered == "5"


def test_formats_datetime_as_iso8601() -> None:
    rendered = render_fallback(
        "Dated {{InvoiceDate}}.",
        evidence={"InvoiceDate": dt.datetime(2026, 3, 3, 10, 30)},
        params={},
    )
    assert rendered == "Dated 2026-03-03T10:30:00."


def test_formats_date_as_iso8601() -> None:
    rendered = render_fallback("Dated {{d}}.", evidence={"d": dt.date(2026, 3, 3)}, params={})
    assert rendered == "Dated 2026-03-03."


def test_unknown_placeholder_raises() -> None:
    with pytest.raises(UnknownPlaceholderError) as exc_info:
        render_fallback("{{not_a_real_field}}", evidence={}, params={})
    assert exc_info.value.field == "not_a_real_field"


def test_template_with_no_placeholders_passes_through_unchanged() -> None:
    rendered = render_fallback(
        "This check has flagged a record for manual review.", evidence={}, params={}
    )
    assert rendered == "This check has flagged a record for manual review."
