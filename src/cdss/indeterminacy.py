"""Phase 3 step 7: indeterminacy surfacing (F6). When a check's indeterminate
rate for a (check, practice, run) execution exceeds a configured threshold,
emit exactly one system data-quality finding summarizing it -- never one
finding per indeterminate row, which would be exactly the alert-fatigue
noise F5/F9 exist to prevent.

**Not routed through `cdss.compiler`/`cdss.dsl` at all**: this system check
observes another check's already-computed `check_executions` stats (rows
already classified by step 5's executor), not source-DB rows -- there is no
view to query. It is still a genuine, versioned `checks`/`check_versions`
row (per the phase spec's "itself versioned in the library, not
hard-coded"), so its `default_severity`/enablement/audit history are managed
the same way as any other check, but its `check_versions.definition` is not
DSL-schema-valid and must never be handed to `cdss.dsl.check_doc_from_dict`
-- flagged here because the run loop that dispatches `status='active'`
checks to the normal executor path (step 8, not yet built) will need to
special-case it (e.g. by slug) rather than compile it like every other
check. No schema change proposed for this -- an explicit `checks.source`/
`category` value for "system check" would be a `DECISIONS.md`-worthy CHECK
constraint change, not something to slip in silently here.

The threshold itself is a plain caller-supplied argument, not looked up from
config by this module -- same "caller decides" precedent `materialize`'s
`auto_resolve` and `watermark_manager`'s strategy argument already set. The
natural home for it is the system check's own `practice_check_config.params`
(step 3's loader already resolves per-practice params generically), wired up
by whichever future step builds the run loop.

Reuses `cdss.materialize.materialize_check_result` for the actual upsert --
the summarized row is a synthetic single-row `CheckExecutionResult`, so it
gets the exact same dedupe/reseen/idempotency machinery every other check's
findings get, including auto-resolution once the rate recovers (a system
check auto-resolving its own noise once the noise clears is unambiguously
correct, not a check-authoring choice a human needs to opt into -- unlike
step 6's `auto_resolve`, this one is fixed `True`, not caller-supplied).
"""

from __future__ import annotations

from cdss.executor import CheckExecutionResult, ExecutedRow

ENTITY_KEY_COLUMNS: tuple[str, ...] = ("target_check_id",)


def compute_indeterminate_rate(result: CheckExecutionResult) -> float | None:
    """`n_indeterminate / rows_examined`, or `None` when nothing was
    examined -- F6's own rule applied one level up: no data means no
    evaluation, never a flag either way."""
    if result.rows_examined == 0:
        return None
    return result.n_indeterminate / result.rows_examined


def build_indeterminacy_check_result(
    system_check_id: str,
    system_check_version_id: str,
    target_result: CheckExecutionResult,
    *,
    threshold: float,
) -> CheckExecutionResult:
    """Always a `CheckExecutionResult` for the *system* check (`check_id`/
    `check_version_id` are the system check's own, `practice_id` inherited
    from `target_result`) -- zero rows when the rate isn't evaluable (no
    finding either way), otherwise exactly one synthetic `ExecutedRow`
    keyed on `target_result.check_id`: `fail` when the rate exceeds
    `threshold`, `pass` otherwise (so a recovered rate can auto-resolve a
    previously-raised system finding through the normal materialize path).
    Never more than one row -- the whole point is summarizing however many
    indeterminate source rows into one signal, not re-emitting each."""
    rate = compute_indeterminate_rate(target_result)
    if rate is None:
        rows: tuple[ExecutedRow, ...] = ()
    else:
        tri_state = "fail" if rate > threshold else "pass"
        evidence = {
            "target_check_id": target_result.check_id,
            "rate": rate,
            "n_indeterminate": target_result.n_indeterminate,
            "rows_examined": target_result.rows_examined,
            "threshold": threshold,
        }
        rows = (
            ExecutedRow(
                entity_key=(target_result.check_id,), tri_state=tri_state, evidence=evidence
            ),
        )

    return CheckExecutionResult(
        check_id=system_check_id,
        check_version_id=system_check_version_id,
        practice_id=target_result.practice_id,
        sql_hash="system:indeterminate-rate",
        watermark_from=None,
        watermark_to=None,
        duration_ms=0,
        rows_examined=len(rows),
        n_pass=sum(1 for r in rows if r.tri_state == "pass"),
        n_fail=sum(1 for r in rows if r.tri_state == "fail"),
        n_indeterminate=0,
        status="ok",
        error_message=None,
        rows=rows,
    )


__all__ = ["ENTITY_KEY_COLUMNS", "build_indeterminacy_check_result", "compute_indeterminate_rate"]
