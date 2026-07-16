"""Phase 1 step 8: one-command orchestration + reports.

Invoked via `python -m cdss.profile`, wrapped by `scripts/profile.ps1`. Runs
steps 2-7 end-to-end for every in-scope view (column profiling, archetype
detection + reference-vocabulary capture, candidate-key detection, sentinel
& test-record-indicator detection, watermark verification, export
reconciliation), then step 4's cross-view pair/containment analysis, and
writes the schema-validated `artifacts/catalog/semantic-catalog-v<N>.json` +
human-readable `artifacts/catalog/profiling-report.md`.

Idempotent: each successful run bumps to a new catalog version
(`catalog.next_catalog_version`) rather than overwriting a prior one.
Interim failures resume cleanly via per-view checkpointing
(`cdss.checkpoint`) -- a re-run skips any view already checkpointed by an
earlier, interrupted attempt, with no manual steps.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cdss.archetype import (
    apply_reference_capture,
    capture_reference_vocabulary,
    detect_view_archetype,
)
from cdss.candidate_keys import CandidateKey, detect_candidate_keys
from cdss.catalog import (
    compute_artifact_hash,
    next_catalog_version,
    write_json,
    write_manifest_entry,
)
from cdss.checkpoint import (
    clear_checkpoint,
    load_checkpoint,
    save_checkpoint,
    view_context_from_view_dict,
)
from cdss.config import load_source_config
from cdss.connection import connect
from cdss.export_reconciliation import (
    ExportViewHypothesis,
    ViewDiscrepancyReport,
    parse_export_hypotheses,
    reconcile_view,
)
from cdss.profiler import (
    ColumnProfile,
    _is_key_column,
    _is_timeout_error,
    fetch_columns,
    profile_view,
)
from cdss.relationships import (
    DEFAULT_MAX_PAIRS_EVALUATED,
    DEFAULT_PER_PAIR_TIMEOUT_SECONDS,
    RelationshipEdge,
    ViewContext,
    detect_relationships,
)
from cdss.sentinels import detect_sentinels, detect_test_record_indicators
from cdss.source import AuditedSourceConnection
from cdss.watermarks import classify_view_watermarks

DEFAULT_VIEWS: tuple[str, ...] = (
    "dbo.AppointmentMedications",
    "dbo.Disease",
    "dbo.Patient",
    "fqb.Allergies",
    "fqb.Diagnosis",
    "dbo.Immunisation",
    "OLAP.Medicine",
    "dbo.PatientAlerts",
    "fqb.Invoices",
    "dbo.Appointments",
)
DEFAULT_EXPORT_HYPOTHESES_PATH = Path("schema_for_SQL_PROJ.txt")
DEFAULT_ENV_REPORT_PATH = Path("artifacts/env-report.json")
DEFAULT_CATALOG_DIR = Path("artifacts/catalog")
DEFAULT_ROW_COUNT_TIMEOUT_SECONDS = 15
DEFAULT_STEP_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class WatermarkHints:
    """Per-view D-018 sampling hint (name, min, max) plus the full
    column->max map used by step 6's monotonicity check -- both sourced
    from Phase 0's `env-report.json`, the historical baseline these checks
    are specifically about (D-017's "live wins" is about correctness of
    *current* facts, not about what the historical comparison point is)."""

    sampling_hint: tuple[str, str, str] | None
    baseline_max_by_column: dict[str, str]


def _load_watermark_hints(env_report_path: Path) -> dict[str, WatermarkHints]:
    if not env_report_path.exists():
        return {}
    data = json.loads(env_report_path.read_text(encoding="utf-8"))
    hints: dict[str, WatermarkHints] = {}
    for obj in data.get("row_stats", []):
        columns = obj.get("watermark_columns", [])
        sampling_hint = None
        if columns:
            first = columns[0]
            sampling_hint = (first["column_name"], first["min_value"], first["max_value"])
        hints[obj["qualified_name"].lower()] = WatermarkHints(
            sampling_hint=sampling_hint,
            baseline_max_by_column={c["column_name"]: c["max_value"] for c in columns},
        )
    return hints


def _cost_dict(cost: Any) -> dict[str, Any]:
    return {
        "view": cost.view,
        "operation": cost.operation,
        "duration_ms": cost.duration_ms,
        "status": cost.status,
    }


def _column_profile_dict(profile: ColumnProfile) -> dict[str, Any]:
    return {
        "column_name": profile.column_name,
        "data_type": profile.data_type,
        "is_free_text": profile.is_free_text,
        "column_class": profile.column_class,
        "sampling": {"sampled": profile.sampling.sampled, "method": profile.sampling.method},
        "null_count": profile.null_count,
        "null_rate": profile.null_rate,
        "distinct_count": profile.distinct_count,
        "min_value": profile.min_value,
        "max_value": profile.max_value,
        "top_values": [{"value": t.value, "frequency": t.frequency} for t in profile.top_values],
        "string_length_stats": (
            {
                "min_length": profile.string_length_stats.min_length,
                "max_length": profile.string_length_stats.max_length,
                "avg_length": profile.string_length_stats.avg_length,
            }
            if profile.string_length_stats is not None
            else None
        ),
        "reference_samples": (
            {"values": profile.reference_samples.values, "sample_only": True}
            if profile.reference_samples is not None
            else None
        ),
        "value_pattern_stats": (
            {"trailing_tag_counts": profile.value_pattern_stats.trailing_tag_counts}
            if profile.value_pattern_stats is not None
            else None
        ),
    }


def _candidate_key_dict(key: CandidateKey) -> dict[str, Any]:
    return {
        "columns": key.columns,
        "distinct_count": key.distinct_count,
        "row_count": key.row_count,
        "evidence_method": key.evidence_method,
    }


def _count_rows(
    audited: AuditedSourceConnection, qualified_name: str, *, timeout_seconds: int
) -> tuple[int | None, str, dict[str, Any] | None]:
    began = time.perf_counter()
    try:
        (count,) = audited.execute_query(
            f"SELECT COUNT(*) FROM {qualified_name}", timeout_seconds=timeout_seconds
        )[0]
    except Exception as exc:
        if not _is_timeout_error(exc):
            raise
        duration_ms = round((time.perf_counter() - began) * 1000, 3)
        return (
            None,
            "indeterminate",
            {
                "view": qualified_name,
                "operation": "column_profile",
                "duration_ms": duration_ms,
                "status": "timeout",
            },
        )
    return int(count), "exact", None


def _empty_view_dict(qualified_name: str, row_count_status: str) -> dict[str, Any]:
    return {
        "qualified_name": qualified_name,
        "row_count": None,
        "row_count_status": row_count_status,
        "archetype": "fact",
        "columns": [],
        "candidate_keys": [],
        "watermark_classification": {"status": "fallback_needed", "columns": []},
        "sentinels": [],
        "test_record_indicators": [],
    }


def profile_one_view(
    audited: AuditedSourceConnection,
    *,
    qualified_name: str,
    hints: WatermarkHints,
    row_count_timeout_seconds: int = DEFAULT_ROW_COUNT_TIMEOUT_SECONDS,
    step_timeout_seconds: int = DEFAULT_STEP_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any], ViewContext, list[dict[str, Any]]]:
    """Runs steps 2, 3, 5, 6 (D-023 archetype included) for one view. Step 4
    (cross-view pair analysis) and step 7 (export reconciliation) are
    handled by the caller, once across/alongside all views."""
    costs: list[dict[str, Any]] = []
    raw_columns = fetch_columns(audited, qualified_name)

    row_count, row_count_status, timeout_cost = _count_rows(
        audited, qualified_name, timeout_seconds=row_count_timeout_seconds
    )
    if timeout_cost is not None:
        costs.append(timeout_cost)
    if row_count is None:
        # F6: no row count -> no per-column profiling is safe or meaningful.
        view_dict = _empty_view_dict(qualified_name, row_count_status)
        empty_context = ViewContext(
            qualified_name=qualified_name, row_count=0, raw_columns=[], profiles=[]
        )
        return view_dict, empty_context, costs

    profiles, profile_costs = profile_view(
        audited,
        qualified_name=qualified_name,
        row_count=row_count,
        columns=raw_columns,
        watermark_column=hints.sampling_hint,
        timeout_seconds=step_timeout_seconds,
    )
    costs.extend(_cost_dict(c) for c in profile_costs)

    archetype_result, archetype_costs = detect_view_archetype(
        audited,
        qualified_name=qualified_name,
        row_count=row_count,
        columns=raw_columns,
        watermark_column=hints.sampling_hint,
        timeout_seconds=step_timeout_seconds,
    )
    costs.extend(_cost_dict(c) for c in archetype_costs)

    if archetype_result.archetype == "reference" and archetype_result.signals.name_column:
        key_column = next((name for name, dt, _cl in raw_columns if _is_key_column(name, dt)), None)
        if key_column is not None:
            samples, tag_stats, ref_costs = capture_reference_vocabulary(
                audited,
                qualified_name=qualified_name,
                name_column=archetype_result.signals.name_column,
                key_column=key_column,
                timeout_seconds=step_timeout_seconds,
            )
            costs.extend(_cost_dict(c) for c in ref_costs)
            name_column = archetype_result.signals.name_column
            profiles = [
                apply_reference_capture(p, samples, tag_stats)
                if p.column_name == name_column
                else p
                for p in profiles
            ]

    candidate_keys, ck_costs = detect_candidate_keys(
        audited,
        qualified_name=qualified_name,
        row_count=row_count,
        columns=raw_columns,
        watermark_column=hints.sampling_hint,
        timeout_seconds=step_timeout_seconds,
    )
    costs.extend(_cost_dict(c) for c in ck_costs)

    live_columns = [(name, data_type) for name, data_type, _cl in raw_columns]
    watermark_classification, wm_costs = classify_view_watermarks(
        audited,
        qualified_name=qualified_name,
        columns=live_columns,
        baseline_max_by_column=hints.baseline_max_by_column,
        timeout_seconds=step_timeout_seconds,
    )
    costs.extend(_cost_dict(c) for c in wm_costs)

    sentinels = detect_sentinels(profiles)
    test_record_indicators = detect_test_record_indicators(profiles, row_count)

    view_dict = {
        "qualified_name": qualified_name,
        "row_count": row_count,
        "row_count_status": row_count_status,
        "archetype": archetype_result.archetype,
        "columns": [_column_profile_dict(p) for p in profiles],
        "candidate_keys": [_candidate_key_dict(k) for k in candidate_keys],
        "watermark_classification": {
            "status": watermark_classification.status,
            "columns": watermark_classification.columns,
        },
        "sentinels": [
            {
                "column_name": s.column_name,
                "sentinel_type": s.sentinel_type,
                "value": s.value,
                "frequency": s.frequency,
                "description": s.description,
            }
            for s in sentinels
        ],
        "test_record_indicators": [
            {
                "column_name": i.column_name,
                "prevalence_count": i.prevalence_count,
                "prevalence_rate": i.prevalence_rate,
            }
            for i in test_record_indicators
        ],
    }
    view_context = ViewContext(
        qualified_name=qualified_name,
        row_count=row_count,
        raw_columns=raw_columns,
        profiles=profiles,
        watermark_column=hints.sampling_hint,
    )
    return view_dict, view_context, costs


def _relationship_edge_dict(edge: RelationshipEdge) -> dict[str, Any]:
    return {
        "from_view": edge.from_view,
        "from_column": edge.from_column,
        "to_view": edge.to_view,
        "to_column": edge.to_column,
        "status": edge.status,
        "containment_a_to_b": edge.containment_a_to_b,
        "containment_b_to_a": edge.containment_b_to_a,
        "orphan_count_a": edge.orphan_count_a,
        "orphan_count_b": edge.orphan_count_b,
    }


def run_profiling(
    audited: AuditedSourceConnection,
    *,
    views: tuple[str, ...],
    export_hypotheses_path: Path,
    env_report_path: Path,
    checkpoint_path: Path,
    catalog_version: int,
    produced_at: str,
    source_database: str,
    max_pairs_evaluated: int = DEFAULT_MAX_PAIRS_EVALUATED,
    per_pair_timeout_seconds: int = DEFAULT_PER_PAIR_TIMEOUT_SECONDS,
    row_count_timeout_seconds: int = DEFAULT_ROW_COUNT_TIMEOUT_SECONDS,
    step_timeout_seconds: int = DEFAULT_STEP_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any], list[ViewDiscrepancyReport]]:
    in_scope = frozenset(v.lower() for v in views)
    hypotheses = parse_export_hypotheses(export_hypotheses_path)
    hints_by_view = _load_watermark_hints(env_report_path)

    checkpoint = load_checkpoint(checkpoint_path)
    view_dicts: dict[str, Any] = checkpoint["views"]
    all_costs: list[dict[str, Any]] = []
    view_contexts: list[ViewContext] = []

    for view in views:
        if view in view_dicts:
            view_contexts.append(view_context_from_view_dict(view_dicts[view]))
            continue
        default_hints = WatermarkHints(sampling_hint=None, baseline_max_by_column={})
        hints = hints_by_view.get(view.lower(), default_hints)
        view_dict, view_context, costs = profile_one_view(
            audited,
            qualified_name=view,
            hints=hints,
            row_count_timeout_seconds=row_count_timeout_seconds,
            step_timeout_seconds=step_timeout_seconds,
        )
        view_dicts[view] = view_dict
        all_costs.extend(costs)
        view_contexts.append(view_context)
        checkpoint["views"] = view_dicts
        save_checkpoint(checkpoint_path, checkpoint)

    edges, pruning_report, relationship_costs = detect_relationships(
        audited,
        views=view_contexts,
        max_pairs_evaluated=max_pairs_evaluated,
        per_pair_timeout_seconds=per_pair_timeout_seconds,
    )
    all_costs.extend(_cost_dict(c) for c in relationship_costs)

    discrepancy_reports: list[ViewDiscrepancyReport] = []
    live_columns_by_view = {
        view.lower(): [(p.column_name, p.data_type) for p in ctx.profiles]
        for view, ctx in zip(views, view_contexts, strict=True)
    }
    for view in views:
        hypothesis = hypotheses.get(
            next((k for k in hypotheses if k.lower() == view.lower()), view),
            ExportViewHypothesis(qualified_name=view, columns=[], related_table_names=[]),
        )
        discrepancy_reports.append(
            reconcile_view(
                hypothesis, live_columns_by_view=live_columns_by_view, in_scope_views=in_scope
            )
        )

    catalog_dict = {
        "catalog_version": catalog_version,
        "produced_at": produced_at,
        "source_database": source_database,
        "views": [view_dicts[view] for view in views],
        "relationships": [_relationship_edge_dict(e) for e in edges],
        "profiling_costs": all_costs,
        "pruning_report": {
            "pairs_considered": pruning_report.pairs_considered,
            "pairs_pruned": pruning_report.pairs_pruned,
            "pairs_evaluated": pruning_report.pairs_evaluated,
            "pairs_skipped_cost": pruning_report.pairs_skipped_cost,
        },
    }
    return catalog_dict, discrepancy_reports


def _render_profiling_report(
    catalog: dict[str, Any], discrepancy_reports: list[ViewDiscrepancyReport]
) -> str:
    lines: list[str] = [
        "# CDSS Phase 1 — Profiling Report",
        "",
        f"Catalog version: {catalog['catalog_version']}",
        f"Produced: {catalog['produced_at']}",
        f"Source database: `{catalog['source_database']}`",
        "",
        "## Views",
        "",
        "| View | Row count | Status | Archetype | Columns | Candidate keys "
        "| Watermark | Sentinels |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for view in catalog["views"]:
        row_count = view["row_count"] if view["row_count"] is not None else "—"
        keys = ", ".join("+".join(k["columns"]) for k in view["candidate_keys"]) or "—"
        lines.append(
            f"| `{view['qualified_name']}` | {row_count} | {view['row_count_status']} | "
            f"{view['archetype']} | {len(view['columns'])} | {keys} | "
            f"{view['watermark_classification']['status']} | {len(view['sentinels'])} |"
        )

    lines += [
        "",
        "## Relationships (step 4)",
        "",
        f"Pruning report: {catalog['pruning_report']}",
        "",
        "| From | To | Status | Containment A→B | Containment B→A |",
        "|---|---|---|---|---|",
    ]
    for edge in catalog["relationships"]:
        lines.append(
            f"| `{edge['from_view']}.{edge['from_column']}` | "
            f"`{edge['to_view']}.{edge['to_column']}` | {edge['status']} | "
            f"{edge['containment_a_to_b']} | {edge['containment_b_to_a']} |"
        )

    lines += [
        "",
        "## Export Discrepancy Log (step 7)",
        "",
        "Every disagreement between `schema_for_SQL_PROJ.txt`'s per-view "
        "hypotheses and the live catalog (D-017 — live always wins).",
        "",
    ]
    any_discrepancy = False
    for report in discrepancy_reports:
        if not report.column_discrepancies and not report.relation_discrepancies:
            continue
        any_discrepancy = True
        lines.append(f"### `{report.qualified_name}`")
        lines.append("")
        for d in report.column_discrepancies:
            lines.append(
                f"- **{d.discrepancy_type}**: `{d.column_name}` "
                f"(documented: {d.documented_type or '—'}, live: {d.live_type or '—'})"
            )
        for r in report.relation_discrepancies:
            lines.append(f"- relation to `{r.related_table_name}`: {r.status}")
        lines.append("")
    if not any_discrepancy:
        lines.append("No discrepancies found.")
        lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the CDSS Phase 1 profiling pipeline.")
    parser.add_argument("--catalog-dir", type=Path, default=DEFAULT_CATALOG_DIR)
    parser.add_argument("--export-path", type=Path, default=DEFAULT_EXPORT_HYPOTHESES_PATH)
    parser.add_argument("--env-report-path", type=Path, default=DEFAULT_ENV_REPORT_PATH)
    args = parser.parse_args(argv)

    config = load_source_config()
    conn = connect(config)
    audited = AuditedSourceConnection(
        conn, component="profile", allowed_objects=frozenset(v.lower() for v in DEFAULT_VIEWS)
    )

    checkpoint_path = args.catalog_dir / ".profile-checkpoint.json"
    version = next_catalog_version(args.catalog_dir)
    produced_at = datetime.now(UTC).isoformat()

    catalog_dict, discrepancy_reports = run_profiling(
        audited,
        views=DEFAULT_VIEWS,
        export_hypotheses_path=args.export_path,
        env_report_path=args.env_report_path,
        checkpoint_path=checkpoint_path,
        catalog_version=version,
        produced_at=produced_at,
        source_database=config.database,
    )

    json_path = args.catalog_dir / f"semantic-catalog-v{version}.json"
    write_json(catalog_dict, json_path)
    artifact_hash = compute_artifact_hash(json_path)
    write_manifest_entry(
        args.catalog_dir / "manifest.jsonl",
        catalog_version=version,
        artifact_path=str(json_path),
        sha256=artifact_hash,
        produced_at=produced_at,
    )
    report_path = args.catalog_dir / "profiling-report.md"
    report_path.write_text(
        _render_profiling_report(catalog_dict, discrepancy_reports), encoding="utf-8"
    )
    clear_checkpoint(checkpoint_path)

    print(f"Wrote {json_path} (sha256 {artifact_hash[:12]}...)")
    print(f"Wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
