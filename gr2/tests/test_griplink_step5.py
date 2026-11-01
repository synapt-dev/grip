"""Full mode on the griplink root, step 5: promote to the member remotes (spec 13.8, Layne's five answers).

Push sends each member's promoted pin to the member's own remote before the root, so a plain
`git clone --recurse-submodules` of the root checks every member out at its pin.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import griplink
from gr2.python_cli.griplink import GriplinkRefusal

from tests.test_griplink_step1 import git, world  # noqa: F401  (api and web, pushed once)
from tests.test_griplink_step3 import _write


def _pin(root: Path, m: str, rev: str = "HEAD") -> str:
    return git(root, "rev-parse", f"{rev}:{m}.griplink/{griplink.PIN}")


def _remote_main(w: dict, m: str) -> str:
    return git(w["tmp"], "--git-dir", str(w["tmp"] / f"remote-{m}.git"), "rev-parse", "refs/heads/main")


def _branch_line(ws: Path, m: str, value: str) -> None:
    git(ws, "config", "-f", str(ws / ".gitmodules"), f"submodule.{m}.branch", value)


def test_q3_branch_is_main_when_gitmodules_names_none(world):
    assert griplink.member_branch(world["ws"], "api") == "main"


def test_q3_branch_is_the_gitmodules_line(world):
    _branch_line(world["ws"], "api", "dev")
    assert griplink.member_branch(world["ws"], "api") == "dev"
    assert griplink.member_branch(world["ws"], "web") == "main"


def test_q3_branch_dot_is_the_roots_own_branch_as_git_reads_it(world):
    _branch_line(world["ws"], "api", ".")
    assert griplink.member_branch(world["ws"], "api") == "main"


@pytest.mark.parametrize("bad", ["a..b", "-x", "has space", "x.lock", "a/"])
def test_q3_a_branch_line_git_would_not_take_refuses_by_member(world, bad):
    _branch_line(world["ws"], "api", bad)
    with pytest.raises(GriplinkRefusal) as e:
        griplink.member_branch(world["ws"], "api")
    assert e.value.args[0].startswith(f"api: its .gitmodules branch line {bad!r} is not a branch name; nothing was")


def test_p1_one_push_lands_both_promoted_members_and_a_plain_recursive_clone_checks_them_out(world):
    ws = world["ws"]
    _write(ws, "api", "src/f1.py", "api change 1\n")
    _write(ws, "web", "src/f3.py", "web change 3\n")
    griplink.commit(ws, "api and web", stage_all=True)
    griplink.promote(ws, "api")
    griplink.promote(ws, "web")
    griplink.push(ws, push_members=True)
    assert _remote_main(world, "api") == _pin(ws, "api")
    assert _remote_main(world, "web") == _pin(ws, "web")
    s = world["tmp"] / "stranger"
    proc = subprocess.run(["git", "-c", "protocol.file.allow=always", "clone", "-q", "--recurse-submodules",
                           f"file://{world['remote']}", str(s)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    for m in ("api", "web"):
        assert git(griplink.member_dir(s, m), "rev-parse", "HEAD") == _pin(ws, m)
    assert (griplink.member_dir(s, "api") / "src" / "f1.py").read_text() == "api change 1\n"
    assert (griplink.member_dir(s, "web") / "src" / "f3.py").read_text() == "web change 3\n"


def _other_pusher(w: dict, m: str, path: str, text: str) -> str:
    """Another desk commits `path` on the member remote's main; returns the new tip."""
    o = w["tmp"] / f"other-{m}"
    if not o.exists():
        subprocess.run(["git", "clone", "-q", str(w["tmp"] / f"remote-{m}.git"), str(o)], check=True)
    git(o, "pull", "-q", "--ff-only")
    (o / path).parent.mkdir(parents=True, exist_ok=True)
    (o / path).write_text(text)
    git(o, "add", "-A")
    git(o, "commit", "-q", "-m", f"other: {path}")
    git(o, "push", "-q", "origin", "HEAD:main")
    return git(o, "rev-parse", "HEAD")


def test_p2b_promote_lands_on_the_other_pushers_commit_with_both_changes(world):
    ws = world["ws"]
    other = _other_pusher(world, "api", "src/f3.py", "other desk's f3\n")
    _write(ws, "api", "src/f1.py", "api change 1\n")
    griplink.commit(ws, "api f1", stage_all=True)
    git(griplink.member_dir(ws, "api"), "fetch", "-q", "origin")
    got = griplink.promote(ws, "api")
    mrepo = griplink.member_dir(ws, "api")
    assert git(mrepo, "rev-parse", f"{got['commit']}^@") == other
    assert git(mrepo, "show", f"{got['commit']}:src/f1.py") == "api change 1"
    assert git(mrepo, "show", f"{got['commit']}:src/f3.py") == "other desk's f3"
    assert _pin(ws, "api") == got["commit"]


def test_p3_a_same_line_conflict_refuses_by_member_and_path_and_moves_nothing(world):
    ws = world["ws"]
    _other_pusher(world, "api", "src/f1.py", "other desk's f1\n")
    _write(ws, "api", "src/f1.py", "api change 1\n")
    griplink.commit(ws, "api f1", stage_all=True)
    mrepo = griplink.member_dir(ws, "api")
    git(mrepo, "fetch", "-q", "origin")
    pin0, head0, shadow0 = _pin(ws, "api"), git(mrepo, "rev-parse", "HEAD"), git(ws, "rev-parse", "HEAD")
    chain0 = git(ws, "rev-parse", griplink.member_ref("api"))
    with pytest.raises(GriplinkRefusal) as e:
        griplink.promote(ws, "api")
    assert e.value.args[0].startswith("api: the change and main at ")
    assert "both changed src/f1.py; nothing was promoted" in e.value.args[0]
    assert (_pin(ws, "api"), git(mrepo, "rev-parse", "HEAD"), git(ws, "rev-parse", "HEAD")) == (pin0, head0, shadow0)
    assert git(ws, "rev-parse", griplink.member_ref("api")) == chain0


def test_p2b_unfetched_tracking_ref_promotes_on_the_pin_with_no_network(world):
    ws = world["ws"]
    _other_pusher(world, "api", "src/f3.py", "other desk's f3\n")  # on the remote, never fetched here
    pin0 = _pin(ws, "api")
    _write(ws, "api", "src/f1.py", "api change 1\n")
    griplink.commit(ws, "api f1", stage_all=True)
    got = griplink.promote(ws, "api")
    assert git(griplink.member_dir(ws, "api"), "rev-parse", f"{got['commit']}^@") == pin0
