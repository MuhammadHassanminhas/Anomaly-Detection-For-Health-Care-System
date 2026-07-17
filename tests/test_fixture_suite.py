"""Phase 4 step 5: per-check fixture suite. Dry-runs every one of the 12 real
LLM-drafted checks from step 4 (embedded here verbatim -- the app DB itself
no longer holds them, see PROJECT_STATE.md/SESSION.md) against the widened
LocalDB fixture (scripts/fixture_db_setup.sql) and asserts each either clears
`cdss.review.check_passes_fixture_test`'s bar or doesn't.

Only requires the fixture SQL Server (`fixture_conn`, D-026) -- `dry_run`
never touches the app DB. Skips (never fails) if unreachable, D-009.1.

Three checks are *expected* to fail their fixture test -- not a fixture-data
gap, a genuine authoring defect this exact mechanism exists to catch before
a human reviewer ever sees a superficially-reasonable check:
`appointment-cancelled-but-no-flag` and `lapsed-health-card-check`/
`overdue-enrollment-expiry-check` all author a self-referencing
`exists`/`not_exists` clause (the same view as both the driving and the
joined side) -- `cdss.compiler`'s `ExistsClause` SQL emission has no
aliasing mechanism for that, so it either produces ambiguous SQL or a
population-wide-uniform (non-correlated) answer that can never split into
both `fail` and `pass` across different rows.
"""

from __future__ import annotations

from typing import Any

import pyodbc

from cdss.dsl import check_doc_from_dict, collect_joined_views
from cdss.review import CheckDetail, can_reach_indeterminate, check_passes_fixture_test, dry_run

_BASE_PATIENT_ENTITY = {
    "key": ["ProfileID"],
    "view": "dbo.Patient",
    "practice_column": "PracticeID",
}
_BASE_APPOINTMENT_ENTITY = {
    "key": ["AppointmentID"],
    "view": "dbo.Appointments",
    "practice_column": "PracticeID",
}

