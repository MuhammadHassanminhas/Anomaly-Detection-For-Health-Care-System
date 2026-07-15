"""Phase 0 step 8: assemble the environment report artifact from the outputs
of steps 4-7 (version capture, surface enumeration, D-001 reconciliation,
row counts + watermark candidates), and write it as schema-validated JSON
plus a human-readable Markdown summary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jsonschema

if TYPE_CHECKING:
    from cdss.reconcile import ReconciliationResult
    from cdss.rowstats import ObjectRowStats
    from cdss.surface import SurfaceObject
    from cdss.verify_env import VersionInfo

SCHEMA_PATH = Path(__file__).parent / "schemas" / "env-report.schema.json"

_DISCREPANCY_STATUSES = ("found_as_table", "found_as_other", "missing")


@dataclass(frozen=True)
class EnvironmentReport:
    generated_at: str
    version: VersionInfo
    surface: list[SurfaceObject]
    reconciliation: ReconciliationResult
    row_stats: list[ObjectRowStats]


def _surface_summary(surface: list[SurfaceObject]) -> dict[str, int]:
    return {
        "total_objects": len(surface),
        "views": sum(1 for obj in surface if obj.object_type == "view"),
        "tables": sum(1 for obj in surface if obj.object_type == "table"),
        "other": sum(1 for obj in surface if obj.object_type == "other"),
    }


def to_dict(report: EnvironmentReport) -> dict[str, Any]:
    return {
        "generated_at": report.generated_at,
        "database": {
            "version_string": report.version.version_string,
            "product_version": report.version.product_version,
            "edition": report.version.edition,
            "engine_edition": report.version.engine_edition,
            "database_name": report.version.database_name,
        },
        "surface": _surface_summary(report.surface),
        "reconciliation": {
            "export_object_count": len(report.reconciliation.entries),
            "entries": [
                {
                    "export_name": entry.export_name,
                    "status": entry.status,
                    "matched_object": entry.matched_object,
                }
                for entry in report.reconciliation.entries
            ],
            "extra_objects": report.reconciliation.extra_objects,
        },
        "row_stats": [
            {
                "qualified_name": stats.qualified_name,
                "object_type": stats.object_type,
                "row_count": stats.row_count,
                "row_count_status": stats.row_count_status,
                "duration_ms": stats.duration_ms,
                "watermark_columns": [
                    {
                        "column_name": column.column_name,
                        "data_type": column.data_type,
                        "min_value": column.min_value,
                        "max_value": column.max_value,
                    }
                    for column in stats.watermark_columns
                ],
            }
            for stats in report.row_stats
        ],
    }


def load_schema(schema_path: Path = SCHEMA_PATH) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(schema_path.read_text(encoding="utf-8"))
    return result


def validate_report_dict(data: dict[str, Any], schema_path: Path = SCHEMA_PATH) -> None:
    """Raise jsonschema.ValidationError if `data` does not conform to the
    env-report schema (Phase 0 exit criterion 3)."""
    jsonschema.validate(instance=data, schema=load_schema(schema_path))


def write_json(report: EnvironmentReport, path: Path, *, schema_path: Path = SCHEMA_PATH) -> None:
    """Write the schema-validated JSON artifact. Idempotent: re-running with
    the same report overwrites the file with identical content."""
    data = to_dict(report)
    validate_report_dict(data, schema_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _render_markdown(report: EnvironmentReport) -> str:
    data = to_dict(report)
    db = data["database"]
    surf = data["surface"]
    entries = data["reconciliation"]["entries"]
    extra_objects = data["reconciliation"]["extra_objects"]
    row_stats = data["row_stats"]

    lines: list[str] = [
        "# CDSS Phase 0 — Environment Report",
        "",
        f"Generated: {report.generated_at}",
        "",
        "## Database",
        "",
        f"- Database: `{db['database_name']}`",
        f"- Product version: {db['product_version']}",
        f"- Edition: {db['edition']} (engine edition {db['engine_edition']})",
        f"- `@@VERSION`: {db['version_string']}",
        "",
        "## Surface",
        "",
        f"- {surf['total_objects']} visible objects: {surf['views']} views, "
        f"{surf['tables']} base tables, {surf['other']} other.",
        "",
        "## Reconciliation (D-001)",
        "",
        f"- {len(entries)} export names reconciled against the live surface.",
    ]

    by_status: dict[str, int] = {}
    for entry in entries:
        by_status[entry["status"]] = by_status.get(entry["status"], 0) + 1
    for status in ("found_as_view", "found_as_table", "found_as_other", "missing"):
        if status in by_status:
            lines.append(f"  - {status}: {by_status[status]}")
    lines.append(
        f"- {len(extra_objects)} additional objects visible on the surface but not in the "
        "export list."
    )

    discrepancies = [entry for entry in entries if entry["status"] in _DISCREPANCY_STATUSES]
    if discrepancies:
        lines += [
            "",
            "| Export name | Status | Matched object |",
            "|---|---|---|",
        ]
        lines += [
            f"| `{entry['export_name']}` | {entry['status']} | {entry['matched_object'] or '—'} |"
            for entry in discrepancies
        ]

    lines += [
        "",
        "## Row counts + watermark candidates",
        "",
        "| Object | Type | Row count | Status | Duration (ms) | Watermark columns |",
        "|---|---|---|---|---|---|",
    ]
    for stats in row_stats:
        watermark_names = ", ".join(c["column_name"] for c in stats["watermark_columns"]) or "—"
        row_count = stats["row_count"] if stats["row_count"] is not None else "—"
        lines.append(
            f"| `{stats['qualified_name']}` | {stats['object_type']} | {row_count} | "
            f"{stats['row_count_status']} | {stats['duration_ms']} | {watermark_names} |"
        )

    watermark_less = [s["qualified_name"] for s in row_stats if not s["watermark_columns"]]
    lines += [
        "",
        f"- {len(watermark_less)} objects have no `InsertedAt`/`UpdatedAt` watermark candidate.",
    ]
    lines += [f"  - `{name}`" for name in watermark_less]
    lines.append("")
    return "\n".join(lines)


def write_markdown(report: EnvironmentReport, path: Path) -> None:
    """Write the human-readable Markdown artifact. Idempotent: re-running
    with the same report overwrites the file with identical content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_markdown(report), encoding="utf-8")
