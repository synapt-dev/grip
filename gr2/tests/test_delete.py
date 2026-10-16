"""Delete a record (gr2/python_cli/delete.py).

Rows:
D1 the STOOK and its SHADOW go together, on the remote and here, and their tracking refs go too;
D2 a record a SHADOW on the remote pins is refused, naming the pinning record; nothing is deleted;
D3 a SHADOW pushed after the last fetch is a stale view: refused, nothing deleted;
D4 the record moved on the remote after the last fetch: refused as stale, nothing deleted;
D5 a ref that is not a STOOK under the namespace is refused.
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
ENV = dict(os.environ, GIT_AUTHOR_NAME="delete-test", GIT_AUTHOR_EMAIL="delete-test@example.invalid",
           GIT_COMMITTER_NAME="delete-test", GIT_COMMITTER_EMAIL="delete-test@example.invalid")


def stook(agent: str) -> str:
    return f"refs/{ORG}/__agents__/{agent}/STOOK"


def shadow(agent: str) -> str:
    return f"refs/{ORG}/__agents__/{agent}/SHADOW"


@pytest.fixture
def dl():
    return importlib.import_module("gr2.python_cli.delete")


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


def push(repo: Path, *refs: str) -> None:
    importlib.import_module("gr2.python_cli.leased_push").push(repo, list(refs), org=ORG)


def on(origin: Path, prefix: str = f"refs/{ORG}/") -> str:
    return git(origin, "for-each-ref", "--format=%(refname)", prefix)


@pytest.fixture
def world(tmp_path):
    """`me` owns agent a: its STOOK and a SHADOW of its own, both pushed; b is another agent's record."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    me = clone_of(origin, tmp_path / "me")
    append(me, "a", "1"), append(me, "b", "1")
    sh = importlib.import_module("gr2.python_cli.shadow")
    push(me, stook("a"), stook("b"))
    git(me, "fetch", "-q", "origin")
    sh.cut(me, stook("a"), [], org=ORG)
    push(me, shadow("a"))
    return origin, me


def test_d1_the_stook_and_its_shadow_go_together(dl, world):
    origin, me = world
    assert dl.delete(me, stook("a"), org=ORG) == [stook("a"), shadow("a")]
    assert on(origin, f"refs/{ORG}/__agents__/a/") == ""
    assert git(me, "for-each-ref", f"refs/{ORG}/__agents__/a/") == ""
    assert git(me, "for-each-ref", f"refs/{ORG}/__remotes__/origin/{ORG}/__agents__/a/") == ""
    assert on(origin, f"refs/{ORG}/__agents__/b/") == stook("b")


def test_d2_a_record_a_shadow_pins_is_refused_by_the_pinning_records_name(dl, world, tmp_path):
    origin, me = world
    team = clone_of(origin, tmp_path / "team")
    sh = importlib.import_module("gr2.python_cli.shadow")
    git(team, "update-ref", TEAM, append(team, "t", "1"))
    push(team, TEAM)
    git(team, "fetch", "-q", "origin")
    sh.cut(team, TEAM, [stook("a")], org=ORG)
    push(team, f"refs/{ORG}/__teams__/t/SHADOW")
    git(me, "fetch", "-q", "origin")
    before = on(origin)
    with pytest.raises(dl.StillAMember) as e:
        dl.delete(me, stook("a"), org=ORG)
    assert e.value.pinned_by == ["__teams__/t"] and on(origin) == before


def test_d3_a_shadow_pushed_after_the_last_fetch_is_a_stale_view(dl, world, tmp_path):
    origin, me = world
    team = clone_of(origin, tmp_path / "team")
    sh = importlib.import_module("gr2.python_cli.shadow")
    git(team, "update-ref", TEAM, append(team, "t", "1"))
    push(team, TEAM)
    git(team, "fetch", "-q", "origin")
    sh.cut(team, TEAM, [stook("a")], org=ORG)
    push(team, f"refs/{ORG}/__teams__/t/SHADOW")          # me has not fetched since
    before = on(origin)
    lp = importlib.import_module("gr2.python_cli.leased_push")
    with pytest.raises(lp.PushRefused) as e:
        dl.delete(me, stook("a"), org=ORG)
    assert "stale tracking" in e.value.why and on(origin) == before


def test_d4_the_record_moved_after_the_last_fetch_is_refused_as_stale(dl, world, tmp_path):
    origin, me = world
    other = clone_of(origin, tmp_path / "other")
    git(other, "update-ref", stook("a"), git(other, "rev-parse", f"refs/{ORG}/__remotes__/origin/{ORG}/__agents__/a/STOOK"))
    append(other, "a", "2")
    push(other, stook("a"))
    before = on(origin)
    lp = importlib.import_module("gr2.python_cli.leased_push")
    with pytest.raises(lp.PushRefused) as e:
        dl.delete(me, stook("a"), org=ORG)
    assert e.value.ref == stook("a") and "stale tracking" in e.value.why and on(origin) == before


@pytest.mark.parametrize("ref", ["refs/heads/main", shadow("a"), "refs/other/__agents__/a/STOOK"])
def test_d5_a_ref_that_is_not_a_stook_under_the_namespace_is_refused(dl, world, ref):
    origin, me = world
    before = on(origin)
    with pytest.raises(ValueError):
        dl.delete(me, ref, org=ORG)
    assert on(origin) == before
