"""Phase 1 step 8: catalog assembly, versioning, and hashing tests. Pure
functions -- no DB access. Fixture data is entirely synthetic.
"""

import json
from pathlib import Path

import jsonschema
import pytest

from cdss.catalog import (
    compute_artifact_hash,
    next_catalog_version,
    validate_catalog_dict,
    write_json,
    write_manifest_entry,
)

MINIMAL_CATALOG = {
    "catalog_version": 1,
    "produced_at": "2026-07-16T00:00:00+00:00",
    "source_database": "INDICI_BI_Full",
    "views": [],
    "relationships": [],
    "profiling_costs": [],
    "pruning_report": {
        "pairs_considered": 0,
        "pairs_pruned": 0,
        "pairs_evaluated": 0,
        "pairs_skipped_cost": 0,
    },
}


def test_validate_catalog_dict_accepts_minimal_catalog() -> None:
    validate_catalog_dict(MINIMAL_CATALOG)


def test_validate_catalog_dict_rejects_missing_field() -> None:
    invalid = {k: v for k, v in MINIMAL_CATALOG.items() if k != "views"}
    with pytest.raises(jsonschema.ValidationError):
        validate_catalog_dict(invalid)


def test_next_catalog_version_empty_directory_returns_1(tmp_path: Path) -> None:
    assert next_catalog_version(tmp_path) == 1


def test_next_catalog_version_missing_directory_returns_1(tmp_path: Path) -> None:
    assert next_catalog_version(tmp_path / "does-not-exist") == 1


def test_next_catalog_version_returns_max_plus_one(tmp_path: Path) -> None:
    (tmp_path / "semantic-catalog-v1.json").write_text("{}")
    (tmp_path / "semantic-catalog-v3.json").write_text("{}")
    (tmp_path / "semantic-catalog-v2.json").write_text("{}")
    assert next_catalog_version(tmp_path) == 4


def test_next_catalog_version_ignores_unrelated_files(tmp_path: Path) -> None:
    (tmp_path / "semantic-catalog-v1.json").write_text("{}")
    (tmp_path / "manifest.jsonl").write_text("")
    (tmp_path / ".profile-checkpoint.json").write_text("{}")
    assert next_catalog_version(tmp_path) == 2


def test_compute_artifact_hash_is_deterministic(tmp_path: Path) -> None:
    path = tmp_path / "artifact.json"
    path.write_text('{"a": 1}', encoding="utf-8")
    assert compute_artifact_hash(path) == compute_artifact_hash(path)


def test_compute_artifact_hash_differs_for_different_content(tmp_path: Path) -> None:
    path_a = tmp_path / "a.json"
    path_b = tmp_path / "b.json"
    path_a.write_text('{"a": 1}', encoding="utf-8")
    path_b.write_text('{"a": 2}', encoding="utf-8")
    assert compute_artifact_hash(path_a) != compute_artifact_hash(path_b)


def test_write_json_validates_and_writes(tmp_path: Path) -> None:
    path = tmp_path / "semantic-catalog-v1.json"
    write_json(MINIMAL_CATALOG, path)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["catalog_version"] == 1


def test_write_json_rejects_invalid_catalog(tmp_path: Path) -> None:
    invalid = {k: v for k, v in MINIMAL_CATALOG.items() if k != "views"}
    path = tmp_path / "semantic-catalog-v1.json"
    with pytest.raises(jsonschema.ValidationError):
        write_json(invalid, path)
    assert not path.exists()


def test_write_manifest_entry_appends_jsonl_line(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.jsonl"
    write_manifest_entry(
        manifest_path,
        catalog_version=1,
        artifact_path="artifacts/catalog/semantic-catalog-v1.json",
        sha256="abc123",
        produced_at="2026-07-16T00:00:00+00:00",
    )
    write_manifest_entry(
        manifest_path,
        catalog_version=2,
        artifact_path="artifacts/catalog/semantic-catalog-v2.json",
        sha256="def456",
        produced_at="2026-07-17T00:00:00+00:00",
    )
    lines = manifest_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first == {
        "catalog_version": 1,
        "artifact_path": "artifacts/catalog/semantic-catalog-v1.json",
        "sha256": "abc123",
        "produced_at": "2026-07-16T00:00:00+00:00",
    }
