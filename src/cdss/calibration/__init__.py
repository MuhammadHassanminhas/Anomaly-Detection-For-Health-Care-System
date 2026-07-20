"""Phase 4 step 6: cdss.calibration.learn_defaults -- F4 per-practice
parameter learning.

For every `strategy: percentile` param on a check, computes each practice's
own empirical distribution of the param's named `measure` via one set-based
SQL statement (SQL Server `PERCENTILE_CONT`, never a per-row Python loop) and
writes the learned value into `practice_check_config.params` with
`params_source = 'calibrated'` (the schema's real enum value -- the phase
spec's own prose says "learned", but the CHECK constraint on
`practice_check_config.params_source` only allows `'default' | 'calibrated'
| 'manual'`; using the real value, not inventing a fourth). A practice with
fewer than `MIN_SAMPLE_SIZE` observations falls back to the param's own
declared `fallback` value instead -- `params_source` stays `'default'`,
never marked `'calibrated'` for a value nobody actually estimated. Either
way, a `calibration_runs` row records the before/after params, the measure,
`p`, and the sample size, so the decision is auditable.

**Measure registry, not a formula-per-param**: `measure` (e.g.
`appointment_to_invoice_lag`) is a plain string label in the DSL
(`docs/dsl.md`) -- this module is the one place that maps a measure name to
the actual set-based query computing it. Only `appointment_to_invoice_lag`
is implemented -- the only measure any currently-authored check (the
checked-in `appointment-completed-no-invoice.yaml` example) actually
declares; a param naming an unregistered measure raises, naming it, rather
than silently guessing at a query.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

from cdss.dsl import ParamDef, check_doc_from_dict
from cdss.source import AuditedSourceConnection

MIN_SAMPLE_SIZE = 10  # D-011 precedent: "minimum 10 events before any demotion"


@dataclass(frozen=True)
class MeasureDistribution:
    percentile_value: float | None
    sample_size: int


MeasureQuery = Callable[[AuditedSourceConnection, str, float], MeasureDistribution]


_LAG_DISTRIBUTION_SQL = (
    "SELECT DATEDIFF(day, a.ScheduleDate, i.InvoiceDate) AS lag_days"
    " FROM dbo.Appointments a"
    " JOIN fqb.Invoices i ON i.AppointmentID = a.AppointmentID"
    " WHERE a.PracticeID = ? AND a.AppointmentCompleted = 1 AND a.IsDeleted = 0"
    "   AND i.IsDeleted = 0 AND a.ScheduleDate IS NOT NULL AND i.InvoiceDate IS NOT NULL"
)


def _measure_appointment_to_invoice_lag(
    source_conn: AuditedSourceConnection, practice_id: str, p: float
) -> MeasureDistribution:
    """Days between a completed appointment's `ScheduleDate` and its linked
    invoice's `InvoiceDate` (`fqb.Invoices.AppointmentID`), for one practice
    -- a single set-based `SELECT`, `PERCENTILE_CONT` computing the whole
    distribution's percentile server-side rather than fetching every row.

    Two scalar subqueries, not one aggregate `SELECT COUNT(*), PERCENTILE_CONT(...)`
    -- found live: SQL Server requires `PERCENTILE_CONT` to carry an `OVER`
    clause even used as a plain aggregate, which turns it into a per-row
    window value with no `GROUP BY`-style collapse, and (unlike `COUNT(*)`)
    a window function over zero input rows returns *no rows at all*, not a
    single row with `NULL`. Each scalar subquery below always yields
    exactly one value -- `0`/`NULL` on an empty distribution -- so the outer
    `SELECT` is always exactly one row, matching every other statement this
    project's SQL guard already accepts (single top-level `SELECT`)."""
    sql = (
        f"SELECT (SELECT COUNT(*) FROM ({_LAG_DISTRIBUTION_SQL}) AS d), "
        "(SELECT TOP 1 PERCENTILE_CONT(?) WITHIN GROUP (ORDER BY lag_days) OVER () "
        f"FROM ({_LAG_DISTRIBUTION_SQL}) AS d2)"
    )
    (row,) = source_conn.execute_query(sql, [practice_id, p / 100, practice_id])
    count, percentile_value = row
    if count == 0:
        return MeasureDistribution(percentile_value=None, sample_size=0)
    return MeasureDistribution(percentile_value=float(percentile_value), sample_size=int(count))


