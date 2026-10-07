"""lane exit must not move a lane's uncommitted work out of sight.

Measured on released 2.0.0a6 (README quickstart walk): after a commit failed on a missing Git
identity, `gr2 lane exit` printed "status": "ok" and exited 0, and the staged edit was gone from the
lane. It had been put in `git stash` in the lane repo, which nothing said and which `lane enter`
never restores. Exit now refuses a lane with uncommitted work unless the caller asks for a stash
(`--dirty stash`), and a stash it makes is named in the output with the command that restores it.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import app as gr2_app
from tests.conftest import make_cli_runner
from tests.test_lane_enter_hook_warnings import _workspace_with_enter_hook

runner = make_cli_runner()


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", *a], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def _entered_lane(tmp_path: Path, hook: str = "exit 0") -> tuple[Path, Path]:
    ws, _, _ = _workspace_with_enter_hook(tmp_path, command=hook, stage_key="on_exit")
    for argv in (["lane", "create", str(ws), "atlas", "x", "--repos", "app", "--branch", "app=feat/x"],
                 ["lane", "enter", str(ws), "atlas", "x", "--actor", "agent:s"]):
        result = runner.invoke(gr2_app.app, argv)
        assert result.exit_code == 0, result.output
    repo = gr2_app._lane_repo_root(ws, "atlas", "x", "app")
    assert (repo / "f.txt").read_text() == "base\n"
    return ws, repo


def _exit(ws: Path, *extra: str):
    return runner.invoke(gr2_app.app, ["lane", "exit", str(ws), "atlas", "--actor", "agent:s", *extra])


def _current_lane(ws: Path) -> str | None:
    from gr2.prototypes import lane_workspace_prototype as lane_proto
    path = lane_proto.current_lane_file(ws, "atlas")
    current = json.loads(path.read_text()).get("current") if path.exists() else None
    return current["lane_name"] if current else None


@pytest.mark.parametrize("kind", ["staged", "unstaged", "untracked"])
def test_exit_refuses_a_lane_with_uncommitted_work_and_leaves_it_in_place(tmp_path: Path, kind: str) -> None:
    ws, repo = _entered_lane(tmp_path)
    name = "new.txt" if kind == "untracked" else "f.txt"
    (repo / name).write_text("edited\n")
    if kind == "staged":
        _git(repo, "add", name)
    before = _git(repo, "status", "--porcelain")

    result = _exit(ws)

    assert result.exit_code != 0, result.output
    assert "app" in result.stderr and "--dirty stash" in result.stderr, result.stderr
    assert '"ok"' not in result.output
    assert (repo / name).read_text() == "edited\n"
    assert _git(repo, "status", "--porcelain") == before
    assert _git(repo, "stash", "list") == ""
    assert _current_lane(ws) == "x"


def test_exit_of_a_clean_lane_is_ok(tmp_path: Path) -> None:
    ws, repo = _entered_lane(tmp_path)
    result = _exit(ws)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok" and payload["stashed"] == [], result.output
    assert _current_lane(ws) != "x"


def test_exit_with_dirty_stash_names_the_stash_and_how_to_restore_it(tmp_path: Path) -> None:
    ws, repo = _entered_lane(tmp_path)
    (repo / "f.txt").write_text("edited\n")
    _git(repo, "add", "f.txt")

    result = _exit(ws, "--dirty", "stash")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    [row] = payload["stashed"]
    assert row["repo"] == "app" and row["path"] == str(repo), payload
    assert row["stash"] == _git(repo, "rev-parse", "stash@{0}"), payload
    assert row["restore"] == f"git -C {repo} stash apply --index {row['stash']}", payload
    assert row["restore"] in result.stderr and row["stash"] in result.stderr
    assert _git(repo, "show", "stash@{0}:f.txt") == "edited"
    assert _current_lane(ws) != "x"


def test_an_unknown_dirty_mode_is_refused_before_anything_moves(tmp_path: Path) -> None:
    ws, repo = _entered_lane(tmp_path)
    (repo / "f.txt").write_text("edited\n")
    result = _exit(ws, "--dirty", "discard")
    assert result.exit_code != 0, result.output
    assert (repo / "f.txt").read_text() == "edited\n"
    assert _git(repo, "stash", "list") == ""
    assert _current_lane(ws) == "x"


def test_an_on_exit_hook_that_leaves_work_in_the_lane_is_refused(tmp_path: Path) -> None:
    ws, repo = _entered_lane(tmp_path, hook="echo hooked > from-hook.txt")
    result = _exit(ws)
    assert result.exit_code != 0, result.output
    assert "after_on_exit" in result.stderr and "app" in result.stderr, result.stderr
    assert '"ok"' not in result.output
    assert (repo / "from-hook.txt").read_text() == "hooked\n"
    assert _git(repo, "stash", "list") == ""
    assert _current_lane(ws) == "x"


def test_the_refusal_names_a_command_that_can_be_pasted(tmp_path: Path) -> None:
    ws, repo = _entered_lane(tmp_path)
    (repo / "f.txt").write_text("edited\n")
    result = _exit(ws)
    assert f"gr2 lane exit {ws} atlas --dirty stash" in result.stderr, result.stderr


def test_a_refusal_after_a_stash_still_prints_the_stash_it_made(tmp_path: Path) -> None:
    ws, repo = _entered_lane(tmp_path, hook="echo hooked > from-hook.txt")
    (repo / "f.txt").write_text("edited\n")
    result = _exit(ws, "--dirty", "stash")
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "refused" and payload["current_lane"] == "x", payload
    assert payload["refusal"] == "lane_has_uncommitted_work_after_on_exit", payload
    [row] = payload["stashed"]
    assert row["stash"] == _git(repo, "rev-parse", "stash@{0}") and row["stash"] in result.stderr, payload
    assert _git(repo, "show", "stash@{0}:f.txt") == "edited"
    assert (repo / "from-hook.txt").read_text() == "hooked\n"
    assert _current_lane(ws) == "x"


def test_a_plain_refusal_prints_a_receipt_naming_the_repo(tmp_path: Path) -> None:
    ws, repo = _entered_lane(tmp_path)
    (repo / "f.txt").write_text("edited\n")
    payload = json.loads(_exit(ws).stdout)
    assert payload["refusal"] == "lane_has_uncommitted_work" and payload["stashed"] == [], payload
    assert payload["dirty"] == [{"repo": "app", "path": str(repo)}], payload


def test_the_printed_restore_brings_back_this_stash_with_its_index_after_a_later_stash(tmp_path: Path) -> None:
    ws, repo = _entered_lane(tmp_path)
    (repo / "f.txt").write_text("staged edit\n")
    _git(repo, "add", "f.txt")
    (repo / "f.txt").write_text("unstaged on top\n")
    index_before = _git(repo, "diff", "--cached")
    tree_before = _git(repo, "diff")
    row = json.loads(_exit(ws, "--dirty", "stash").stdout)["stashed"][0]
    (repo / "other.txt").write_text("a later, different stash\n")
    _git(repo, "stash", "push", "-u", "-q", "-m", "later")
    subprocess.run(row["restore"], shell=True, check=True, capture_output=True)
    assert _git(repo, "diff", "--cached") == index_before
    assert _git(repo, "diff") == tree_before
    assert row["stash"] in _git(repo, "stash", "list", "--format=%H").split()


def test_a_blocking_on_exit_hook_after_a_stash_still_prints_the_receipt(tmp_path: Path) -> None:
    ws, _, _ = _workspace_with_enter_hook(tmp_path, command="exit 4", stage_key="on_exit",
                                          extra='on_failure = "block"\n')
    for argv in (["lane", "create", str(ws), "atlas", "x", "--repos", "app", "--branch", "app=feat/x"],
                 ["lane", "enter", str(ws), "atlas", "x", "--actor", "agent:s"]):
        assert runner.invoke(gr2_app.app, argv).exit_code == 0
    repo = gr2_app._lane_repo_root(ws, "atlas", "x", "app")
    (repo / "f.txt").write_text("edited\n")
    result = _exit(ws, "--dirty", "stash")
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    assert payload["refusal"] == "exit_step_failed" and payload["current_lane"] == "x", payload
    [row] = payload["stashed"]
    assert row["stash"] == _git(repo, "rev-parse", "stash@{0}"), payload
    assert _current_lane(ws) == "x"


def test_an_unknown_mode_prints_a_receipt(tmp_path: Path) -> None:
    ws, repo = _entered_lane(tmp_path)
    result = _exit(ws, "--dirty", "discard")
    assert result.exit_code == 2 and json.loads(result.stdout)["refusal"] == "unknown_dirty_mode", result.output
