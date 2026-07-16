"""Phase 1 step 8: semantic catalog assembly, versioning, and validation.

Pure functions -- no DB access. Versions the artifact (`next_catalog_version`
-- idempotent: each successful run bumps to a new version rather than
overwriting) and records a version+hash manifest line per run
(`write_manifest_entry`), since the catalog schema's `additionalProperties:
false` (step 1) leaves no room for a hash field inside the catalog itself.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import jsonschema

SCHEMA_PATH = Path(__file__).parent / "schemas" / "semantic-catalog.schema.json"
_VERSION_RE = re.compile(r"semantic-catalog-v(\d+)\.json$")


def load_schema(schema_path: Path = SCHEMA_PATH) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(schema_path.read_text(encoding="utf-8"))
    return result


def validate_catalog_dict(data: dict[str, Any], schema_path: Path = SCHEMA_PATH) -> None:
    jsonschema.validate(instance=data, schema=load_schema(schema_path))


def next_catalog_version(catalog_dir: Path) -> int:
    """Idempotent versioning: scans `catalog_dir` for existing
    `semantic-catalog-v<N>.json` files, returns max(N) + 1 (1 if none, or if
    the directory doesn't exist yet)."""
    if not catalog_dir.is_dir():
        return 1
    versions = [
        int(match.group(1))
        for path in catalog_dir.iterdir()
        if (match := _VERSION_RE.search(path.name)) is not None
    ]
    return max(versions, default=0) + 1


def compute_artifact_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(data: dict[str, Any], path: Path, *, schema_path: Path = SCHEMA_PATH) -> None:
    """Schema-validates before writing anything to disk. Idempotent:
    re-running with identical `data` overwrites the file with identical
    content."""
    validate_catalog_dict(data, schema_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_manifest_entry(
    manifest_path: Path,
    *,
    catalog_version: int,
    artifact_path: str,
    sha256: str,
    produced_at: str,
) -> None:
    """Appends one JSONL line recording this run's catalog version + artifact
    hash + timestamp -- the provenance record the schema itself has no room
    for."""
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "catalog_version": catalog_version,
        "artifact_path": artifact_path,
        "sha256": sha256,
        "produced_at": produced_at,
    }
    with manifest_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