# The 12 real checks generated live in Phase 4 step 4 (source='llm'),
# embedded verbatim from the exported record -- see PROJECT_STATE.md.
_REAL_CHECKS: dict[str, dict[str, Any]] = {
    "active-patient-no-invoices": {
        "id": "active-patient-no-invoices",
        "title": "Active patient with no invoices",
        "category": "workflow",
        "default_severity": "medium",
        "entity": {**_BASE_PATIENT_ENTITY, "base_filters": ["IsActive = 1", "IsDeleted = 0"]},
        "params": {},
        "prerequisites": [],
        "predicate": {
            "not_exists": {
                "on": "fqb.Invoices.PatientID = dbo.Patient.ProfileID",
                "view": "fqb.Invoices",
            }
        },
        "evidence": ["ProfileID", "IsActive", "PracticeID"],
        "actions": ["raise-billing-task"],
        "resolution": "Investigate missing invoices for the active patient.",
    },
    "appointment-activity-left-open": {
        "id": "appointment-activity-left-open",
        "title": "Appointment left in 'On Hold' status",
        "category": "workflow",
        "default_severity": "medium",
        "entity": {**_BASE_APPOINTMENT_ENTITY, "base_filters": ["IsDeleted = 0"]},
        "params": {},
        "prerequisites": ["AppointmentCompleted IS NULL"],
        "predicate": {"all": ["AppointmentStatus = 'On Hold'", "WaitingForpayment IS NULL"]},
        "evidence": ["AppointmentID", "AppointmentStatus", "PracticeID"],
        "actions": ["chase-result"],
        "resolution": "Follow up on the appointment to conclude the necessary actions.",
    },
    "appointment-cancelled-but-no-flag": {
        "id": "appointment-cancelled-but-no-flag",
        "title": "Cancelled appointment without follow-up flag",
        "category": "workflow",
        "default_severity": "high",
        "entity": {**_BASE_APPOINTMENT_ENTITY, "base_filters": ["IsDeleted = 0"]},
        "params": {},
        "prerequisites": ["CancelledTime IS NOT NULL"],
        "predicate": {
            "all": [
                "AppointmentStatus = 'Cancelled'",
                {
                    "not_exists": {
                        "on": "dbo.Appointments.AppointmentID = dbo.Appointments.CancelledTime",
                        "view": "dbo.Appointments",
                    }
                },
            ]
        },
        "evidence": ["AppointmentID", "AppointmentStatus", "CancelledTime", "PracticeID"],
        "actions": ["raise-recall-task"],
        "resolution": "Ensure follow-up on cancelled appointments is documented.",
    },
    "appointment-completed-no-invoice": {
        "id": "appointment-completed-no-invoice",
        "title": "Appointment completed without a corresponding invoice",
        "category": "workflow",
        "default_severity": "high",
        "entity": {**_BASE_APPOINTMENT_ENTITY, "base_filters": ["IsDeleted = 0"]},
        "params": {},
        "prerequisites": ["AppointmentCompleted IS NOT NULL"],
        "predicate": {
            "all": [
                "AppointmentStatus = 'Appointment Completed'",
                {
                    "not_exists": {
                        "on": "fqb.Invoices.AppointmentID = dbo.Appointments.AppointmentID",
                        "view": "fqb.Invoices",
                    }
                },
            ]
        },
        "evidence": ["AppointmentID", "AppointmentStatus", "PracticeID"],
        "actions": ["verify-invoice"],
        "resolution": "Ensure that an invoice is created before closing the appointment.",
    },
    "high-risk-patient-no-follow-up": {
        "id": "high-risk-patient-no-follow-up",
        "title": "High-Risk Patient Without Follow-Up Care",
        "category": "care-gap",
        "default_severity": "critical",
        "entity": {
            **_BASE_PATIENT_ENTITY,
            "base_filters": ["IsDeleted = 0", "IsActive = 1", "IsHighCare = 1"],
        },
        "params": {},
        "prerequisites": [],
        "predicate": {
            "not_exists": {
                "on": "dbo.Appointments.PatientID = dbo.Patient.ProfileID",
                "view": "dbo.Appointments",
                "where": "DATEDIFF(DAY, dbo.Appointments.ScheduleDate, GETDATE()) <= 30",
            }
        },
        "evidence": ["ProfileID", "IsActive", "IsHighCare"],
        "actions": ["flag-for-clinician-review"],
        "resolution": "The patient is flagged for a review to ensure they receive timely care.",
    },
    "lapsed-health-card-check": {
        "id": "lapsed-health-card-check",
        "title": "Patient Health Card Expiry Overdue",
        "category": "care-gap",
        "default_severity": "medium",
        "entity": {**_BASE_PATIENT_ENTITY, "base_filters": ["IsDeleted = 0", "IsActive = 1"]},
        "params": {},
        "prerequisites": [],
        "predicate": {
            "exists": {
                "on": "dbo.Patient.ProfileID = dbo.Patient.ProfileID",
                "view": "dbo.Patient",
                "where": "dbo.Patient.HealthCardExpiryDate < GETDATE()",
            }
        },
        "evidence": ["ProfileID", "HealthCardExpiryDate"],
        "actions": ["flag-for-clinician-review"],
        "resolution": "Patient needs to be notified regarding their expired health card.",
    },
    "missing-notes-on-completed-appointment": {
        "id": "missing-notes-on-completed-appointment",
        "title": "Completed appointment without notes",
        "category": "workflow",
        "default_severity": "high",
        "entity": {**_BASE_APPOINTMENT_ENTITY, "base_filters": ["IsDeleted = 0"]},
        "params": {},
        "prerequisites": ["AppointmentCompleted IS NOT NULL"],
        "predicate": {"all": ["AppointmentStatus = 'Appointment Completed'", "Notes IS NULL"]},
        "evidence": ["AppointmentID", "Notes", "PracticeID"],
        "actions": ["flag-for-data-steward-review"],
        "resolution": "Add notes to the completed appointment.",
    },
    "no-appointment-overdue-follow-up": {
        "id": "no-appointment-overdue-follow-up",
        "title": "Patient Overdue for Follow-Up Appointment",
        "category": "care-gap",
        "default_severity": "high",
        "entity": {**_BASE_PATIENT_ENTITY, "base_filters": ["IsDeleted = 0", "IsActive = 1"]},
        "params": {},
        "prerequisites": [],
        "predicate": {
            "not_exists": {
                "on": "dbo.Appointments.PatientID = dbo.Patient.ProfileID",
                "view": "dbo.Appointments",
                "where": "dbo.Appointments.ScheduleDate >= DATEADD(MONTH, -12, GETDATE())",
            }
        },
        "evidence": ["ProfileID"],
        "actions": ["book-recall"],
        "resolution": "Patient is scheduled for a follow-up appointment.",
    },
    "no-recent-appointment-high-needs-patient": {
        "id": "no-recent-appointment-high-needs-patient",
        "title": "High-Needs Patient With No Recent Appointment",
        "category": "care-gap",
        "default_severity": "high",
        "entity": {
            **_BASE_PATIENT_ENTITY,
            "base_filters": ["IsDeleted = 0", "IsActive = 1", "IsHighCare = 1"],
        },
        "params": {},
        "prerequisites": [],
        "predicate": {
            "not_exists": {
                "on": "dbo.Appointments.PatientID = dbo.Patient.ProfileID",
                "view": "dbo.Appointments",
                "where": "dbo.Appointments.ScheduleDate >= DATEADD(MONTH, -6, GETDATE())",
            }
        },
        "evidence": ["ProfileID", "IsActive", "IsHighCare"],
        "actions": ["flag-for-clinician-review"],
        "resolution": "The patient is flagged for a follow-up regarding their high needs care.",
    },
    "open-activity-with-no-follow-up": {
        "id": "open-activity-with-no-follow-up",
        "title": "Open appointment activity without follow-up",
        "category": "workflow",
        "default_severity": "medium",
        "entity": {**_BASE_APPOINTMENT_ENTITY, "base_filters": ["IsDeleted = 0"]},
        "params": {},
        "prerequisites": ["ConsultStartTime IS NOT NULL"],
        "predicate": {
            "any": ["AppointmentStatus = 'On Hold'", "AppointmentStatus = 'Waiting for Payment'"]
        },
        "evidence": ["AppointmentID", "ConsultStartTime", "PracticeID"],
        "actions": ["raise-recall-task"],
        "resolution": "Prompt follow-up on active open appointment activities.",
    },
    "overdue-enrollment-expiry-check": {
        "id": "overdue-enrollment-expiry-check",
        "title": "Patient Overdue for Enrollment Renewal",
        "category": "care-gap",
        "default_severity": "medium",
        "entity": {**_BASE_PATIENT_ENTITY, "base_filters": ["IsDeleted = 0", "IsActive = 1"]},
        "params": {},
        "prerequisites": [],
        "predicate": {
            "exists": {
                "on": "dbo.Patient.ProfileID = dbo.Patient.ProfileID",
                "view": "dbo.Patient",
                "where": "dbo.Patient.EnrollmentExpiryDate < GETDATE()",
            }
        },
        "evidence": ["ProfileID", "EnrollmentExpiryDate"],
        "actions": ["raise-recall-task"],
        "resolution": "Patient is prompted for enrollment renewal.",
    },
    "uncompleted-appointment-requires-followup": {
        "id": "uncompleted-appointment-requires-followup",
        "title": "Appointment not marked as completed requiring follow-up",
        "category": "workflow",
        "default_severity": "medium",
        "entity": {**_BASE_APPOINTMENT_ENTITY, "base_filters": ["IsDeleted = 0"]},
        "params": {},
        "prerequisites": ["AppointmentCompleted IS NULL"],
        "predicate": {
            "any": ["AppointmentStatus = 'Waiting for Payment'", "AppointmentStatus = 'Cancelled'"]
        },
        "evidence": ["AppointmentID", "AppointmentStatus", "PracticeID"],
        "actions": ["flag-for-clinician-review"],
        "resolution": "Review the appointment status and update as necessary.",
    },
}

