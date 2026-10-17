"""ARBON and the close (gr2/python_cli/arbon.py).

Rows:
A1 a close puts the STOOK, the SHADOW and the arbon on the remote; the arbon is a tag object on the SHADOW commit;
A2 an arbon never moves: a second close of the same rheon is refused with nothing written, here or there;
A3 a close whose push is turned away moves nothing on the remote and removes its local arbon;
A4 recurse=check refuses a member that moved backwards on the remote, by name and both shas, pushing nothing;
A5 a fresh clone of the arbon alone reads every member it pins;
A6 a rheon that is not one lowercase ref component is refused before anything is written;
A7 recurse=on-demand refuses a member this clone holds no value for, by name;
A8 a remote without atomic push support refuses the close (never degrades to per-ref) and moves nothing.
A9 allow_rewind reaches the members alone: a locally rewritten SHADOW is refused under it and origin is unchanged.
A10 under allow_rewind an on-demand member rewound here lands, with the SHADOW pinning it and naming it in an
    Allow-Rewind line; without the flag it is refused and origin is unchanged.
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
ENV = dict(os.environ, GIT_AUTHOR_NAME="arbon-test", GIT_AUTHOR_EMAIL="arbon-test@example.invalid",
           GIT_COMMITTER_NAME="arbon-test", GIT_COMMITTER_EMAIL="arbon-test@example.invalid")


def stook(agent: str) -> str:
    return f"refs/{ORG}/__agents__/{agent}/STOOK"


@pytest.fixture
def ab():
    return importlib.import_module("gr2.python_cli.arbon")


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


def on(origin: Path, ref: str) -> str | None:
    return git(origin, "for-each-ref", "--format=%(objectname)", ref) or None


@pytest.fixture
def world(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    w = clone_of(origin, tmp_path / "writers")
    a1, b1 = append(w, "a", "1"), append(w, "b", "1")
    push(w, stook("a"), stook("b"))
    me = clone_of(origin, tmp_path / "me")
    git(me, "update-ref", TEAM, append(me, "t", "1"))
    return origin, w, me, {"a": a1, "b": b1}


def test_a1_a_close_puts_stook_shadow_and_arbon_on_the_remote(ab, world):
    origin, _, me, _ = world
    c = ab.close(me, TEAM, "9", [stook("a"), stook("b")], org=ORG)
    arbon = f"refs/{ORG}/__teams__/t/__arbons__/9"
    assert on(origin, TEAM) == git(me, "rev-parse", TEAM)
    assert on(origin, SHADOW) == c.shadow == git(me, "rev-parse", SHADOW)
    assert on(origin, arbon) == c.arbon != c.shadow
    assert git(origin, "cat-file", "-t", c.arbon) == "tag"
    assert git(origin, "rev-parse", f"{c.arbon}^{{commit}}") == c.shadow
    assert "tag 9" in git(origin, "cat-file", "-p", c.arbon)


def test_a2_an_arbon_never_moves(ab, world):
    origin, _, me, _ = world
    ab.close(me, TEAM, "9", [stook("a")], org=ORG)
    git(me, "update-ref", TEAM, append(me, "t", "2"))
    here, there = git(me, "for-each-ref"), git(origin, "for-each-ref")
    with pytest.raises(ab.ArbonExists):
        ab.close(me, TEAM, "9", [stook("a")], org=ORG)
    assert git(me, "for-each-ref") == here and git(origin, "for-each-ref") == there


def test_a3_a_turned_away_close_moves_nothing_and_removes_its_local_arbon(ab, world, tmp_path):
    origin, w, me, _ = world
    other = clone_of(origin, tmp_path / "other")
    git(other, "update-ref", TEAM, append(other, "t", "x"))
    push(other, TEAM)                                       # someone else wrote the team record after me fetched
    before = git(origin, "for-each-ref")
    lp = importlib.import_module("gr2.python_cli.leased_push")
    with pytest.raises(lp.PushRefused) as e:
        ab.close(me, TEAM, "9", [stook("a")], org=ORG)
    assert e.value.ref == TEAM and "stale tracking" in e.value.why
    assert git(origin, "for-each-ref") == before
    assert git(me, "for-each-ref", f"refs/{ORG}/__teams__/t/__arbons__/") == ""


def test_a4_check_refuses_a_member_that_moved_backwards_on_the_remote(ab, world):
    origin, w, me, tips = world
    a2 = append(w, "a", "2")
    push(w, stook("a"))
    git(me, "fetch", "-q", "origin")
    git(w, "update-ref", stook("a"), tips["a"])
    push(w, stook("a"), allow_rewind=True)                  # rewound on the remote after me fetched
    before = git(origin, "for-each-ref")
    with pytest.raises(ab.MemberNotCurrent) as e:
        ab.close(me, TEAM, "9", [stook("a")], org=ORG)
    assert (e.value.member, e.value.pin, e.value.now) == ("__agents__/a", a2, tips["a"])
    assert git(origin, "for-each-ref") == before


def test_a5_a_fresh_clone_of_the_arbon_alone_reads_every_member(ab, world, tmp_path):
    origin, _, me, tips = world
    ab.close(me, TEAM, "9", [stook("a"), stook("b")], org=ORG)
    fresh = tmp_path / "fresh"
    subprocess.run(["git", "init", "-q", str(fresh)], check=True)
    arbon = f"refs/{ORG}/__teams__/t/__arbons__/9"
    git(fresh, "fetch", "-q", str(origin), f"{arbon}:{arbon}")
    for t in tips.values():
        assert subprocess.run(["git", "-C", str(fresh), "cat-file", "-e", f"{t}^{{commit}}"]).returncode == 0


@pytest.mark.parametrize("rheon", ["", "Nine", "9/1", "9.", "9..1", "9.lock"])
def test_a6_a_bad_rheon_is_refused_before_anything_is_written(ab, world, rheon):
    origin, _, me, _ = world
    here, there = git(me, "for-each-ref"), git(origin, "for-each-ref")
    with pytest.raises(ValueError):
        ab.close(me, TEAM, rheon, [stook("a")], org=ORG)
    assert git(me, "for-each-ref") == here and git(origin, "for-each-ref") == there


def test_a7_on_demand_refuses_a_member_this_clone_holds_no_value_for(ab, world):
    origin, _, me, _ = world
    before = git(origin, "for-each-ref")
    lp = importlib.import_module("gr2.python_cli.leased_push")
    with pytest.raises(lp.PushRefused) as e:
        ab.close(me, TEAM, "9", [stook("a")], org=ORG, recurse="on-demand")
    assert e.value.ref == stook("a") and "on-demand" in e.value.why
    assert git(origin, "for-each-ref") == before


def test_a8_a_remote_without_atomic_push_refuses_the_close_and_moves_nothing(ab, world):
    """A8 (a section 4 probe): a remote that does not advertise atomic push (receive.advertiseAtomic
    false) must REFUSE the close, never degrade to a per-ref push that could land the STOOK without its arbon."""
    origin, _, me, _ = world
    git(origin, "config", "receive.advertiseAtomic", "false")
    lp = importlib.import_module("gr2.python_cli.leased_push")
    arbon = f"refs/{ORG}/__teams__/t/__arbons__/9"
    with pytest.raises(lp.PushRejected, match="atomic"):
        ab.close(me, TEAM, "9", [stook("a"), stook("b")], org=ORG)
    assert (on(origin, TEAM), on(origin, SHADOW), on(origin, arbon)) == (None, None, None)
    assert git(me, "for-each-ref", "--format=%(refname)", arbon) == ""
    git(origin, "config", "--unset", "receive.advertiseAtomic")
    c = ab.close(me, TEAM, "9", [stook("a"), stook("b")], org=ORG)     # the same close lands once atomic is back
    assert on(origin, arbon) == c.arbon


def test_a9_allow_rewind_never_lets_a_rewritten_shadow_through(ab, world):
    """A9: allow_rewind is the members' flag, given to the cut only. The close's own STOOK,
    SHADOW and arbon are always pushed without it, so a SHADOW rewritten locally is refused and origin is unchanged."""
    origin, _, me, _ = world
    c = ab.close(me, TEAM, "9", [stook("a"), stook("b")], org=ORG)
    forged = git(me, "commit-tree", git(me, "rev-parse", f"{c.shadow}^{{tree}}"), "-m", "amended",
                 "-p", git(me, "rev-parse", TEAM))           # its first parent is not origin's SHADOW tip
    git(me, "update-ref", SHADOW, forged)
    before = git(origin, "for-each-ref")
    lp = importlib.import_module("gr2.python_cli.leased_push")
    arbon = f"refs/{ORG}/__teams__/t/__arbons__/10"
    with pytest.raises(lp.PushRefused) as e:
        ab.close(me, TEAM, "10", [stook("a"), stook("b")], org=ORG, allow_rewind=True)
    assert e.value.ref == SHADOW and "does not descend" in e.value.why
    assert git(origin, "for-each-ref") == before
    assert on(origin, SHADOW) == c.shadow and on(origin, arbon) is None
    assert git(me, "for-each-ref", "--format=%(refname)", arbon) == ""


def test_a10_allow_rewind_lands_an_on_demand_member_rewound_here(ab, world):
    """A10: the members' flag reaches the on-demand member refs. A member rewound in this clone lands on origin
    under allow_rewind=True, with the SHADOW carrying its Allow-Rewind line; A9 still holds for the SHADOW itself."""
    origin, _, me, tips = world
    git(me, "update-ref", stook("a"), tips["a"])
    a2 = append(me, "a", "2")
    c9 = ab.close(me, TEAM, "9", [stook("a")], org=ORG, recurse="on-demand")
    assert on(origin, stook("a")) == a2 == git(me, "rev-parse", f"{c9.shadow}:pins/__agents__/a")   # pins what landed
    git(me, "update-ref", stook("a"), tips["a"])            # rewound here, one behind origin
    lp = importlib.import_module("gr2.python_cli.leased_push")
    before = git(origin, "for-each-ref")
    with pytest.raises(lp.PushRefused) as e:                # without the flag the rewind is refused
        ab.close(me, TEAM, "10", [stook("a")], org=ORG, recurse="on-demand", snapshot=False)
    assert e.value.ref == stook("a") and "rewind" in e.value.why
    assert git(origin, "for-each-ref") == before
    c = ab.close(me, TEAM, "10", [stook("a")], org=ORG, recurse="on-demand", allow_rewind=True)
    assert on(origin, stook("a")) == tips["a"]
    assert on(origin, SHADOW) == c.shadow and on(origin, f"refs/{ORG}/__teams__/t/__arbons__/10") == c.arbon
    assert f"Allow-Rewind: __agents__/a rewind {a2} -> {tips['a']}" in git(origin, "log", "-1", "--format=%B", c.shadow)
