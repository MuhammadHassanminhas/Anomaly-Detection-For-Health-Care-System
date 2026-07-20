from pathlib import Path

from cdss._dotenv import load_dotenv, parse_dotenv


def test_parse_dotenv_skips_blank_and_comment_lines() -> None:
    text = "\nA=1\n# comment\nB=2\n"
    assert parse_dotenv(text) == {"A": "1", "B": "2"}


def test_parse_dotenv_strips_whitespace_around_key_and_value() -> None:
    assert parse_dotenv("  KEY  =  value  ") == {"KEY": "value"}


def test_load_dotenv_does_not_override_existing_env_var(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("KEY=from_file\n", encoding="utf-8")
    env = {"KEY": "from_real_env"}
    load_dotenv(env, path=path)
    assert env["KEY"] == "from_real_env"


def test_load_dotenv_fills_in_missing_var(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("KEY=from_file\n", encoding="utf-8")
    env: dict[str, str] = {}
    load_dotenv(env, path=path)
    assert env["KEY"] == "from_file"


def test_load_dotenv_missing_file_is_a_silent_no_op(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    load_dotenv(env, path=tmp_path / "does-not-exist.env")
    assert env == {}
