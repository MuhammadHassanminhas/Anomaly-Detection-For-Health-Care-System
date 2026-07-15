"""Phase 0 step 6: reconcile the export's named objects against the
enumerated surface (D-001).

This makes no judgment call — it only tabulates found-as-view /
found-as-table / found-as-other / missing / extra. Ruling on what the
discrepancies mean is the product owner's, not code's.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cdss.surface import SurfaceObject

ReconciliationStatus = Literal["found_as_view", "found_as_table", "found_as_other", "missing"]

_STATUS_BY_OBJECT_TYPE: dict[str, ReconciliationStatus] = {
    "view": "found_as_view",
    "table": "found_as_table",
    "other": "found_as_other",
}


def load_export_names(path: Path) -> list[str]:
    """Extract the "table" field from each entry of the export JSON
    (schema_for_SQL_PROJ.txt, D-017: unverified documentation, used only as
    the list of names to reconcile — never as a schema authority)."""
    entries = json.loads(path.read_text(encoding="utf-8"))
    return [str(entry["table"]) for entry in entries]


@dataclass(frozen=True)
class ReconciliationEntry:
    export_name: str
    status: ReconciliationStatus
    matched_object: str | None


@dataclass(frozen=True)
class ReconciliationResult:
    entries: list[ReconciliationEntry]
    extra_objects: list[str]


def _split_qualified_name(qualified_name: str) -> tuple[str, str]:
    schema, _, name = qualified_name.partition(".")
    return schema.lower(), name.lower()


def reconcile(
    export_names: Sequence[str], surface: Sequence[SurfaceObject]
) -> ReconciliationResult:
    index: dict[tuple[str, str], SurfaceObject] = {
        (obj.schema.lower(), obj.name.lower()): obj for obj in surface
    }

    matched_keys: set[tuple[str, str]] = set()
    entries: list[ReconciliationEntry] = []
    for export_name in export_names:
        key = _split_qualified_name(export_name)
        matched = index.get(key)
        if matched is None:
            entries.append(
                ReconciliationEntry(export_name=export_name, status="missing", matched_object=None)
            )
            continue
        matched_keys.add(key)
        entries.append(
            ReconciliationEntry(
                export_name=export_name,
                status=_STATUS_BY_OBJECT_TYPE[matched.object_type],
                matched_object=matched.qualified_name,
            )
        )

    extra_objects = sorted(
        obj.qualified_name
        for obj in surface
        if (obj.schema.lower(), obj.name.lower()) not in matched_keys
    )
    return ReconciliationResult(entries=entries, extra_objects=extra_objects)
