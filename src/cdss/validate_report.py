"""python -m cdss.validate_report <path> — validates an env-report.json
artifact against src/cdss/schemas/env-report.schema.json. Exits 0 if valid,
1 otherwise. Phase 0 exit criterion 3 / gatekeeper command.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import jsonschema

from cdss.report import SCHEMA_PATH, load_schema


def validate_file(path: Path, *, schema_path: Path = SCHEMA_PATH) -> str | None:
    """Return None if `path` validates against the schema, else the error message."""
    data = json.loads(path.read_text(encoding="utf-8"))
    try:
        jsonschema.validate(instance=data, schema=load_schema(schema_path))
    except jsonschema.ValidationError as exc:
        return str(exc)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a CDSS env-report.json artifact.")
    parser.add_argument("report_path", type=Path)
    args = parser.parse_args(argv)

    error = validate_file(args.report_path)
    if error is not None:
        print(f"INVALID: {args.report_path}\n{error}")
        return 1
    print(f"VALID: {args.report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
