"""Phase 2 step 4: every checked-in example check compiles to a checked-in
golden `.sql` file (`tests/golden/<check-id>.sql`); any diff fails this test.
Golden files are generated once (compiled, then reviewed) and are the source
of truth for future compiler changes -- a deliberate compiler-output change
updates the golden file in the same commit, not silently.
"""

from __future__ import annotations

from pathlib import Path

from cdss.compiler import compile_check
from cdss.dsl import parse_check_document

EXAMPLES_DIR = Path(__file__).parent.parent / "examples" / "checks"
GOLDEN_DIR = Path(__file__).parent / "golden"


def _compile_example(path: Path) -> str:
    doc = parse_check_document(path.read_text(encoding="utf-8"))
    return compile_check(doc).sql_text


def test_golden_dir_has_exactly_one_sql_file_per_example() -> None:
    example_stems = {p.stem for p in EXAMPLES_DIR.glob("*.yaml")}
    golden_stems = {p.stem for p in GOLDEN_DIR.glob("*.sql")}
    assert example_stems == golden_stems


def test_every_example_compiles_to_its_golden_sql() -> None:
    for path in sorted(EXAMPLES_DIR.glob("*.yaml")):
        golden_path = GOLDEN_DIR / f"{path.stem}.sql"
        compiled_sql = _compile_example(path)
        expected_sql = golden_path.read_text(encoding="utf-8")
        assert compiled_sql == expected_sql, f"compiled SQL diverged from {golden_path}"


def test_every_example_double_compiles_byte_identical() -> None:
    for path in sorted(EXAMPLES_DIR.glob("*.yaml")):
        assert _compile_example(path) == _compile_example(path)
