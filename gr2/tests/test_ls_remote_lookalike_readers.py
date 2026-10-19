"""The six ls-remote readers outside grip read each listing by "\\n" and take a name only when it is EXACTLY the ref asked.

`git ls-remote <remote> refs/heads/trunk` matches a pattern by its tail, so it also lists a valid ref named
refs/heads/trunk<sep>/refs/heads/trunk (and refs/heads/a<sep>/refs/heads/trunk), where <sep> is U+2028 or U+0085.
str.splitlines (and str.split()) cut that name at <sep>, leaving a row that reads "B\trefs/heads/trunk".
The remote holds refs/heads/trunk at A and both lookalikes at B.

Rows:
W1 app._live_remote_tips gives trunk at A;
W2 grip_cli._atomic_push sees origin already holding trunk at A (a refused push that landed);
W3 grip_cli._leases names trunk at A when it asks the clone to fetch first;
T1 app._pr_create_for_review finds trunk on the remote at the reviewed commit A (one match, not two);
T2 push._remote_branch_sha returns A (one receipt, not three);
T3 app._start_lane_branch starts a new lane at the live tip A (it takes the first exact row, and the real name sorts first).
"""
from __future__ import annotations

import subprocess

import pytest
import typer

from gr2.python_cli import app, grip_cli, push

SEPS = pytest.mark.parametrize("sep", ["\u2028", "\u0085"], ids=["u2028", "u0085"])
TRUNK = "refs/heads/trunk"


def _g(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _world(tmp_path, sep):
    """A bare remote: trunk at A, both lookalikes at B, HEAD -> trunk. Returns (remote, clone at trunk, A, B)."""
    ws = tmp_path / "ws"
    subprocess.run(["git", "init", "-q", str(ws)], check=True)
    tips = []
    for n in ("a", "b"):
        _g(ws, "commit", "-q", "--allow-empty", "-m", n)
        tips.append(_g(ws, "rev-parse", "HEAD"))
    a, b = tips
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _g(ws, "push", "-q", "--no-verify", str(remote), f"{b}:{TRUNK}")
    for name, sha in ((TRUNK, a), (f"refs/heads/trunk{sep}/{TRUNK}", b), (f"refs/heads/a{sep}/{TRUNK}", b)):
        _g(remote, "update-ref", name, sha)
    _g(remote, "symbolic-ref", "HEAD", TRUNK)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True, capture_output=True)
    return remote, clone, a, b


@SEPS
def test_w1_live_remote_tips(tmp_path, sep):
    remote, clone, a, _ = _world(tmp_path, sep)
    assert app._live_remote_tips(clone, str(remote), TRUNK) == {TRUNK: a}


@SEPS
def test_w2_atomic_push_sees_the_landed_tip(tmp_path, sep):
    remote, clone, a, b = _world(tmp_path, sep)
    # Origin's trunk is at B when the push leaves; another push lands A first (the hook moves it), so this one is
    # refused, and the reader asks origin whether trunk already holds A.
    _g(remote, "update-ref", TRUNK, b)
    hook = tmp_path / "hooks" / "pre-receive"
    hook.parent.mkdir()
    hook.write_text(f"#!/bin/sh\nenv -u GIT_QUARANTINE_PATH git update-ref {TRUNK} {a}\nexit 1\n")
    hook.chmod(0o755)
    _g(remote, "config", "core.hooksPath", str(hook.parent))
    assert grip_cli._atomic_push(clone, [(a, TRUNK)], verify=False, leases={TRUNK: b}) is True
    assert _g(remote, "rev-parse", TRUNK) == a


@SEPS
def test_w3_leases_names_the_real_tip(tmp_path, sep):
    _, clone, a, _ = _world(tmp_path, sep)
    _g(clone, "update-ref", "-d", "refs/remotes/origin/trunk")
    with pytest.raises(grip_cli.NativeStoreRefusal) as exc:
        grip_cli._leases(clone, a, [TRUNK])
    assert f"origin holds {TRUNK} at {a} " in str(exc.value)


@SEPS
def test_t1_pr_create_reads_one_remote_tip(tmp_path, sep, monkeypatch, capsys):
    remote, clone, a, _ = _world(tmp_path, sep)
    m = {"key": "m", "commit": a, "remote": str(remote)}
    monkeypatch.setattr(app, "resolve_review_subject", lambda *_: ("t", [m]))
    monkeypatch.setattr(app.pr_ops, "review_pr_groups", lambda *_: [])
    monkeypatch.setattr(app.platform_ops, "get_platform_adapter", lambda *_: object())
    monkeypatch.setattr(app.platform_ops, "adapter_api_version", lambda *_: 1)
    monkeypatch.setattr(app, "_review_members_on_host", lambda *_: {"o/m": (m, clone)})
    monkeypatch.setattr(app, "_effective_target_matches", lambda *_: True)
    from gr2.python_cli import review_members
    monkeypatch.setattr(review_members, "workspace_members", lambda *_: [])
    with pytest.raises(typer.Exit):  # it passes the tip check and stops at the next one: no base branch is known
        app._pr_create_for_review(clone, "t", "github", "", False, None, None, None)
    err = capsys.readouterr().err
    assert "not the reviewed commit" not in err and "members disagree on the base branch" in err


@SEPS
def test_t2_remote_branch_sha_reads_one_receipt(tmp_path, sep):
    remote, clone, a, _ = _world(tmp_path, sep)
    assert push._remote_branch_sha(clone, str(remote), "trunk") == a


@SEPS
def test_t3_start_lane_branch_starts_at_the_live_tip(tmp_path, sep):
    _, clone, a, _ = _world(tmp_path, sep)
    assert app._start_lane_branch("m", clone, "feat", "trunk", seed_is_work=False) == (a, "tip")
