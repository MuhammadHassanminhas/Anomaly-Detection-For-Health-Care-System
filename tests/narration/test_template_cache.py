"""Phase 5 step 5: cdss.narrate.TemplateCache -- F10, LLM calls are
O(active checks), not O(findings). Pure logic, no DB involved. This is the
step's own named deliverable: N findings of one check make exactly 1 LLM
call; a check-version bump makes exactly 1 more.
"""

from __future__ import annotations

import json

from cdss.narrate import TemplateCache, compose, evidence_shape_hash

_DEFINITION = {
    "id": "invoice-negative-total-amount",
    "category": "data-quality",
    "evidence": ["InvoiceTransactionID", "TotalAmount"],
    "actions": ["flag-for-data-steward-review"],
    "resolution": "TotalAmount is corrected, or the finding is dismissed with a reason.",
    "params": {},
}
_FALLBACK_TEMPLATE = "This check has flagged a record for manual review."
_VALID_RESPONSE = json.dumps(
    {
        "template": "Invoice {{InvoiceTransactionID}} has a negative total of {{TotalAmount}}.",
        "actions": ["flag-for-data-steward-review"],
    }
)


class CountingLLMClient:
    """Returns the same canned response every call -- since a cache hit
    should mean this is never called more than once per (check_version,
    evidence-shape), a second identically-shaped call reaching it would be
    the bug, not the fixture."""

    def __init__(self, response: str = _VALID_RESPONSE) -> None:
        self.call_count = 0
        self._response = response

    def complete(self, prompt: str) -> str:
        self.call_count += 1
        return self._response


def _compose(
    client: CountingLLMClient,
    cache: TemplateCache,
    *,
    check_version_id: str,
    evidence: dict[str, object],
) -> str:
    result = compose(
        client,
        model_id="gpt-4o-mini",
        check_version_id=check_version_id,
        definition=_DEFINITION,
        rationale="TotalAmount should never be negative.",
        fallback_template=_FALLBACK_TEMPLATE,
        evidence=evidence,
        params={},
        env={"CDSS_ENV": "test"},
        cache=cache,
    )
    assert result.validation_status == "valid"
    return result.rendered


def test_n_findings_of_one_check_make_exactly_one_llm_call() -> None:
    client = CountingLLMClient()
    cache = TemplateCache()
    rendered_texts = [
        _compose(
            client,
            cache,
            check_version_id="version-1",
            evidence={"InvoiceTransactionID": f"INV-{i}", "TotalAmount": "-12.50"},
        )
        for i in range(10)
    ]
    assert client.call_count == 1
    # each finding's own real value is still rendered correctly -- the
    # cache reuses the *template*, never a stale rendered string
    assert rendered_texts == [f"Invoice INV-{i} has a negative total of -12.50." for i in range(10)]


def test_a_check_version_bump_makes_exactly_one_more_llm_call() -> None:
    client = CountingLLMClient()
    cache = TemplateCache()
    for _ in range(5):
        _compose(
            client,
            cache,
            check_version_id="version-1",
            evidence={"InvoiceTransactionID": "INV-1", "TotalAmount": "-12.50"},
        )
    assert client.call_count == 1

    for _ in range(5):
        _compose(
            client,
            cache,
            check_version_id="version-2",
            evidence={"InvoiceTransactionID": "INV-1", "TotalAmount": "-12.50"},
        )
    assert client.call_count == 2


def test_a_differently_shaped_evidence_dict_makes_one_more_llm_call() -> None:
    client = CountingLLMClient()
    cache = TemplateCache()
    _compose(
        client,
        cache,
        check_version_id="version-1",
        evidence={"InvoiceTransactionID": "INV-1", "TotalAmount": "-12.50"},
    )
    assert client.call_count == 1

    # TotalAmount is a real number here instead of a string -- a different
    # evidence *shape* (type changes), same field names
    result = compose(
        client,
        model_id="gpt-4o-mini",
        check_version_id="version-1",
        definition=_DEFINITION,
        rationale="TotalAmount should never be negative.",
        fallback_template=_FALLBACK_TEMPLATE,
        evidence={"InvoiceTransactionID": "INV-2", "TotalAmount": -12.5},
        params={},
        env={"CDSS_ENV": "test"},
        cache=cache,
    )
    assert result.validation_status == "valid"
    assert client.call_count == 2


