"""Phase 4 step 2: cdss.authoring.derive -- the profiling-derived check
generator (F3a). Pure tests run against an entirely synthetic fixture
catalog (fabricated view/column names, never real INDICI_BI_Full data,
same convention as tests/test_dsl.py). DB-gated persistence tests require
CDSS_APP_DB_URL and skip (never fail) otherwise -- D-009.1.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import sqlalchemy as sa

from cdss.authoring.derive import generate_draft_checks, persist_draft_checks

_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")


def _column(
    name: str,
    *,
    data_type: str = "varchar",
    column_class: str = "measure",
    distinct_count: int | None = 10,
    min_value: str | None = None,
    max_value: str | None = None,
    top_values: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "column_name": name,
        "data_type": data_type,
        "is_free_text": False,
        "column_class": column_class,
        "sampling": {"sampled": False, "method": "none"},
        "null_count": 0,
        "null_rate": 0.0,
        "distinct_count": distinct_count,
        "min_value": min_value,
        "max_value": max_value,
        "top_values": top_values or [],
        "string_length_stats": None,
        "reference_samples": None,
        "value_pattern_stats": None,
    }


def _view(
    qualified_name: str,
    *,
    columns: list[dict[str, Any]],
    candidate_keys: list[dict[str, Any]] | None = None,
    sentinels: list[dict[str, Any]] | None = None,
    row_count: int = 500,
) -> dict[str, Any]:
    return {
        "qualified_name": qualified_name,
        "row_count": row_count,
        "row_count_status": "exact",
        "archetype": "fact",
        "columns": columns,
        "candidate_keys": candidate_keys or [],
        "watermark_classification": {"status": "fallback_needed", "columns": []},
        "sentinels": sentinels or [],
        "test_record_indicators": [],
    }


def _catalog(*, views: list[dict[str, Any]], relationships: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "catalog_version": 1,
        "produced_at": "2026-01-01T00:00:00+00:00",
        "source_database": "TEST_DB",
        "views": views,
        "relationships": relationships,
        "profiling_costs": [],
        "pruning_report": {
            "pairs_considered": 0,
            "pairs_pruned": 0,
            "pairs_evaluated": 0,
            "pairs_skipped_cost": 0,
        },
    }


def _base_catalog() -> dict[str, Any]:
    """One fact view (dbo.FakeAppointments) referencing one reference view
    (dbo.FakeClinicType), carrying one of every evidence type this
    generator reacts to -- entirely synthetic, fabricated names."""
    appointments = _view(
        "dbo.FakeAppointments",
        row_count=500,
        columns=[
            _column("AppointmentID", data_type="int", column_class="key", distinct_count=500),
            _column("PracticeID", data_type="int", column_class="key", distinct_count=3),
            _column("ClinicTypeID", data_type="int", column_class="key", distinct_count=5),
            _column(
                "StatusCode",
                data_type="varchar",
                column_class="categorical_coded",
                distinct_count=3,
                top_values=[
                    {"value": "Booked", "frequency": 300},
                    {"value": "Completed", "frequency": 150},
                    {"value": "Cancelled", "frequency": 50},
                ],
            ),
            _column(
                "ScheduleDate",
                data_type="datetime2",
                column_class="measure",
                distinct_count=450,
                min_value="2020-01-01 00:00:00",
                max_value="2026-08-01 00:00:00",
            ),
            _column(
                "LegacyPatientRef",
                data_type="int",
                column_class="key",
                distinct_count=488,
            ),
        ],
        candidate_keys=[
            {
                "columns": ["AppointmentID"],
                "distinct_count": 500,
                "row_count": 500,
                "evidence_method": "exact",
            }
        ],
        sentinels=[
            {
                "column_name": "LegacyPatientRef",
                "sentinel_type": "zero_or_negative_id",
                "value": "0",
                "frequency": 12,
                "description": "legacy placeholder for un-migrated patient reference",
            }
        ],
    )
    clinic_type = _view(
        "dbo.FakeClinicType",
        row_count=5,
        columns=[
            _column("ClinicTypeID", data_type="int", column_class="key", distinct_count=5),
            _column("PracticeID", data_type="int", column_class="key", distinct_count=3),
            _column("ClinicTypeName", data_type="varchar", column_class="reference_vocabulary"),
        ],
        candidate_keys=[
            {
                "columns": ["ClinicTypeID"],
                "distinct_count": 5,
                "row_count": 5,
                "evidence_method": "exact",
            }
        ],
    )
    edge = {
        "from_view": "dbo.FakeAppointments",
        "from_column": "ClinicTypeID",
        "to_view": "dbo.FakeClinicType",
        "to_column": "ClinicTypeID",
        "status": "evaluated",
        "containment_a_to_b": 0.97,
        "containment_b_to_a": 1.0,
        "orphan_count_a": 3,
        "orphan_count_b": 0,
    }
    return _catalog(views=[appointments, clinic_type], relationships=[edge])


_GENERATION_NOW = datetime(2026, 7, 17, 12, 0, 0)


def test_generation_is_deterministic() -> None:
    catalog = _base_catalog()
    first = generate_draft_checks(catalog, now=_GENERATION_NOW)
    second = generate_draft_checks(catalog, now=_GENERATION_NOW)
    assert first == second
    assert [d.slug for d in first] == [d.slug for d in second]


def test_every_draft_id_matches_dsl_slug_pattern() -> None:
    drafts = generate_draft_checks(_base_catalog(), now=_GENERATION_NOW)
    assert drafts, "fixture catalog should yield at least one draft"
    for draft in drafts:
        assert _ID_PATTERN.match(draft.slug), f"slug '{draft.slug}' violates the DSL id pattern"
        assert draft.definition["id"] == draft.slug


def test_referential_check_generated_only_for_the_orphaned_direction() -> None:
    drafts = generate_draft_checks(_base_catalog(), now=_GENERATION_NOW)
    referential = [d for d in drafts if d.category == "referential"]
    assert len(referential) == 1
    draft = referential[0]
    assert draft.definition["entity"]["view"] == "dbo.FakeAppointments"
    assert draft.definition["entity"]["key"] == ["AppointmentID"]
    assert draft.definition["predicate"]["not_exists"]["view"] == "dbo.FakeClinicType"
    assert "3" in draft.rationale
    assert "0.9700" in draft.rationale


def test_no_referential_check_when_no_orphans_in_either_direction() -> None:
    catalog = _base_catalog()
    catalog["relationships"][0]["orphan_count_a"] = 0
    catalog["relationships"][0]["orphan_count_b"] = 0
    drafts = generate_draft_checks(catalog, now=_GENERATION_NOW)
    assert not [d for d in drafts if d.category == "referential"]


def test_no_referential_check_for_a_skipped_cost_edge() -> None:
    catalog = _base_catalog()
    catalog["relationships"][0]["status"] = "skipped_cost"
    catalog["relationships"][0]["orphan_count_a"] = None
    catalog["relationships"][0]["orphan_count_b"] = None
    drafts = generate_draft_checks(catalog, now=_GENERATION_NOW)
    assert not [d for d in drafts if d.category == "referential"]


def test_no_referential_check_when_the_orphaned_side_is_a_reference_view() -> None:
    # Live bug (Phase 4 step 7, first real run): a relationship edge can have
    # orphans on the side that happens to be a reference/dictionary view
    # (dbo.Disease has FKs pointing INTO it that are missing, i.e. orphans on
    # its own "a" side of some edge) -- that must never become the check's
    # entity (no PracticeID column). The other direction (fact-side orphans)
    # must still generate normally.
    catalog = _base_catalog()
    catalog["views"][1]["archetype"] = "reference"  # dbo.FakeClinicType
    # orphan_count_a is already 3 (dbo.FakeAppointments, a fact view) --
    # unaffected. Flip orphan_count_b onto the now-reference view too, to
    # prove that direction is suppressed.
    catalog["relationships"][0]["orphan_count_b"] = 2
    catalog["relationships"][0]["containment_b_to_a"] = 0.9

    drafts = generate_draft_checks(catalog, now=_GENERATION_NOW)

    referential = [d for d in drafts if d.category == "referential"]
    assert len(referential) == 1
    assert referential[0].definition["entity"]["view"] == "dbo.FakeAppointments"


def test_sentinel_generates_a_numeric_placeholder_value_check() -> None:
    drafts = generate_draft_checks(_base_catalog(), now=_GENERATION_NOW)
    sentinel_drafts = [d for d in drafts if "zero-or-negative-id" in d.slug]
    assert len(sentinel_drafts) == 1
    draft = sentinel_drafts[0]
    assert draft.definition["predicate"] == "LegacyPatientRef = 0"
    assert "12" in draft.rationale


def test_sentinel_generates_a_quoted_string_placeholder_value_check() -> None:
    catalog = _base_catalog()
    catalog["views"][0]["columns"].append(
        _column("LegacyStatusText", data_type="varchar", column_class="key")
    )
    catalog["views"][0]["sentinels"].append(
        {
            "column_name": "LegacyStatusText",
            "sentinel_type": "magic_value",
            "value": "N/A",
            "frequency": 4,
            "description": "legacy magic string",
        }
    )
    drafts = generate_draft_checks(catalog, now=_GENERATION_NOW)
    draft = next(d for d in drafts if "magic-value" in d.slug)
    assert draft.definition["predicate"] == "LegacyStatusText = 'N/A'"


def test_enum_check_generated_for_categorical_column_with_enough_domain_values() -> None:
    drafts = generate_draft_checks(_base_catalog(), now=_GENERATION_NOW)
    enum_drafts = [d for d in drafts if "invalid-domain-value" in d.slug]
    assert len(enum_drafts) == 1
    draft = enum_drafts[0]
    param_name = next(iter(draft.definition["params"]))
    assert draft.definition["params"][param_name]["default"]["value"] == [
        "Booked",
        "Completed",
        "Cancelled",
    ]
    assert draft.definition["predicate"] == {"not": f"StatusCode IN ({{{param_name}}})"}


def test_no_enum_check_below_the_minimum_domain_size() -> None:
    catalog = _base_catalog()
    catalog["views"][0]["columns"][3]["top_values"] = [{"value": "Booked", "frequency": 300}]
    drafts = generate_draft_checks(catalog, now=_GENERATION_NOW)
    assert not [d for d in drafts if "invalid-domain-value" in d.slug]


def test_range_check_generated_when_max_value_already_in_the_future() -> None:
    drafts = generate_draft_checks(_base_catalog(), now=_GENERATION_NOW)
    range_drafts = [d for d in drafts if "impossible-future-date" in d.slug]
    assert len(range_drafts) == 1
    draft = range_drafts[0]
    assert draft.definition["predicate"] == "ScheduleDate > sysdatetime()"
    assert "2026-08-01" in draft.rationale


def test_no_range_check_when_max_value_is_not_in_the_future() -> None:
    catalog = _base_catalog()
    catalog["views"][0]["columns"][4]["max_value"] = "2020-06-01 00:00:00"
    drafts = generate_draft_checks(catalog, now=_GENERATION_NOW)
    assert not [d for d in drafts if "impossible-future-date" in d.slug]


def test_no_range_check_for_a_non_measure_column() -> None:
    catalog = _base_catalog()
    catalog["views"][0]["columns"][4]["column_class"] = "key"
    drafts = generate_draft_checks(catalog, now=_GENERATION_NOW)
    assert not [d for d in drafts if "impossible-future-date" in d.slug]


def test_duplicate_check_generated_for_a_candidate_key() -> None:
    drafts = generate_draft_checks(_base_catalog(), now=_GENERATION_NOW)
    duplicate_drafts = [d for d in drafts if d.slug.endswith("-duplicate")]
    assert len(duplicate_drafts) == 2  # one per view's single-column candidate key
    appointment_dup = next(d for d in duplicate_drafts if "appointments" in d.slug)
    assert appointment_dup.definition["predicate"] == (
        "COUNT(*) OVER (PARTITION BY dbo.FakeAppointments.AppointmentID) > 1"
    )
    assert appointment_dup.definition["entity"]["key"] == ["AppointmentID"]


def test_no_drafts_generated_for_a_reference_archetype_view() -> None:
    # Live bug (Phase 4 step 7, first real run against semantic-catalog-v3.json):
    # dbo.Disease is archetype="reference" and has no PracticeID column at
    # all -- generating a sentinel/enum/duplicate draft against it raised
    # CheckReferenceError ("unknown column 'PracticeID'"). A reference view
    # must never be a check's own entity, only a join target.
    catalog = _base_catalog()
    reference_view = _view(
        "dbo.FakeReferenceVocab",
        columns=[
            _column(
                "VocabCode",
                data_type="varchar",
                column_class="categorical_coded",
                distinct_count=3,
                top_values=[
                    {"value": "A", "frequency": 10},
                    {"value": "B", "frequency": 5},
                ],
            ),
        ],
        candidate_keys=[
            {
                "columns": ["VocabCode"],
                "distinct_count": 3,
                "row_count": 3,
                "evidence_method": "exact",
            }
        ],
        sentinels=[
            {
                "column_name": "VocabCode",
                "sentinel_type": "magic_value",
                "value": "ZZZ",
                "frequency": 1,
                "description": "placeholder",
            }
        ],
    )
    reference_view["archetype"] = "reference"
    catalog["views"].append(reference_view)

    drafts = generate_draft_checks(catalog, now=_GENERATION_NOW)

    assert not [d for d in drafts if "reference-vocab" in d.slug.lower()]
    assert not [d for d in drafts if d.definition["entity"]["view"] == "dbo.FakeReferenceVocab"]


def test_every_generated_draft_is_schema_and_catalog_valid() -> None:
    # generate_draft_checks self-validates internally (check_doc_from_dict +
    # validate_check_against_catalog) and would already have raised if any
    # draft were malformed -- this just proves it actually ran and produced
    # a non-trivial set covering every evidence type.
    drafts = generate_draft_checks(_base_catalog(), now=_GENERATION_NOW)
    categories = {d.category for d in drafts}
    assert categories == {"referential", "data-quality"}
    assert len(drafts) == 6  # 1 referential + 1 sentinel + 1 enum + 1 range + 2 duplicate


# --- persistence (DB-gated) --------------------------------------------------


def test_persist_draft_checks_inserts_with_profiling_source_and_draft_status(
    conn: sa.Connection,
) -> None:
    drafts = generate_draft_checks(_base_catalog(), now=_GENERATION_NOW)
    inserted_ids = persist_draft_checks(conn, drafts)
    assert len(inserted_ids) == len(drafts)
    rows = conn.execute(
        sa.text("SELECT slug, source, status FROM checks WHERE id = ANY(:ids)"),
        {"ids": inserted_ids},
    ).all()
    assert len(rows) == len(drafts)
    for row in rows:
        assert row.source == "profiling"
        assert row.status == "draft"


def test_persist_draft_checks_writes_rationale_and_affected_views(conn: sa.Connection) -> None:
    drafts = generate_draft_checks(_base_catalog(), now=_GENERATION_NOW)
    referential = next(d for d in drafts if d.category == "referential")
    persist_draft_checks(conn, [referential])
    row = conn.execute(
        sa.text(
            "SELECT cv.rationale, cv.affected_views, cv.version_number "
            "FROM check_versions cv JOIN checks c ON c.id = cv.check_id "
            "WHERE c.slug = :slug"
        ),
        {"slug": referential.slug},
    ).one()
    assert row.rationale == referential.rationale
    assert set(row.affected_views) == set(referential.affected_views)
    assert row.version_number == 1


def test_persist_draft_checks_is_idempotent_by_slug(conn: sa.Connection) -> None:
    drafts = generate_draft_checks(_base_catalog(), now=_GENERATION_NOW)
    first_ids = persist_draft_checks(conn, drafts)
    second_ids = persist_draft_checks(conn, drafts)
    assert second_ids == []
    count = conn.execute(
        sa.text("SELECT count(*) FROM checks WHERE id = ANY(:ids)"), {"ids": first_ids}
    ).scalar_one()
    assert count == len(drafts)
