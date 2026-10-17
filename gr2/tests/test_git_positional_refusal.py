"""A user value that reaches git as a positional (a remote, a url) and begins with '-' is refused before any git
call, so git never reads it as an option such as --upload-pack or --receive-pack, which run a program.
Rows:
P1 gitops.positional refuses an empty or option-shaped value and passes a remote name, a path or a url through;
P2 sync status probes each spec url with ls-remote: an option-shaped url is reported unreachable, nothing runs;
P3 delete refuses an option-shaped remote before its ls-remote and its push; nothing runs, nothing is deleted;
P4 the remote read under a leased push and a member check refuses an option-shaped remote; nothing runs.
"""
from __future__ import annotations

import importlib
import os
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import gitops
from gr2.python_cli import leased_push
from gr2.python_cli.gitops import probe_remote

ORG = "example"
ENV = dict(os.environ, GIT_AUTHOR_NAME="positional-test", GIT_AUTHOR_EMAIL="positional-test@example.invalid",
           GIT_COMMITTER_NAME="positional-test", GIT_COMMITTER_EMAIL="positional-test@example.invalid")
OPTIONS = ["--upload-pack", "--receive-pack"]


@pytest.fixture(autouse=True)
def _identity(monkeypatch):
    for k, v in ENV.items():
        if k.startswith("GIT_"):
            monkeypatch.setenv(k, v)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True,
                          env=ENV).stdout.decode().strip()


@pytest.fixture
def world(tmp_path):
    """`me` holds one STOOK, pushed to `origin` and fetched back, so a delete or a push of it would go through."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    me = tmp_path / "me"
    subprocess.run(["git", "init", "-q", str(me)], check=True)
    git(me, "remote", "add", "origin", str(origin))
    importlib.import_module("gr2.python_cli.tracking").ensure_fetch_lines(me, ORG)
    stook = f"refs/{ORG}/__agents__/a/STOOK"
    tree = git(me, "mktree")
    git(me, "update-ref", stook, git(me, "commit-tree", tree, "-m", "one"))
    leased_push.push(me, [stook], org=ORG)
    git(me, "fetch", "-q", "origin")
    return origin, me, stook


@pytest.mark.parametrize("value", ["", "-", "--upload-pack=x", "-u", "--receive-pack=x"])
def test_p1_positional_refuses_an_empty_or_option_shaped_value(value):
    with pytest.raises(ValueError, match="remote_option_shaped"):
        gitops.positional(value, "remote", ValueError)


@pytest.mark.parametrize("value", ["origin", "/srv/a.git", "https://example.invalid/a.git", "a-b", "x--y"])
def test_p1_positional_passes_a_name_a_path_or_a_url(value):
    assert gitops.positional(value, "url") == value


@pytest.mark.parametrize("option", OPTIONS)
def test_p2_probe_never_runs_an_option_shaped_url(tmp_path, option):
    marker = tmp_path / "ran"
    reachable, detail = probe_remote(f"{option}=touch {marker}")
    assert not marker.exists()
    assert reachable is False and "url_option_shaped" in detail


def test_p2_probe_still_reaches_a_real_url(world):
    origin, _, _ = world
    assert probe_remote(str(origin)) == (True, "")


@pytest.mark.parametrize("option", OPTIONS)
def test_p3_delete_refuses_an_option_shaped_remote(world, tmp_path, option):
    origin, me, stook = world
    marker = tmp_path / "ran"
    delete = importlib.import_module("gr2.python_cli.delete")
    with pytest.raises(leased_push.PushRefused, match="remote_option_shaped"):
        delete.delete(me, stook, org=ORG, remote=f"{option}=touch {marker}")
    assert not marker.exists()
    assert git(origin, "for-each-ref", "--format=%(refname)") == stook
    assert git(me, "rev-parse", "--verify", stook)


def test_p3_delete_still_deletes_through_a_named_remote(world):
    origin, me, stook = world
    importlib.import_module("gr2.python_cli.delete").delete(me, stook, org=ORG, remote="origin")
    assert git(origin, "for-each-ref", "--format=%(refname)") == ""


@pytest.mark.parametrize("option", OPTIONS)
def test_p4_the_remote_read_refuses_an_option_shaped_remote(world, tmp_path, option):
    _, me, stook = world
    marker = tmp_path / "ran"
    with pytest.raises(leased_push.PushRefused, match="remote_option_shaped"):
        leased_push._remote_values(me, f"{option}=touch {marker}", [stook])
    assert not marker.exists()


def test_p4_the_remote_read_still_reads_a_named_remote(world):
    origin, me, stook = world
    assert leased_push._remote_values(me, "origin", [stook]) == {stook: git(origin, "rev-parse", stook)}
