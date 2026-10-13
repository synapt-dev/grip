"""Lease 2 at the cut (gr2/python_cli/shadow.py): every member the previous SHADOW pinned must fast-forward to
its new pin, checked by a LOCAL ancestry walk; a commit not held is fetched, and still missing is refused by name.

Rows:
L1 a member that moved forward passes;
L2 a rewind is refused by member and both shas, with nothing written; allow_rewind takes it and records it;
L3 a rewrite (diverged) is refused as a rewrite;
L4 a previous pin that no one holds is refused: "cannot verify <member>: <sha> not held";
L5 offline is the same refusal, never a pass; with the remote back the same cut fetches and the walk runs;
L6 a new member and a removed member are allowed and named.
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
SHADOW = f"refs/{ORG}/__teams__/t/SHADOW"
EVENTS = fd.parse_proto("""syntax = "proto3";
package example.events.v1;

message Event {
  string by = 1;
  string seq = 2;
}
""")
ENV = dict(os.environ, GIT_AUTHOR_NAME="lease-test", GIT_AUTHOR_EMAIL="lease-test@example.invalid",
           GIT_COMMITTER_NAME="lease-test", GIT_COMMITTER_EMAIL="lease-test@example.invalid")


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


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True,
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


def push(repo: Path, *refs: str, allow_rewind: bool = False) -> None:
    importlib.import_module("gr2.python_cli.leased_push").push(repo, list(refs), org=ORG, allow_rewind=allow_rewind)


@pytest.fixture
def world(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    w = clone_of(origin, tmp_path / "writers")
    a1, b1 = append(w, "a", "1"), append(w, "b", "1")
    push(w, stook("a"), stook("b"))
    me = clone_of(origin, tmp_path / "me")
    git(me, "update-ref", TEAM, append(me, "t", "1"))
    return origin, w, me, a1


def test_l1_a_member_that_moved_forward_passes(sh, world):
    _, w, me, _ = world
    sh.cut(me, TEAM, [stook("a"), stook("b")], org=ORG)
    a2 = append(w, "a", "2")
    push(w, stook("a"))
    git(me, "fetch", "-q", "origin")
    c = sh.cut(me, TEAM, [stook("a"), stook("b")], org=ORG)
    assert git(me, "rev-parse", f"{c.commit}:pins/__agents__/a") == a2 and c.rewound == []


def test_l2_a_rewind_is_refused_by_member_and_both_shas_and_allow_rewind_records_it(sh, world):
    _, w, me, a1 = world
    a2 = append(w, "a", "2")
    push(w, stook("a"))
    git(me, "fetch", "-q", "origin")
    first = sh.cut(me, TEAM, [stook("a")], org=ORG).commit
    git(w, "update-ref", stook("a"), a1)
    push(w, stook("a"), allow_rewind=True)
    git(me, "fetch", "-q", "origin")
    with pytest.raises(sh.MemberRewound) as e:
        sh.cut(me, TEAM, [stook("a")], org=ORG)
    assert (e.value.member, e.value.kind, e.value.old, e.value.new) == ("__agents__/a", "rewind", a2, a1)
    assert a2 in str(e.value) and a1 in str(e.value)
    assert git(me, "rev-parse", SHADOW) == first                       # nothing written
    c = sh.cut(me, TEAM, [stook("a")], org=ORG, allow_rewind=True)
    assert c.rewound == ["__agents__/a"]
    assert f"Allow-Rewind: __agents__/a rewind {a2} -> {a1}" in git(me, "log", "-1", "--format=%B", c.commit)


def test_l3_a_rewrite_is_refused_as_a_rewrite(sh, world):
    _, w, me, a1 = world
    a2 = append(w, "a", "2")
    push(w, stook("a"))
    git(me, "fetch", "-q", "origin")
    sh.cut(me, TEAM, [stook("a")], org=ORG)
    git(w, "update-ref", stook("a"), a1)
    side = append(w, "a", "side")                                     # diverges from a2 at a1
    push(w, stook("a"), allow_rewind=True)
    git(me, "fetch", "-q", "origin")
    with pytest.raises(sh.MemberRewound) as e:
        sh.cut(me, TEAM, [stook("a")], org=ORG)
    assert (e.value.kind, e.value.old, e.value.new) == ("rewrite", a2, side)


def previous_shadow_pinning(me: Path, sha: str) -> None:
    """A previous SHADOW whose pin for member a is ``sha`` (a gitlink needs no object), with no parents."""
    inner = subprocess.run(["git", "-C", str(me), "mktree"], input=f"160000 commit {sha}\ta\n".encode(),
                           capture_output=True, check=True, env=ENV).stdout.decode().strip()
    mid = subprocess.run(["git", "-C", str(me), "mktree"], input=f"040000 tree {inner}\t__agents__\n".encode(),
                         capture_output=True, check=True, env=ENV).stdout.decode().strip()
    top = subprocess.run(["git", "-C", str(me), "mktree"], input=f"040000 tree {mid}\tpins\n".encode(),
                         capture_output=True, check=True, env=ENV).stdout.decode().strip()
    git(me, "update-ref", SHADOW, git(me, "commit-tree", top, "-m", "previous"))


def test_l4_a_previous_pin_no_one_holds_is_refused_by_name(sh, world):
    _, _, me, _ = world
    ghost = "1" * 40
    previous_shadow_pinning(me, ghost)
    before = git(me, "rev-parse", SHADOW)
    with pytest.raises(sh.CannotVerify) as e:
        sh.cut(me, TEAM, [stook("a")], org=ORG)
    assert f"cannot verify __agents__/a: {ghost} not held" in str(e.value)
    assert git(me, "rev-parse", SHADOW) == before


def test_l5_offline_is_the_same_refusal_and_with_the_remote_back_the_walk_runs(sh, world):
    _, w, me, a1 = world
    x = append(w, "z", "1")                          # origin holds it (z/STOOK); `me` has not fetched it
    push(w, stook("z"))
    previous_shadow_pinning(me, x)
    url = git(me, "remote", "get-url", "origin")
    git(me, "remote", "set-url", "origin", str(Path(url).parent / "nowhere.git"))
    with pytest.raises(sh.CannotVerify) as e:
        sh.cut(me, TEAM, [stook("a")], org=ORG)
    assert f"cannot verify __agents__/a: {x} not held" in str(e.value)
    git(me, "remote", "set-url", "origin", url)
    with pytest.raises(sh.MemberRewound) as e:      # the remote is back: x is fetched and the walk runs
        sh.cut(me, TEAM, [stook("a")], org=ORG)
    assert (e.value.kind, e.value.old, e.value.new) == ("rewrite", x, a1)


def test_l6_a_new_member_and_a_removed_member_are_allowed_and_named(sh, world):
    _, w, me, _ = world
    sh.cut(me, TEAM, [stook("a"), stook("b")], org=ORG)
    append(w, "c", "1")
    push(w, stook("c"))
    git(me, "fetch", "-q", "origin")
    c = sh.cut(me, TEAM, [stook("a"), stook("c")], org=ORG)
    assert (c.added, c.removed) == (["__agents__/c"], ["__agents__/b"])
