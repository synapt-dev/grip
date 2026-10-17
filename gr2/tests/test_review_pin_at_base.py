"""`gr2 review pin --from-range --at-base`: a frozen range cut on an older dev pins at that base, read-only.

The remote's trunk has moved past the base the range was cut on. With --at-base the pin records the range's base
(not the moved tip) and names it at-base in the bind's own message; nothing in the member clone moves. Without the
flag the pin refuses as before (base_not_live_head). Real git and a bare local remote; the target branch is `trunk`."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from gr2.python_cli.app import app
from tests.native_root_helper import native_root

runner = CliRunner()


def _git(cwd: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@e", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
           "PATH": os.environ.get("PATH", "")}
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
                          check=True).stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name + "\n")
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", name)
    return _git(repo, "rev-parse", "HEAD")


def _world(tmp_path: Path) -> dict:
    """A range BASE..HEAD frozen in `src` (head local-only), then trunk on the remote moved one commit past BASE."""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "trunk", str(remote))
    src = tmp_path / "src"
    _git(tmp_path, "init", "-q", "-b", "trunk", str(src))
    base = _commit(src, "a")
    _git(src, "remote", "add", "origin", str(remote))
    _git(src, "push", "-q", "origin", "trunk")
    _git(src, "checkout", "-qb", "feat")
    head = _commit(src, "b")
    patch = tmp_path / "range.patch"
    patch.write_text(_git(src, "format-patch", "--stdout", f"{base}..{head}") + "\n")
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(remote), str(other))
    tip = _commit(other, "c")
    _git(other, "push", "-q", "origin", "trunk")
    ws = native_root(tmp_path / "ws")
    _git(ws, "config", "user.name", "t")
    _git(ws, "config", "user.email", "t@e")
    return {"remote": remote, "src": src, "ws": ws, "base": base, "head": head, "tip": tip, "patch": patch}


def _pin(w: dict, *extra: str):
    return runner.invoke(app, ["review", "pin", str(w["ws"]), "--repo", "src", "--remote", str(w["remote"]),
                               "--base", w["base"], "--head", w["head"], "--ref", "refs/heads/trunk",
                               "--from-range", str(w["patch"]), *extra])


def _member_state(src: Path) -> tuple[str, str, str, str]:
    """HEAD, the index's tree, the worktree's status, and every ref: what a pin must not move."""
    return (_git(src, "rev-parse", "HEAD"), _git(src, "write-tree"),
            _git(src, "status", "--porcelain", "--untracked-files=all"), _git(src, "for-each-ref"))


def test_at_base_pins_the_frozen_base_when_the_ref_has_moved(tmp_path: Path) -> None:
    w = _world(tmp_path)
    before = _member_state(w["src"])
    result = _pin(w, "--at-base")
    found = re.search(r"gr:[0-9a-f]{40}", result.output)
    assert result.exit_code == 0 and found, result.output
    shown = runner.invoke(app, ["review", "show", str(w["ws"]), found.group(0), "--json"])
    assert shown.exit_code == 0, shown.output
    (member,) = json.loads(shown.output)["members"]
    assert member["base"] == w["base"] and member["head"] == w["head"]
    assert member["at_base"] == w["base"] != w["tip"]
    assert _member_state(w["src"]) == before


def test_without_at_base_a_moved_ref_still_refuses(tmp_path: Path) -> None:
    w = _world(tmp_path)
    before = _member_state(w["src"])
    result = _pin(w)
    assert result.exit_code == 2 and "base_not_live_head" in result.output, result.output
    assert _git(w["ws"], "for-each-ref", "refs/dev.synapt.grip/") == ""
    assert _member_state(w["src"]) == before


def test_at_base_without_a_range_is_refused(tmp_path: Path) -> None:
    w = _world(tmp_path)
    result = runner.invoke(app, ["review", "pin", str(w["ws"]), "--repo", "src", "--remote", str(w["remote"]),
                                 "--base", w["base"], "--head", w["head"], "--ref", "refs/heads/trunk",
                                 "--source", str(w["src"]), "--at-base"])
    assert result.exit_code == 2 and "--at-base requires --from-range" in result.output, result.output


def _side_base(tmp_path: Path, push_side: bool) -> dict:
    """A range cut on an orphan `side` branch whose base trunk never reached; pushed (reachable) or not."""
    w = _world(tmp_path)
    side = tmp_path / "side"
    _git(tmp_path, "clone", "-q", str(w["remote"]), str(side))
    _git(side, "checkout", "-q", "--orphan", "side")
    base = _commit(side, "z")
    if push_side:
        _git(side, "push", "-q", "origin", "side")
    _git(side, "checkout", "-qb", "feat2")
    head = _commit(side, "y")
    w["patch"].write_text(_git(side, "format-patch", "--stdout", f"{base}..{head}") + "\n")
    w.update(base=base, head=head)
    return w


def test_at_base_refuses_a_base_the_ref_never_passed_through(tmp_path: Path) -> None:
    # Row: a base on a side branch trunk never reached pinned, exit 0.
    w = _side_base(tmp_path, push_side=True)
    result = _pin(w, "--at-base")
    assert result.exit_code == 2 and "at_base_not_ancestor" in result.output, result.output
    assert "check: " in result.output and "pin without --at-base" in result.output, result.output
    assert _git(w["ws"], "for-each-ref", "refs/dev.synapt.grip/") == ""


def test_at_base_names_a_base_the_remote_does_not_hold(tmp_path: Path) -> None:
    w = _side_base(tmp_path, push_side=False)
    result = _pin(w, "--at-base")
    assert result.exit_code == 2 and "at_base_unreachable" in result.output, result.output
    assert "corrupt" not in result.output and "check: " in result.output, result.output
    assert _git(w["ws"], "for-each-ref", "refs/dev.synapt.grip/") == ""


def test_at_base_at_the_live_tip_writes_no_at_base_trailer(tmp_path: Path) -> None:
    w = _world(tmp_path)
    tip = w["tip"]
    _git(w["src"], "fetch", "-q", "origin", "trunk")
    _git(w["src"], "rebase", "-q", tip)
    head = _git(w["src"], "rev-parse", "HEAD")
    w["patch"].write_text(_git(w["src"], "format-patch", "--stdout", f"{tip}..{head}") + "\n")
    w.update(base=tip, head=head)
    result = _pin(w, "--at-base")
    found = re.search(r"gr:[0-9a-f]{40}", result.output)
    assert result.exit_code == 0 and found, result.output
    assert "At-base" not in _git(w["ws"], "log", "-1", "--format=%B", found.group(0)[3:])
