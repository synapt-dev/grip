"""`gr2 help agent` prints the agent guide, and the guide it prints is ``api/skill.md``.

``api/skill.md`` is generated (scripts/dump_skill.py) and lives outside the package, so the
wheel carries a byte copy, ``gr2/AGENT_GUIDE.md``. These rows keep the copy, the package data
and the printed text equal to the source, byte for byte: the help text goes through Rich
elsewhere, and Rich eats square brackets, so a text compare after a render would hide that.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import typer.main
from typer.testing import CliRunner

from gr2.python_cli import app as app_module
from gr2.python_cli.app import app

GR2 = Path(__file__).resolve().parents[1]
SKILL = GR2 / "api" / "skill.md"
GUIDE = GR2 / "gr2" / "AGENT_GUIDE.md"


def test_the_bundled_guide_is_byte_equal_to_api_skill_md():
    assert GUIDE.read_bytes() == SKILL.read_bytes(), (
        "gr2/AGENT_GUIDE.md differs from api/skill.md: run `python scripts/dump_skill.py --write` from gr2/")


def test_the_guide_ships_as_package_data():
    data = tomllib.loads((GR2 / "pyproject.toml").read_text(encoding="utf-8"))
    assert "AGENT_GUIDE.md" in data["tool"]["setuptools"]["package-data"]["gr2"]


def test_help_agent_prints_the_guide_bytes_raw():
    # The source carries `[` on many lines; a print through the Rich console would drop them.
    assert b"[" in SKILL.read_bytes()
    result = CliRunner().invoke(app, ["help", "agent"])
    assert result.exit_code == 0, result.output
    assert result.stdout_bytes == SKILL.read_bytes()


def test_help_agent_without_the_bundled_guide_exits_1_and_says_reinstall(monkeypatch, tmp_path):
    # The command finds the guide beside the package from the module's __file__; moving that
    # into an empty tmp dir makes the read fail as a missing file would, with the package untouched.
    monkeypatch.setattr(app_module, "__file__", str(tmp_path / "python_cli" / "app.py"))
    result = CliRunner().invoke(app, ["help", "agent"])
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert "AGENT_GUIDE.md" in result.stderr and "reinstall gr2" in result.stderr, result.stderr


def test_help_agent_refuses_extra_words_and_dash_a():
    for args in (["help", "agent", "extra"], ["help", "-a", "agent"]):
        result = CliRunner().invoke(app, args)
        assert result.exit_code == 2, (args, result.output)


def test_no_command_is_named_like_a_topic():
    commands = set(typer.main.get_command(app).commands)
    topics = set(getattr(app_module, "HELP_TOPICS", {}))
    assert topics and not commands & topics, commands & topics
