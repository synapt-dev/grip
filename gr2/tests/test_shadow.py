"""SHADOW over members in the same repository (gr2/python_cli/shadow.py).

Rows:
H1 shape: the tree is the STOOK's tree plus pins/<member path> gitlinks; the parents are the STOOK's tip, then
   each member's resolved commit; the SHADOW ref is set;
H2 resolution reads the tracking tree: a member's unpushed local write is not pinned;
H3 a member's pushed SHADOW is pinned in place of its STOOK (the recursion);
H4 a SHADOW pushed alone to a fresh remote carries every member it pins;
H5 a dangling member is refused by name, with nothing written;
H6 a second cut takes the first as its first parent;
H7 a member outside the namespace, or named twice, is refused.
"""
from __future__ import annotations

import importlib
import os
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import review_field_tree as fd

ORG = "example"
TEAM = f"refs/{ORG}/__teams__/t/STOOK"
EVENTS = fd.parse_proto("""syntax = "proto3";
package example.events.v1;

message Event {
  string by = 1;
  string seq = 2;
}
""")
ENV = dict(os.environ, GIT_AUTHOR_NAME="shadow-test", GIT_AUTHOR_EMAIL="shadow-test@example.invalid",
           GIT_COMMITTER_NAME="shadow-test", GIT_COMMITTER_EMAIL="shadow-test@example.invalid")


def stook(agent: str) -> str:
    return f"refs/{ORG}/__agents__/{agent}/STOOK"


@pytest.fixture
def sh():
    return importlib.import_module("gr2.python_cli.shadow")


@pytest.fixture(autouse=True)
def _identity(monkeypatch):
    for k, v in ENV.items():
        if k.startswith("GIT_"):
            monkeypatch.setenv(k, v)


def git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=check,
                          env=ENV).stdout.decode().strip()


def append(repo: Path, agent: str, seq: str) -> str:
    ar = importlib.import_module("gr2.python_cli.agent_record")
    return ar.append(repo, agent, {"by": agent, "seq": seq}, org=ORG, message="Event", schema=EVENTS).commit


def clone_of(origin: Path, path: Path) -> Path:
    tr = importlib.import_module("gr2.python_cli.tracking")
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    git(path, "remote", "add", "origin", str(origin))
    tr.ensure_fetch_lines(path, ORG)
    git(path, "fetch", "-q", "origin")
    return path


def push(repo: Path, *refs: str) -> None:
    importlib.import_module("gr2.python_cli.leased_push").push(repo, list(refs), org=ORG)


@pytest.fixture
def world(tmp_path):
    """origin holds a/STOOK and b/STOOK (pushed by their writers); `me` owns the team record and has fetched."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    w = clone_of(origin, tmp_path / "writers")
    a1, b1 = append(w, "a", "1"), append(w, "b", "1")
    push(w, stook("a"), stook("b"))
    me = clone_of(origin, tmp_path / "me")
    git(me, "update-ref", TEAM, append(me, "t", "1"))      # the team's own record, written by its owner
    return origin, w, me, {"a": a1, "b": b1}


def parents(repo: Path, sha: str) -> list[str]:
    return git(repo, "rev-list", "--parents", "-n", "1", sha).split()[1:]


def test_h1_tree_is_the_stook_plus_pins_and_parents_are_the_stook_then_members(sh, world):
    _, _, me, tips = world
    sha = sh.cut(me, TEAM, [stook("a"), stook("b")], org=ORG)
    assert git(me, "rev-parse", f"refs/{ORG}/__teams__/t/SHADOW") == sha
    team_tip = git(me, "rev-parse", TEAM)
    assert parents(me, sha) == [team_tip, tips["a"], tips["b"]]
    stook_entries = git(me, "ls-tree", team_tip).splitlines()
    shadow_entries = git(me, "ls-tree", sha).splitlines()
    assert [e for e in shadow_entries if not e.endswith("\tpins")] == stook_entries
    assert git(me, "ls-tree", "-r", f"{sha}:pins").splitlines() == [
        f"160000 commit {tips['a']}\t__agents__/a", f"160000 commit {tips['b']}\t__agents__/b"]


def test_h2_an_unpushed_local_write_is_not_pinned(sh, world):
    _, _, me, tips = world
    git(me, "update-ref", stook("a"), tips["a"])
    append(me, "a", "local-only")                           # ahead of what the remote has
    sha = sh.cut(me, TEAM, [stook("a")], org=ORG)
    assert git(me, "rev-parse", f"{sha}:pins/__agents__/a") == tips["a"]


def test_h3_a_members_pushed_shadow_is_pinned_in_place_of_its_stook(sh, world):
    _, w, me, tips = world
    # a member's own shadow: a commit over its STOOK tip, pushed beside it
    tree = git(w, "rev-parse", f"{tips['a']}^{{tree}}")
    a_shadow = git(w, "commit-tree", tree, "-p", tips["a"], "-m", "a's shadow")
    git(w, "update-ref", f"refs/{ORG}/__agents__/a/SHADOW", a_shadow)
    push(w, f"refs/{ORG}/__agents__/a/SHADOW")
    git(me, "fetch", "-q", "origin")
    sha = sh.cut(me, TEAM, [stook("a"), stook("b")], org=ORG)
    assert git(me, "rev-parse", f"{sha}:pins/__agents__/a") == a_shadow
    assert git(me, "rev-parse", f"{sha}:pins/__agents__/b") == tips["b"]


def test_h4_a_shadow_pushed_alone_carries_every_member(sh, world, tmp_path):
    _, _, me, tips = world
    sha = sh.cut(me, TEAM, [stook("a"), stook("b")], org=ORG)
    fresh = tmp_path / "fresh.git"
    subprocess.run(["git", "init", "-q", "--bare", str(fresh)], check=True)
    git(me, "push", "-q", str(fresh), f"refs/{ORG}/__teams__/t/SHADOW:refs/{ORG}/__teams__/t/SHADOW")
    for t in tips.values():
        assert subprocess.run(["git", "-C", str(fresh), "cat-file", "-e", f"{t}^{{commit}}"]).returncode == 0
    assert git(fresh, "for-each-ref", "--format=%(refname)") == f"refs/{ORG}/__teams__/t/SHADOW"


def test_h5_a_dangling_member_is_refused_with_nothing_written(sh, world):
    _, _, me, _ = world
    before = git(me, "for-each-ref")
    objects = git(me, "count-objects", "-v")
    with pytest.raises(sh.Dangling) as e:
        sh.cut(me, TEAM, [stook("a"), stook("ghost")], org=ORG)
    assert stook("ghost") in str(e.value)
    assert git(me, "for-each-ref") == before and git(me, "count-objects", "-v") == objects


def test_h6_a_second_cut_takes_the_first_as_its_first_parent(sh, world):
    _, _, me, _ = world
    first = sh.cut(me, TEAM, [stook("a")], org=ORG)
    git(me, "update-ref", TEAM, append(me, "t", "2"))
    second = sh.cut(me, TEAM, [stook("a")], org=ORG)
    assert parents(me, second)[0] == first


@pytest.mark.parametrize("members", [["refs/heads/main"], [f"refs/other/__agents__/a/STOOK"],
                                     [f"refs/{ORG}/__agents__/a/SHADOW"], [stook("a"), stook("a")]])
def test_h7_a_member_outside_the_namespace_or_named_twice_is_refused(sh, world, members):
    _, _, me, _ = world
    with pytest.raises(ValueError):
        sh.cut(me, TEAM, members, org=ORG)
    assert git(me, "for-each-ref", f"refs/{ORG}/__teams__/t/SHADOW") == ""