# Checks known, live-proven, to be structurally incapable of clearing the
# fixture-test bar -- a self-referencing exists/not_exists clause, not a
# fixture-data gap (see module docstring).
_EXPECTED_TO_FAIL_FIXTURE_TEST = frozenset(
    {
        "appointment-cancelled-but-no-flag",
        "lapsed-health-card-check",
        "overdue-enrollment-expiry-check",
    }
)


def _detail_for(slug: str) -> CheckDetail:
    definition = _REAL_CHECKS[slug]
    doc = check_doc_from_dict(definition)
    affected_views = {doc.entity.view}
    collect_joined_views(doc.predicate, affected_views)
    return CheckDetail(
        check_id="00000000-0000-0000-0000-000000000000",
        slug=slug,
        title=definition["title"],
        category=definition["category"],
        default_severity=definition["default_severity"],
        status="draft",
        version_id="00000000-0000-0000-0000-000000000001",
        version_number=1,
        definition=definition,
        rationale="LLM-drafted (Phase 4 step 4)",
        affected_views=sorted(affected_views),
    )


def test_every_real_check_is_classified() -> None:
    assert set(_REAL_CHECKS) == {
        "active-patient-no-invoices",
        "appointment-activity-left-open",
        "appointment-cancelled-but-no-flag",
        "appointment-completed-no-invoice",
        "high-risk-patient-no-follow-up",
        "lapsed-health-card-check",
        "missing-notes-on-completed-appointment",
        "no-appointment-overdue-follow-up",
        "no-recent-appointment-high-needs-patient",
        "open-activity-with-no-follow-up",
        "overdue-enrollment-expiry-check",
        "uncompleted-appointment-requires-followup",
    }
    assert set(_REAL_CHECKS) >= _EXPECTED_TO_FAIL_FIXTURE_TEST