def test_without_a_cache_every_call_hits_the_llm() -> None:
    client = CountingLLMClient()
    for i in range(5):
        result = compose(
            client,
            model_id="gpt-4o-mini",
            check_version_id="version-1",
            definition=_DEFINITION,
            rationale="TotalAmount should never be negative.",
            fallback_template=_FALLBACK_TEMPLATE,
            evidence={"InvoiceTransactionID": f"INV-{i}", "TotalAmount": "-12.50"},
            params={},
            env={"CDSS_ENV": "test"},
        )
        assert result.validation_status == "valid"
    assert client.call_count == 5


def test_a_fallback_result_is_never_cached_so_the_next_finding_retries_the_llm() -> None:
    # first response is garbage (falls back, nothing cached); second is valid
    client = CountingLLMClient()
    client._response = "not json {{{"  # type: ignore[attr-defined]
    cache = TemplateCache()

    first = compose(
        client,
        model_id="gpt-4o-mini",
        check_version_id="version-1",
        definition=_DEFINITION,
        rationale="TotalAmount should never be negative.",
        fallback_template=_FALLBACK_TEMPLATE,
        evidence={"InvoiceTransactionID": "INV-1", "TotalAmount": "-12.50"},
        params={},
        env={"CDSS_ENV": "test"},
        cache=cache,
    )
    assert first.validation_status == "fallback_static"
    assert client.call_count == 1
    assert len(cache) == 0

    client._response = _VALID_RESPONSE  # type: ignore[attr-defined]
    second = compose(
        client,
        model_id="gpt-4o-mini",
        check_version_id="version-1",
        definition=_DEFINITION,
        rationale="TotalAmount should never be negative.",
        fallback_template=_FALLBACK_TEMPLATE,
        evidence={"InvoiceTransactionID": "INV-2", "TotalAmount": "-9.00"},
        params={},
        env={"CDSS_ENV": "test"},
        cache=cache,
    )
    assert second.validation_status == "valid"
    assert client.call_count == 2
    assert len(cache) == 1


def test_a_cache_hit_that_fails_revalidation_falls_back_instead_of_serving_stale_trust() -> None:
    # Every cache hit is re-rendered and re-validated against *this* call's
    # own inputs (never served on trust alone) -- this is what actually
    # catches a caller passing a mismatched `definition` for the same
    # `check_version_id` between two calls (the pairing is the caller's
    # responsibility; compose() cannot enforce it, only defend against it).
    client = CountingLLMClient()
    cache = TemplateCache()
    _compose(
        client,
        cache,
        check_version_id="version-1",
        evidence={"InvoiceTransactionID": "INV-1", "TotalAmount": "-12.50"},
    )
    assert client.call_count == 1
    assert len(cache) == 1

    narrower_definition = {**_DEFINITION, "actions": []}  # no longer allows the cached action
    result = compose(
        client,
        model_id="gpt-4o-mini",
        check_version_id="version-1",
        definition=narrower_definition,
        rationale="TotalAmount should never be negative.",
        fallback_template=_FALLBACK_TEMPLATE,
        evidence={"InvoiceTransactionID": "INV-2", "TotalAmount": "-9.00"},
        params={},
        env={"CDSS_ENV": "test"},
        cache=cache,
    )
    assert result.validation_status == "blocked_fallback"
    assert result.rendered == _FALLBACK_TEMPLATE
    # the LLM itself was never called again -- this was caught by
    # revalidation on the cache-hit path, not by a second draft attempt
    assert client.call_count == 1


# --- evidence_shape_hash -------------------------------------------------


def test_evidence_shape_hash_ignores_values_but_not_types_or_field_names() -> None:
    assert evidence_shape_hash({"a": "x"}) == evidence_shape_hash({"a": "y"})
    assert evidence_shape_hash({"a": "x"}) != evidence_shape_hash({"a": 1})
    assert evidence_shape_hash({"a": "x"}) != evidence_shape_hash({"b": "x"})
    assert evidence_shape_hash({}) == evidence_shape_hash({})


def test_evidence_shape_hash_is_order_independent() -> None:
    assert evidence_shape_hash({"a": "x", "b": 1}) == evidence_shape_hash({"b": 2, "a": "y"})
