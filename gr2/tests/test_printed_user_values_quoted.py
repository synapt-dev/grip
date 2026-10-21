"""A printed command a person is told to run quotes the values they typed into the command that printed it.

Each row drives the verb with a value that needs quoting (a space or a `;`), takes the printed command, runs it in
`sh` with printf standing in for gr2, and asserts the value comes back as ONE argument. Unquoted, the space splits
the value and the `;` ends the command. Each row goes red if its site prints the raw value again.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from gr2.python_cli.app import app
from tests.test_lane_create_defaults import _create, _flat, _push_branch, _workspace
from tests.test_lane_create_refusal_leaves_nothing import _blocked_hook_workspace

runner = CliRunner()


def _argv(printed: str) -> list[str]:
    """The argv a shell builds from the printed line: its first word is swapped for printf, which prints each argument
    NUL-terminated, so a split value or a second command shows in the list."""
    words = printed.split(" ", 1)
    out = subprocess.run(["sh", "-c", "printf '%s\\0' " + words[1]], capture_output=True, text=True)
    return [words[0], *out.stdout.split("\0")[:-1]]


def _missing_repo_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "w s;nosuchcmd" / "ws"
    (ws / ".grip").mkdir(parents=True)
    (ws / ".grip" / "workspace_spec.toml").write_text(
        'schema_version = 1\nworkspace_name = "m"\n\n'
        '[[repos]]\nname = "app"\npath = "repos/app"\nurl = "https://example.invalid/app.git"\n\n'
        '[[units]]\nname = "atlas"\npath = "agents/atlas"\nrepos = ["app"]\n'
    )
    return ws


def _apply_line(output: str) -> str:
    m = re.search(r"run `(gr2 apply [^`]*)` first", output)
    assert m, output
    return m.group(1)


def test_review_checkout_pr_prints_the_workspace_as_one_argument(tmp_path: Path) -> None:
    ws = _missing_repo_workspace(tmp_path)
    res = runner.invoke(app, ["review", "checkout-pr", str(ws), "atlas", "app", "7"])
    assert res.exit_code != 0
    assert _argv(_apply_line(res.output)) == ["gr2", "apply", str(ws.resolve()), "--yes"]


def test_review_open_pr_head_prints_the_workspace_as_one_argument(tmp_path: Path) -> None:
    ws = _missing_repo_workspace(tmp_path)
    res = runner.invoke(app, ["review", "open", str(ws), "atlas", "app", "7"])
    assert res.exit_code != 0
    assert "shared repo missing for review open" in res.output, res.output
    assert _argv(_apply_line(res.output)) == ["gr2", "apply", str(ws.resolve()), "--yes"]


def test_lane_create_collision_prints_the_lane_name_branch_as_one_argument(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path, ["alpha", "beta"])
    _push_branch(ws, "beta", "de;mo")
    res = runner.invoke(app, ["lane", "create", str(ws), "atlas", "de;mo"])
    assert res.exit_code == 2
    m = re.search(r"pass (--branch \S+) to use the existing branch", _flat(res))
    assert m, _flat(res)
    assert _argv("gr2 " + m.group(1)) == ["gr2", "--branch", "de;mo"]


def test_a_kept_lane_prints_its_continue_command_with_each_value_as_one_argument(tmp_path: Path) -> None:
    ws = _blocked_hook_workspace(tmp_path / "w s;nosuchcmd")
    res = runner.invoke(app, ["lane", "create", str(ws), "atlas", "fe;at", "--repos", "app", "--branch", "feat/lane"])
    assert res.exit_code == 1, res.output
    combined = res.output + str(res.stderr)
    assert "the lane 'atlas/fe;at' was KEPT" in combined, combined
    m = re.search(r"continue with it: (.*)\n", combined)
    assert m, combined
    assert _argv(m.group(1)) == ["gr2", "review", "create-project", str(ws), "atlas", "fe;at"]
