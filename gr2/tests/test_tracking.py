"""The tracking tree (gr2/python_cli/tracking.py): a clone's view of a remote's records under the caller's
namespace, filled by fetch and read with no network.

Rows:
T1 the fetch lines are written once (a second call writes nothing) and one fetch fills the tracking tree with
   the remote's shas, and nothing outside it;
T2 a read is local: it answers with the remote unreachable, and keeps the last fetched value until the next fetch;
T3 a remote that holds a remotes tree is not nested into this clone's;
T4 names: an org or remote that is not one lowercase ref component is refused before config is written, and a
   ref outside the namespace, or inside a remotes tree, has no tracking ref;
T5 the tracked value is the ref's own object id, unpeeled: an annotated tag stays its tag object (the lease value).
"""
from __future__ import annotations

import importlib
import os
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import review_field_tree as fd

ORG = "example"
STOOK_A = f"refs/{ORG}/__agents__/a/STOOK"
EVENTS = fd.parse_proto("""syntax = "proto3";
package example.events.v1;

message Event {
  string by = 1;
  string seq = 2;
}
""")
ENV = dict(os.environ, GIT_AUTHOR_NAME="tracking-test", GIT_AUTHOR_EMAIL="tracking-test@example.invalid",
           GIT_COMMITTER_NAME="tracking-test", GIT_COMMITTER_EMAIL="tracking-test@example.invalid")


@pytest.fixture
def tracking():
    return importlib.import_module("gr2.python_cli.tracking")


@pytest.fixture(autouse=True)
def _identity(monkeypatch):
    for k, v in ENV.items():
        if k.startswith("GIT_"):
            monkeypatch.setenv(k, v)


def git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=check,
                          env=ENV).stdout.decode().strip()


def append(origin: Path, agent: str, seq: str) -> str:
    ar = importlib.import_module("gr2.python_cli.agent_record")
    return ar.append(origin, agent, {"by": agent, "seq": seq}, org=ORG, message="Event", schema=EVENTS).commit


@pytest.fixture
def pair(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    git(clone, "remote", "add", "origin", str(origin))
    return origin, clone


def refs(repo: Path, pattern: str = f"refs/{ORG}/") -> dict[str, str]:
    out = git(repo, "for-each-ref", "--format=%(refname) %(objectname)", pattern)
    return dict(line.split() for line in out.splitlines() if line)


def test_t1_lines_written_once_and_one_fetch_fills_only_the_tracking_tree(tracking, pair):
    origin, clone = pair
    sha = append(origin, "a", "1")
    assert tracking.ensure_fetch_lines(clone, ORG) is True
    assert tracking.ensure_fetch_lines(clone, ORG) is False
    lines = git(clone, "config", "--get-all", "remote.origin.fetch").splitlines()
    for line in tracking.fetch_lines(ORG):
        assert lines.count(line) == 1
    git(clone, "fetch", "-q", "origin")
    tracked_name = f"refs/{ORG}/__remotes__/origin/{ORG}/__agents__/a/STOOK"
    assert refs(clone) == {tracked_name: sha}          # the tree only: no local STOOK was made
    assert tracking.tracked(clone, STOOK_A, org=ORG) == sha


def test_t2_a_read_is_local_and_keeps_the_last_fetched_value(tracking, pair):
    origin, clone = pair
    first = append(origin, "a", "1")
    tracking.ensure_fetch_lines(clone, ORG)
    git(clone, "fetch", "-q", "origin")
    second = append(origin, "a", "2")
    url = git(clone, "remote", "get-url", "origin")
    git(clone, "remote", "set-url", "origin", str(Path(url).parent / "nowhere.git"))
    assert tracking.tracked(clone, STOOK_A, org=ORG) == first      # unreachable remote, the read still answers
    git(clone, "remote", "set-url", "origin", url)
    git(clone, "fetch", "-q", "origin")
    assert tracking.tracked(clone, STOOK_A, org=ORG) == second != first


def test_t3_a_remote_that_holds_a_remotes_tree_is_not_nested(tracking, pair):
    origin, clone = pair
    sha = append(origin, "a", "1")
    planted = f"refs/{ORG}/__remotes__/origin/{ORG}/__agents__/z/STOOK"
    git(origin, "update-ref", planted, sha)
    tracking.ensure_fetch_lines(clone, ORG)
    git(clone, "fetch", "-q", "origin")
    assert refs(clone, f"refs/{ORG}/__remotes__/origin/{ORG}/__remotes__/") == {}
    assert tracking.tracked(clone, STOOK_A, org=ORG) == sha


BAD = ["Example", "ex/ample", "", "ex.", "ex..ample", "x.lock"]


@pytest.mark.parametrize("org,remote", [(b, "origin") for b in BAD] + [(ORG, b) for b in BAD])
def test_t4_names_refused_before_config_is_written(tracking, pair, org, remote):
    _, clone = pair
    before = git(clone, "config", "--list", "--local")
    with pytest.raises(ValueError):
        tracking.ensure_fetch_lines(clone, org, remote)
    assert git(clone, "config", "--list", "--local") == before


@pytest.mark.parametrize("ref", ["refs/heads/main", f"refs/{ORG}", f"refs/{ORG}/",
                                 f"refs/{ORG}/__remotes__/origin/{ORG}/__agents__/a/STOOK", f"refs/other/__agents__/a/STOOK"])
def test_t4_a_ref_outside_the_namespace_or_inside_a_remotes_tree_has_no_tracking_ref(tracking, ref):
    with pytest.raises(ValueError):
        tracking.tracking_ref(ref, org=ORG)


def test_t5_the_tracked_value_is_unpeeled(tracking, pair):
    origin, clone = pair
    sha = append(origin, "a", "1")
    tag_ref = f"refs/{ORG}/__teams__/t/__arbons__/1"
    obj = subprocess.run(["git", "-C", str(origin), "mktag"], input=(
        f"object {sha}\ntype commit\ntag 1\ntagger tracking-test <tracking-test@example.invalid> 0 +0000\n\nclose 1\n"
    ).encode(), capture_output=True, check=True, env=ENV).stdout.decode().strip()
    git(origin, "update-ref", tag_ref, obj)
    tracking.ensure_fetch_lines(clone, ORG)
    git(clone, "fetch", "-q", "origin")
    assert obj != sha
    assert tracking.tracked(clone, tag_ref, org=ORG) == obj
