"""`review pin --help` names every flag a single --from-range row still needs.

A frozen range.patch carries the range, not the row: a single row refuses unless --repo, --remote, --base and --head
are all given. This row reads the refusal's own flag list and the rendered help, so it fails when either one moves.
"""
from __future__ import annotations

import re
from pathlib import Path

from gr2.python_cli.app import app
from tests.conftest import make_cli_runner
from tests.test_review_a4 import _flat

WIDE = {"COLUMNS": "400", "TERMINAL_WIDTH": "400"}


def _from_range_help() -> str:
    result = make_cli_runner().invoke(app, ["review", "pin", "--help"], env=WIDE)
    assert result.exit_code == 0, result.output
    text = _flat(result.stdout)
    start = text.index("--from-range <path>")
    return text[start:text.index("--at-base", start)]


def _refusal_flags(tmp_path: Path) -> list[str]:
    """The flags the single-row refusal names when a user gives only --base, --ref and --from-range."""
    patch = tmp_path / "range.patch"
    patch.write_text("From 0000000000000000000000000000000000000000 Mon Sep 17 00:00:00 2001\n")
    result = make_cli_runner().invoke(
        app, ["review", "pin", str(tmp_path), "--repo", "grip", "--base", "1" * 40, "--ref", "dev",
              "--from-range", str(patch)], env=WIDE)
    assert result.exit_code == 2, result.output
    text = _flat(result.output)
    match = re.search(r"((?:--[a-z]+(?:, )?)+) required without --rows-json", text)
    assert match, text
    return re.findall(r"--[a-z]+", match.group(1))


def test_the_from_range_help_names_every_flag_the_single_row_refusal_names(tmp_path):
    flags = _refusal_flags(tmp_path)
    assert "--remote" in flags and "--head" in flags, flags
    help_text = _from_range_help()
    for flag in flags:
        assert flag in help_text, (flag, help_text)


def test_the_from_range_help_names_all_four_row_flags():
    help_text = _from_range_help()
    for flag in ("--repo", "--remote", "--base", "--head"):
        assert flag in help_text, (flag, help_text)