def test_good_checks_pass_their_fixture_test(fixture_conn: pyodbc.Connection) -> None:
    for slug in _REAL_CHECKS:
        if slug in _EXPECTED_TO_FAIL_FIXTURE_TEST:
            continue
        detail = _detail_for(slug)
        result = dry_run(detail)
        doc = check_doc_from_dict(detail.definition)
        assert check_passes_fixture_test(result, doc), (
            f"{slug}: status={result.status} n_fail={result.n_fail} "
            f"n_pass={result.n_pass} n_indeterminate={result.n_indeterminate} "
            f"can_reach_indeterminate={can_reach_indeterminate(doc)}"
        )


def test_self_referencing_checks_fail_their_fixture_test(fixture_conn: pyodbc.Connection) -> None:
    for slug in _EXPECTED_TO_FAIL_FIXTURE_TEST:
        detail = _detail_for(slug)
        result = dry_run(detail)
        doc = check_doc_from_dict(detail.definition)
        assert not check_passes_fixture_test(result, doc), (
            f"{slug} was expected to fail its fixture test (self-referencing "
            f"exists/not_exists) but passed: status={result.status} "
            f"n_fail={result.n_fail} n_pass={result.n_pass}"
        )


def test_bare_not_exists_checks_cannot_reach_indeterminate_by_design() -> None:
    structurally_indeterminate_incapable = {
        "active-patient-no-invoices",
        "high-risk-patient-no-follow-up",
        "lapsed-health-card-check",
        "no-appointment-overdue-follow-up",
        "no-recent-appointment-high-needs-patient",
        "overdue-enrollment-expiry-check",
    }
    for slug in structurally_indeterminate_incapable:
        doc = check_doc_from_dict(_REAL_CHECKS[slug])
        assert not can_reach_indeterminate(doc), slug


def test_checks_with_a_leaf_or_prerequisite_can_reach_indeterminate() -> None:
    can_be_indeterminate = {
        "appointment-activity-left-open",
        "appointment-cancelled-but-no-flag",
        "appointment-completed-no-invoice",
        "missing-notes-on-completed-appointment",
        "open-activity-with-no-follow-up",
        "uncompleted-appointment-requires-followup",
    }
    for slug in can_be_indeterminate:
        doc = check_doc_from_dict(_REAL_CHECKS[slug])
        assert can_reach_indeterminate(doc), slug
