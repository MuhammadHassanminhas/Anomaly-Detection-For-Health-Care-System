"""Phase 4 step 1: the curated action library.

Operational actions only (book/verify/chase/flag/correct/raise) -- never a
clinical judgment (guardrail 10). This module is the single source of truth
for the action-code vocabulary: it seeds the app-DB `action_library` table
(giving every `check_actions` row real FK-enforced referential integrity,
replacing Phase 2's `cdss.dsl.STUB_ACTION_LIBRARY`) and supplies the same
code set `cdss.dsl.validate_check_against_catalog` checks every check's
`actions:` list against, so the DB table and the offline validator can never
drift into two independent lists.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert


@dataclass(frozen=True)
class ActionDef:
    code: str
    title: str
    description: str


CURATED_ACTIONS: tuple[ActionDef, ...] = (
    ActionDef(
        "book-recall",
        "Book a recall",
        "Schedule a follow-up appointment for the patient.",
    ),
    ActionDef(
        "verify-invoice",
        "Verify invoice",
        "Check the invoice against the source record and correct it if needed.",
    ),
    ActionDef(
        "chase-result",
        "Chase a result",
        "Follow up on an ordered test or result that has not come back.",
    ),
    ActionDef(
        "flag-for-clinician-review",
        "Flag for clinician review",
        "Route the finding to a clinician for judgment; no automated action taken.",
    ),
    ActionDef(
        "correct-record",
        "Correct record",
        "Fix a data-entry error in the source record.",
    ),
    ActionDef(
        "raise-billing-task",
        "Raise billing task",
        "Create a task for the billing team to resolve a financial discrepancy.",
    ),
    ActionDef(
        "request-nhi-lookup",
        "Request NHI lookup",
        "Ask reception/admin staff to look up and record the patient's NHI.",
    ),
    ActionDef(
        "flag-for-data-steward-review",
        "Flag for data steward review",
        "Route a data-quality issue to whoever owns source-data stewardship.",
    ),
    ActionDef(
        "raise-recall-task",
        "Raise recall task",
        "Create a task for staff to recall a patient who is overdue.",
    ),
)

KNOWN_ACTIONS: frozenset[str] = frozenset(a.code for a in CURATED_ACTIONS)

_action_library_table = sa.Table(
    "action_library",
    sa.MetaData(),
    sa.Column("code", sa.Text(), primary_key=True),
    sa.Column("title", sa.Text(), nullable=False),
    sa.Column("description", sa.Text(), nullable=True),
)


def seed_action_library(conn: sa.Connection) -> None:
    """Upsert every curated action into the app-DB table. Idempotent -- safe
    to run on every deploy, not just once; a title/description edit here
    updates the existing row rather than erroring or duplicating."""
    for action in CURATED_ACTIONS:
        stmt = pg_insert(_action_library_table).values(
            code=action.code, title=action.title, description=action.description
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["code"],
            set_={"title": stmt.excluded.title, "description": stmt.excluded.description},
        )
        conn.execute(stmt)


def main() -> None:
    from cdss.app_db import load_app_db_url

    engine = sa.create_engine(load_app_db_url())
    try:
        with engine.begin() as conn:
            seed_action_library(conn)
    finally:
        engine.dispose()
    print(f"seeded {len(CURATED_ACTIONS)} action_library rows")


if __name__ == "__main__":
    main()


__all__ = ["CURATED_ACTIONS", "KNOWN_ACTIONS", "ActionDef", "seed_action_library"]
