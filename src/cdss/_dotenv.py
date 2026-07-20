"""Tiny stdlib `.env` loader -- a handful of KEY=VALUE lines don't need the
`python-dotenv` dependency. Real environment variables always win: this only
fills in names the process doesn't already have set, so nothing set by the
shell, CI, or a test's `monkeypatch` is ever overridden by the file.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from pathlib import Path

_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def parse_dotenv(text: str) -> dict[str, str]:
    """`KEY=VALUE` per line; blank lines, `#`-comments, and lines with no
    `=` are ignored. No quoting support -- the real `.env` file has none."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def load_dotenv(env: MutableMapping[str, str], path: Path = _ENV_PATH) -> None:
    if not path.is_file():
        return
    for key, value in parse_dotenv(path.read_text(encoding="utf-8")).items():
        env.setdefault(key, value)
