"""Full mode's texts, each pinned by equality: the --mode help of store init, the one-branch refusal with its next
step, the first line of status, the commit line at the root, and the store-init advice for a root with no spec."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import typer

from gr2.python_cli import griplink
from gr2.python_cli.app import app
from gr2.python_cli.griplink import GriplinkRefusal

from tests.test_griplink_cli import _edit_and_stage, gr2
from tests.test_griplink_cli import world as cli_world  # noqa: F401
from tests.test_griplink_step1 import git, world  # noqa: F401

NEXT = "full mode works on the root branch main only for now; next: git switch main"


def _refusal(ws: Path) -> str:
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.commit(ws, "change", stage_all=True)
    assert exc.value.code == 3
    return str(exc.value)


def test_a_side_branch_names_the_next_step(world):
    git(world["ws"], "checkout", "-q", "-b", "side")
    assert _refusal(world["ws"]) == f"side: {NEXT} (or git branch -m side main), then gr2 commit again"


def test_a_branch_name_with_a_shell_character_is_quoted_in_the_command(world):
    subprocess.run(["git", "check-ref-format", "--branch", "a$b"], check=True, capture_output=True)
    git(world["ws"], "checkout", "-q", "-b", "a$b")
    assert _refusal(world["ws"]) == f"a$b: {NEXT} (or git branch -m 'a$b' main), then gr2 commit again"


def test_a_detached_head_is_told_to_switch_only(world):
    git(world["ws"], "checkout", "-q", "--detach")
    assert _refusal(world["ws"]) == f"a detached HEAD: {NEXT}, then gr2 commit again"


def test_status_names_the_upstream_when_one_is_set(world):
    ws = world["ws"]
    git(ws, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(ws, "config", "branch.main.remote", "origin")
    git(ws, "config", "branch.main.merge", "refs/heads/main")
    lines = griplink.status_lines(griplink.status(ws))
    assert lines == ["status: on main, upstream origin/main; nothing to commit"]


def test_commit_at_the_root_says_each_members_change_stays_staged_there(cli_world, monkeypatch):
    root = cli_world["root"]
    _edit_and_stage(root)
    r = gr2(root, monkeypatch, "commit", "-m", "change a")
    assert r.exit_code == 0, r.stderr
    sha = git(root, "rev-parse", "HEAD")
    assert r.stdout.strip() == (f"committed {sha} at the root (api); each member's change stays staged in "
                                "its own git, not committed there")


def test_a_root_with_no_spec_is_told_both_inits(tmp_path, monkeypatch):
    root = tmp_path / "empty"
    root.mkdir()
    git(root, "init", "-q")
    r = gr2(root, monkeypatch, "store", "status")
    assert r.exit_code == 5, (r.stdout, r.stderr)
    assert r.stderr.strip() == (
        f"no grip.toml at {root} and no committed spec at HEAD; this root is not a store these verbs can measure "
        "-- run gr2 store init here (for full mode: gr2 store init --mode full <new dir> --clone NAME=URL for each "
        "member)")


def test_store_init_mode_help_says_what_full_makes():
    store = typer.main.get_command(app).commands["store"]
    (mode,) = [p for p in store.commands["init"].params if p.name == "mode"]
    assert "Full makes the root a git repository on branch main with no grip.toml." in mode.help
    assert "into grip.toml" not in mode.help
    assert (
        "Inside it, each member is checked out under <member>.griplink, holding the member's own commit as the "
        "gitlink, and <member> links to that checkout. A pin need not be on a remote."
    ) in mode.help
    assert "beside it" not in mode.help
    assert ".grip/fields" not in mode.help
    assert "is refused." in mode.help
