"""A leased push of records under a caller's namespace (gr2/python_cli/leased_push.py).

Rows:
P1 an atomic push of two records moves both on the remote, and the tracking tree follows;
P2 a lease turns the push away when the remote moved after the check, and with atomic no ref moves;
P3 stale tracking is refused before anything is sent, naming the ref; no ref moves, the fresh one included;
P4 a record new to the remote is pushed on an empty lease, and its tracking ref is made;
P5 a rewind is refused by name unless allow_rewind, which takes it; P5b an amended (diverged) record is a rewrite;
P6 a ref with no local value is refused, and a ref outside the namespace has no lease.
"""
from __future__ import annotations

import importlib
import os
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import review_field_tree as fd

ORG = "example"
EVENTS = fd.parse_proto("""syntax = "proto3";
package example.events.v1;

message Event {
  string by = 1;
  string seq = 2;
}
""")
ENV = dict(os.environ, GIT_AUTHOR_NAME="push-test", GIT_AUTHOR_EMAIL="push-test@example.invalid",
           GIT_COMMITTER_NAME="push-test", GIT_COMMITTER_EMAIL="push-test@example.invalid")


def stook(agent: str) -> str:
    return f"refs/{ORG}/__agents__/{agent}/STOOK"


@pytest.fixture
def lp():
    return importlib.import_module("gr2.python_cli.leased_push")


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


def remote(origin: Path, ref: str) -> str | None:
    out = git(origin, "for-each-ref", "--format=%(objectname)", ref)
    return out or None


@pytest.fixture
def world(tmp_path, lp):
    """origin with a/STOOK and b/STOOK at one event each; `me` and `other` are fetched clones."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    seed = clone_of(origin, tmp_path / "seed")
    append(seed, "a", "1"), append(seed, "b", "1")
    lp.push(seed, [stook("a"), stook("b")], org=ORG)
    me = clone_of(origin, tmp_path / "me")
    other = clone_of(origin, tmp_path / "other")
    for c in (me, other):   # each writer holds the records it fetched (its own sheaf in a real clone)
        for a in ("a", "b"):
            git(c, "update-ref", stook(a), git(c, "rev-parse", f"refs/{ORG}/__remotes__/origin/{ORG}/__agents__/{a}/STOOK"))
    return origin, me, other


def test_p1_an_atomic_push_moves_both_and_the_tracking_tree_follows(lp, world):
    origin, me, _ = world
    a2, b2 = append(me, "a", "2"), append(me, "b", "2")
    assert lp.push(me, [stook("a"), stook("b")], org=ORG) == {stook("a"): a2, stook("b"): b2}
    assert (remote(origin, stook("a")), remote(origin, stook("b"))) == (a2, b2)
    tr = importlib.import_module("gr2.python_cli.tracking")
    assert tr.tracked(me, stook("a"), org=ORG) == a2 and tr.tracked(me, stook("b"), org=ORG) == b2


def test_p2_a_lease_turns_the_push_away_and_atomic_moves_nothing(lp, world, monkeypatch):
    origin, me, other = world
    a_before, b_before = remote(origin, stook("a")), remote(origin, stook("b"))
    moved = append(other, "a", "x")
    lp.push(other, [stook("a")], org=ORG)                 # the remote moves after `me` fetched
    append(me, "a", "2"), append(me, "b", "2")
    # the check is made to see the old values, as if the other writer landed between the check and the push
    monkeypatch.setattr(lp, "_remote_values", lambda repo, rem, refs: {stook("a"): a_before, stook("b"): b_before})
    with pytest.raises(lp.PushRejected):
        lp.push(me, [stook("a"), stook("b")], org=ORG)
    assert (remote(origin, stook("a")), remote(origin, stook("b"))) == (moved, b_before)


def test_p3_stale_tracking_is_refused_before_anything_is_sent(lp, world):
    origin, me, other = world
    b_before = remote(origin, stook("b"))
    moved = append(other, "a", "x")
    lp.push(other, [stook("a")], org=ORG)
    append(me, "a", "2"), append(me, "b", "2")
    with pytest.raises(lp.PushRefused) as e:
        lp.push(me, [stook("b"), stook("a")], org=ORG)
    assert e.value.ref == stook("a") and "stale tracking" in e.value.why
    assert (remote(origin, stook("a")), remote(origin, stook("b"))) == (moved, b_before)


def test_p4_a_new_record_is_pushed_on_an_empty_lease(lp, world):
    origin, me, _ = world
    c1 = append(me, "c", "1")
    assert lp.push(me, [stook("c")], org=ORG) == {stook("c"): c1}
    assert remote(origin, stook("c")) == c1
    tr = importlib.import_module("gr2.python_cli.tracking")
    assert tr.tracked(me, stook("c"), org=ORG) == c1


def test_p5_a_rewind_is_refused_unless_allowed(lp, world):
    origin, me, _ = world
    a1 = remote(origin, stook("a"))
    a2 = append(me, "a", "2")
    lp.push(me, [stook("a")], org=ORG)
    git(me, "update-ref", stook("a"), a1)                 # back to the parent: a rewind of what the remote has
    with pytest.raises(lp.PushRefused) as e:
        lp.push(me, [stook("a")], org=ORG)
    assert e.value.why.startswith("rewind:") and remote(origin, stook("a")) == a2
    lp.push(me, [stook("a")], org=ORG, allow_rewind=True)
    assert remote(origin, stook("a")) == a1


def test_p5b_an_amended_record_is_refused_as_a_rewrite(lp, world):
    origin, me, _ = world
    a1 = remote(origin, stook("a"))
    a2 = append(me, "a", "2")
    lp.push(me, [stook("a")], org=ORG)
    git(me, "update-ref", stook("a"), a1)
    append(me, "a", "amended")                            # a sibling of a2: neither descends from the other
    with pytest.raises(lp.PushRefused) as e:
        lp.push(me, [stook("a")], org=ORG)
    assert e.value.why.startswith("rewrite:") and remote(origin, stook("a")) == a2


def test_p6_no_local_value_and_outside_the_namespace_are_refused(lp, world):
    origin, me, _ = world
    with pytest.raises(lp.PushRefused) as e:
        lp.push(me, [stook("nobody")], org=ORG)
    assert "no local value" in e.value.why and remote(origin, stook("nobody")) is None
    git(me, "update-ref", "refs/heads/main", git(me, "rev-parse", stook("a")))
    with pytest.raises(ValueError):
        lp.push(me, ["refs/heads/main"], org=ORG)
