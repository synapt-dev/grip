"""`store commit` writes `grip.toml` as TOML a reader can load back, whatever the hand-written tables hold.

THE DEFECT, measured on dev a59c78d3: the writer emitted strings with `json.dumps`, and JSON writes a character
outside the Basic Multilingual Plane as a surrogate pair (`\\ud83d\\ude00`), which TOML refuses ("Escaped character
is not a Unicode scalar value"), so one such character in a member path or in a hand-written `[[sets]]` table made
the next read of the committed `grip.toml` fail. A TOML date or datetime in a hand-written table had no literal at
all and stopped `store commit` with a ValueError.

THE SPECIFIED SHAPE: every literal the writer emits reads back, through the real TOML reader, as the value it was
given, and the file stays pure ASCII (non-ASCII text is written as TOML escapes), as it was before.
"""
from __future__ import annotations

import datetime
import tomllib
from pathlib import Path

import pytest

from gr2.python_cli import grip_cli
from tests.test_store_git_hooks import _store
from tests.test_store_git_native_smoke import git, gr2

STRINGS = [
    "set-\U0001F600",            # outside the BMP: JSON's surrogate pair is not TOML
    "café",                 # BMP, non-ASCII
    "del\x7fchar",               # DEL: JSON leaves it raw, TOML forbids it raw
    'quote " and back\\slash',
    "line\nbreak\tand tab",
    "nul\x00byte",
    "plain",
]


def _loads(literal: str):
    return tomllib.loads(f"k = {literal}\n")["k"]


@pytest.mark.parametrize("text", STRINGS)
def test_a_string_literal_reads_back_as_the_string(text):
    assert _loads(grip_cli._toml_value(text)) == text


@pytest.mark.parametrize("text", STRINGS)
def test_a_string_literal_is_pure_ascii(text):
    """No reader then has an encoding question: git's output is decoded with the locale encoding on Windows."""
    assert grip_cli._toml_value(text).isascii() and grip_cli._toml_key(text).isascii()


@pytest.mark.parametrize("text", STRINGS)
def test_a_key_reads_back_as_the_key(text):
    key = grip_cli._toml_key(text)
    assert tomllib.loads(f"{key} = 1\n") == {text: 1}


@pytest.mark.parametrize("value", [
    datetime.date(2026, 10, 11),
    datetime.datetime(2026, 10, 11, 3, 36, 0, tzinfo=datetime.timezone.utc),
    datetime.datetime(2026, 10, 11, 3, 36, 0),
    datetime.time(3, 36, 0),
])
def test_a_toml_date_or_time_reads_back_as_the_same_value(value):
    assert _loads(grip_cli._toml_value(value)) == value


def test_nested_values_read_back_whole():
    value = {"when": datetime.date(2026, 10, 11), "tags": ["a", "set-\U0001F600"], "n": 3, "ok": True}
    assert _loads(grip_cli._toml_value(value)) == value


def test_the_members_writer_reads_back_a_member_path_outside_the_bmp(tmp_path: Path):
    member = {"name": "m-\U0001F600", "path": "dir-\U0001F600/x", "remote": "/r/\U0001F600", "pin": "0" * 40}
    grip_cli._write_native_members(tmp_path, [member])

    loaded = tomllib.loads((tmp_path / "grip.toml").read_text(encoding="utf-8"))["members"][0]

    assert loaded["name"] == member["name"] and loaded["path"] == member["path"]
    assert loaded["remotes"]["origin"] == member["remote"]
    assert (tmp_path / "grip.toml").read_bytes().isascii()


def test_store_commit_carries_a_hand_written_set_with_a_date_and_an_emoji_name(tmp_path: Path):
    root = _store(tmp_path)
    text = (root / "grip.toml").read_text(encoding="utf-8")
    (root / "grip.toml").write_text(
        text.rstrip("\n") + '\n\n[[sets]]\nname = "set-\U0001F600"\nmembers = ["alpha", "beta"]\nsince = 2026-10-11\n',
        encoding="utf-8",
    )

    result = gr2(root, "commit", "-m", "a set with a date and an emoji", check=False)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert git(root, "show", "HEAD:grip.toml").stdout.isascii()
    committed = tomllib.loads(git(root, "show", "HEAD:grip.toml").stdout)
    assert committed["sets"] == [{"name": "set-\U0001F600", "members": ["alpha", "beta"], "since": datetime.date(2026, 10, 11)}]
