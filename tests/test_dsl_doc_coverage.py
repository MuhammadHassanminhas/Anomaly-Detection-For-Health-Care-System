"""Phase 2 exit criterion 5: `docs/dsl.md` and `check-dsl.schema.json` are
consistent -- every construct named in one has a corresponding shape in the
other, verified mechanically here rather than by eye. Each check below
extracts the same construct set from both sources independently (regex over
the doc's markdown, the loaded JSON Schema) and asserts they're equal --
catches drift in *either* direction: the doc naming something the schema
doesn't enforce, or the schema enforcing something the doc never explains.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

DOC_PATH = Path(__file__).parent.parent / "docs" / "dsl.md"
SCHEMA_PATH = Path(__file__).parent.parent / "src" / "cdss" / "schemas" / "check-dsl.schema.json"

DOC_TEXT = DOC_PATH.read_text(encoding="utf-8")
SCHEMA: dict[str, Any] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _section(heading: str) -> str:
    """Text between a `## <heading>` line and the next `## ` heading (or EOF)."""
    pattern = re.compile(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
    match = pattern.search(DOC_TEXT)
    assert match is not None, f"docs/dsl.md has no '## {heading}' section"
    return match.group(1)


def _first_yaml_block(text: str) -> str:
    match = re.search(r"```yaml\n(.*?)```", text, re.DOTALL)
    assert match is not None, "expected a ```yaml fenced block"
    return match.group(1)


# --- top-level shape -----------------------------------------------------------


def _doc_top_level_keys() -> set[str]:
    block = _first_yaml_block(_section("Top-level shape"))
    return {m.group(1) for m in re.finditer(r"^([a-z_]+):", block, re.MULTILINE)}


def test_top_level_keys_match_schema_required() -> None:
    assert _doc_top_level_keys() == set(SCHEMA["required"])


# --- entity fields ---------------------------------------------------------------


def _doc_entity_keys() -> set[str]:
    section = _section("Top-level shape")
    block = re.search(r"```yaml\nentity:\n(.*?)```", section, re.DOTALL)
    assert block is not None, "expected the `entity:` example block"
    return {m.group(1) for m in re.finditer(r"^  ([a-z_]+):", block.group(1), re.MULTILINE)}


def test_entity_keys_match_schema_required() -> None:
    assert _doc_entity_keys() == set(SCHEMA["properties"]["entity"]["required"])


# --- predicate node constructs ---------------------------------------------------


def _doc_predicate_node_keywords() -> set[str]:
    section = _section("Predicate nodes")
    return {m.group(1) for m in re.finditer(r"\| `([a-z_]+):", section)}


def _schema_predicate_node_keywords() -> set[str]:
    branches = SCHEMA["$defs"]["predicateNode"]["oneOf"]
    keywords: set[str] = set()
    for branch in branches:
        if "required" in branch:
            keywords.update(branch["required"])
    return keywords


def test_predicate_node_keywords_match_schema() -> None:
    assert _doc_predicate_node_keywords() == _schema_predicate_node_keywords()


def _doc_exists_clause_fields() -> set[str]:
    section = _section("Predicate nodes")
    match = re.search(r"`exists: \{([^}]*)\}`", section)
    assert match is not None, "expected the `exists: {...}` shape row"
    return {token.strip().rstrip("?") for token in match.group(1).split(",")}


def test_exists_clause_fields_match_schema() -> None:
    schema_fields = set(SCHEMA["$defs"]["existsClause"]["properties"])
    assert _doc_exists_clause_fields() == schema_fields


# --- typed params: type enum + default strategies --------------------------------


def _doc_param_type_enum() -> set[str]:
    section = _section("Typed params")
    match = re.search(r"type: integer\s+# (.+)$", section, re.MULTILINE)
    assert match is not None, "expected the `type: integer  # ...` enum comment"
    return {token.strip() for token in match.group(1).split("|")}


def test_param_type_enum_matches_schema() -> None:
    schema_enum = set(SCHEMA["$defs"]["paramDef"]["properties"]["type"]["enum"])
    assert _doc_param_type_enum() == schema_enum


def _doc_param_default_strategies() -> set[str]:
    section = _section("Typed params")
    return set(re.findall(r"strategy: (\w+)", section))


def _schema_param_default_strategies() -> set[str]:
    branches = SCHEMA["$defs"]["paramDefault"]["oneOf"]
    return {branch["properties"]["strategy"]["const"] for branch in branches}


def test_param_default_strategies_match_schema() -> None:
    assert _doc_param_default_strategies() == _schema_param_default_strategies()


# --- category / severity enums ----------------------------------------------------


def _doc_enum_comment(field_prefix: str) -> set[str]:
    block = _first_yaml_block(_section("Top-level shape"))
    match = re.search(rf"^{field_prefix}: \S+\s+# (.+)$", block, re.MULTILINE)
    assert match is not None, f"expected an enum comment on the '{field_prefix}' line"
    return {token.strip() for token in match.group(1).split("|")}


def test_category_enum_matches_schema() -> None:
    assert _doc_enum_comment("category") == set(SCHEMA["properties"]["category"]["enum"])


def test_default_severity_enum_matches_schema() -> None:
    assert _doc_enum_comment("default_severity") == set(
        SCHEMA["properties"]["default_severity"]["enum"]
    )
