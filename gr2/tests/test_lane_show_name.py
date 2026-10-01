"""`lane show` is the verb; `lane current` is its hidden alias on the registry's drop path.

`show` prints a stored thing, as `target show` and `lane lease show` already do. The old
spelling must answer exactly as the new one does and must not appear in `lane --help`.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from gr2.prototypes import lane_workspace_prototype as lane_proto
from gr2.python_cli.app import app


def _unit_with_a_current_lane(tmp_path: Path) -> Path:
    path = lane_proto.current_lane_file(tmp_path, "default")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"current": {"lane_name": "feat-x", "owner_unit": "default"}, "recent": []}))
    return tmp_path


def test_lane_show_and_the_hidden_lane_current_print_the_same_thing(tmp_path: Path) -> None:
    ws = _unit_with_a_current_lane(tmp_path)
    runner = CliRunner()
    new = runner.invoke(app, ["lane", "show", str(ws), "default", "--json"])
    old = runner.invoke(app, ["lane", "current", str(ws), "default", "--json"])
    assert new.exit_code == old.exit_code == 0, (new.output, old.output)
    assert new.output == old.output
    assert "feat-x" in new.output, "the output must carry the lane, or equality proves nothing"


def _hidden_flags(group: str) -> dict[str, bool]:
    """{command name: hidden?} read from the registered typer app, not from rendered help:
    the rendering depends on the terminal and the typer version, the registration does not."""
    for info in app.registered_groups:
        if info.name == group:
            return {cmd.name: bool(cmd.hidden) for cmd in info.typer_instance.registered_commands}
    raise AssertionError(f"no group named {group!r} is registered")


def test_the_old_spelling_is_hidden_and_the_new_one_is_listed() -> None:
    flags = _hidden_flags("lane")
    assert flags["show"] is False, "lane show must be a listed verb"
    assert flags["current"] is True, "lane current must be a hidden alias"
