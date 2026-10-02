"""`hooks show` and `hooks run` are the verbs; `repo hooks` and `repo hook-run` are hidden
aliases of them on the registry's drop path.

One topic lives in one group: the hook consent verbs (`hooks trust|revoke|status`) were
joined by the hook READ and RUN verbs, which used to sit under `repo`. Each alias must
still answer exactly as its new name does, and must not show in `repo --help`.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from gr2.python_cli.app import app


def _repo_with_hooks(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".gr2").mkdir(parents=True)
    (repo / ".gr2" / "hooks.toml").write_text(
        '[[lifecycle.on_enter]]\nname = "greet"\ncommand = "true"\n'
    )
    return repo


def test_hooks_show_and_the_hidden_repo_hooks_print_the_same_json(tmp_path: Path) -> None:
    repo = _repo_with_hooks(tmp_path)
    runner = CliRunner()
    new = runner.invoke(app, ["hooks", "show", str(repo)])
    old = runner.invoke(app, ["repo", "hooks", str(repo)])
    assert new.exit_code == 0, new.output
    assert old.exit_code == 0, old.output
    assert json.loads(new.output) == json.loads(old.output)
    assert json.loads(new.output), "the parsed hooks must not be empty"


def test_hooks_run_and_the_hidden_repo_hook_run_agree(tmp_path: Path) -> None:
    repo = _repo_with_hooks(tmp_path)
    runner = CliRunner()
    args = [str(tmp_path), str(repo), "on_enter", "--json"]
    new = runner.invoke(app, ["hooks", "run", *args])
    old = runner.invoke(app, ["repo", "hook-run", *args])
    assert new.exit_code == old.exit_code, (new.output, old.output)
    # the unbound-consent notice precedes the JSON on the same stream; parse from the brace
    got = [json.loads(r.output[r.output.index("{"):]) for r in (new, old)]
    assert got[0]["stage"] == got[1]["stage"] == "on_enter"
    assert got[0]["results"] == got[1]["results"]


def _hidden_flags(group: str) -> dict[str, bool]:
    """{command name: hidden?} read from the registered typer app, not from rendered help:
    the rendering depends on the terminal and the typer version, the registration does not."""
    for info in app.registered_groups:
        if info.name == group:
            return {cmd.name: bool(cmd.hidden) for cmd in info.typer_instance.registered_commands}
    raise AssertionError(f"no group named {group!r} is registered")


def test_the_old_repo_spellings_are_hidden_but_the_new_ones_are_listed() -> None:
    repo, hooks = _hidden_flags("repo"), _hidden_flags("hooks")
    assert repo["hooks"] is True and repo["hook-run"] is True, "the old spellings must be hidden aliases"
    assert hooks["show"] is False and hooks["run"] is False, "the new spellings must be listed verbs"