MEASURE_REGISTRY: dict[str, MeasureQuery] = {
    "appointment_to_invoice_lag": _measure_appointment_to_invoice_lag,
}


class UnknownMeasureError(ValueError):
    """A percentile param names a measure with no registered query."""


@dataclass(frozen=True)
class LearnedParam:
    param_name: str
    value: float
    sample_size: int
    learned: bool  # False when the sample was too small and `fallback` was used instead


_SELECT_CURRENT_PARAMS_SQL = sa.text(
    "SELECT params FROM practice_check_config WHERE practice_id = :practice_id "
    "AND check_id = :check_id"
)

_UPSERT_PRACTICE_CHECK_CONFIG_SQL = sa.text(
    "INSERT INTO practice_check_config (practice_id, check_id, params, params_source) "
    "VALUES (:practice_id, :check_id, CAST(:params AS jsonb), :params_source) "
    "ON CONFLICT (practice_id, check_id) DO UPDATE SET "
    "params = practice_check_config.params || CAST(:params AS jsonb), "
    "params_source = :params_source"
)

_INSERT_CALIBRATION_RUN_SQL = sa.text(
    "INSERT INTO calibration_runs (practice_id, check_id, params_before, params_after, notes) "
    "VALUES (:practice_id, :check_id, CAST(:params_before AS jsonb), "
    "CAST(:params_after AS jsonb), :notes)"
)


def learn_defaults_for_check(
    source_conn: AuditedSourceConnection,
    conn: sa.Connection,
    *,
    check_id: str,
    practice_id: str,
    definition: dict[str, Any],
    min_sample_size: int = MIN_SAMPLE_SIZE,
) -> list[LearnedParam]:
    """Learns every `percentile`-strategy param on `definition` for one
    (practice, check) pair, merges the results into that practice's
    `practice_check_config.params` (only the learned/fallback keys --
    `||` preserves every other key already there), and records one
    `calibration_runs` snapshot for the pair. Returns what was computed for
    each param, learned or not."""
    doc = check_doc_from_dict(definition)
    percentile_params: dict[str, ParamDef] = {
        name: param for name, param in doc.params.items() if param.default.strategy == "percentile"
    }
    if not percentile_params:
        return []

    before_row = conn.execute(
        _SELECT_CURRENT_PARAMS_SQL, {"practice_id": practice_id, "check_id": check_id}
    ).one_or_none()
    params_before = before_row.params if before_row is not None else {}

    results: list[LearnedParam] = []
    params_after: dict[str, float] = {}
    notes: list[str] = []
    any_learned = False

    for name, param in percentile_params.items():
        measure = param.default.measure
        assert measure is not None  # schema requires it for strategy=percentile
        measure_fn = MEASURE_REGISTRY.get(measure)
        if measure_fn is None:
            raise UnknownMeasureError(
                f"no registered measure query for '{measure}' (param '{name}')"
            )
        p = param.default.p
        assert p is not None
        distribution = measure_fn(source_conn, practice_id, p)
        has_enough_data = distribution.sample_size >= min_sample_size
        if has_enough_data and distribution.percentile_value is not None:
            value = distribution.percentile_value
            learned = True
            any_learned = True
        else:
            value = float(param.default.fallback)
            learned = False
        params_after[name] = value
        results.append(
            LearnedParam(
                param_name=name, value=value, sample_size=distribution.sample_size, learned=learned
            )
        )
        notes.append(
            f"{name}: measure={measure} p={p} n={distribution.sample_size} learned={learned}"
        )

    conn.execute(
        _UPSERT_PRACTICE_CHECK_CONFIG_SQL,
        {
            "practice_id": practice_id,
            "check_id": check_id,
            "params": json.dumps(params_after),
            "params_source": "calibrated" if any_learned else "default",
        },
    )
    conn.execute(
        _INSERT_CALIBRATION_RUN_SQL,
        {
            "practice_id": practice_id,
            "check_id": check_id,
            "params_before": json.dumps(params_before),
            "params_after": json.dumps(params_after),
            "notes": "; ".join(notes),
        },
    )
    return results


__all__ = [
    "MEASURE_REGISTRY",
    "MIN_SAMPLE_SIZE",
    "LearnedParam",
    "MeasureDistribution",
    "UnknownMeasureError",
    "learn_defaults_for_check",
]
