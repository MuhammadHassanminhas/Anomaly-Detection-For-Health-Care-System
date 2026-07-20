"""Check registry loader (Phase 3 step 3): the only path from the app DB to
a runnable check definition.

load_active_checks() is a single SELECT joining checks -> that check's latest
check_version -> practice_check_config. The WHERE clause (checks.status =
'active') is the F3 gate: there is no parameter, override, or code path in
this module that can surface a draft/in_review/rejected/retired check --
excluding them is structural, not a filter callers could accidentally skip.

"Latest check_version" (there is no separate is-current flag in the schema)
is the row with the highest version_number for that check_id -- check_versions
is immutable/append-only, so the highest version_number is by construction
the current one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

_ACTIVE_CHECKS_SQL = sa.text(
    """
    WITH latest_versions AS (
        SELECT DISTINCT ON (check_id) *
        FROM check_versions
        ORDER BY check_id, version_number DESC
    )
    SELECT
        c.id AS check_id,
        c.slug,
        c.title,
        c.category,
        c.default_severity,
        v.id AS check_version_id,
        v.version_number,
        v.definition,
        v.definition_hash,
        v.rationale,
        v.fallback_template,
        v.affected_views,
        v.params_schema,
        pcc.practice_id,
        pcc.enabled,
        pcc.demoted,
        pcc.params,
        pcc.params_source
    FROM checks c
    JOIN latest_versions v ON v.check_id = c.id
    JOIN practice_check_config pcc ON pcc.check_id = c.id
    WHERE c.status = 'active'
    ORDER BY c.slug, pcc.practice_id
    """
)


@dataclass(frozen=True)
class LoadedCheck:
    check_id: str
    slug: str
    title: str
    category: str
    default_severity: str
    check_version_id: str
    version_number: int
    definition: dict[str, Any]
    definition_hash: str
    affected_views: list[str]
    params_schema: dict[str, Any]
    practice_id: str
    enabled: bool
    demoted: bool
    params: dict[str, Any]
    params_source: str
    rationale: str | None = None
    fallback_template: str = ""


def load_active_checks(conn: sa.Connection, *, practice_id: str | None = None) -> list[LoadedCheck]:
    """Load every status=active check's latest version, one row per
    configured practice. A check with no practice_check_config row for any
    practice yields nothing -- there's nothing to execute for it yet, not an
    error. `practice_id` narrows the result to one practice; omitted, every
    configured practice is returned."""
    rows = conn.execute(_ACTIVE_CHECKS_SQL).mappings().all()
    checks = [
        LoadedCheck(
            check_id=str(row["check_id"]),
            slug=row["slug"],
            title=row["title"],
            category=row["category"],
            default_severity=row["default_severity"],
            check_version_id=str(row["check_version_id"]),
            version_number=row["version_number"],
            definition=row["definition"],
            definition_hash=row["definition_hash"],
            affected_views=list(row["affected_views"]),
            params_schema=row["params_schema"],
            rationale=row["rationale"],
            fallback_template=row["fallback_template"],
            practice_id=row["practice_id"],
            enabled=row["enabled"],
            demoted=row["demoted"],
            params=row["params"],
            params_source=row["params_source"],
        )
        for row in rows
    ]
    if practice_id is not None:
        checks = [c for c in checks if c.practice_id == practice_id]
    return checks


__all__ = ["LoadedCheck", "load_active_checks"]
