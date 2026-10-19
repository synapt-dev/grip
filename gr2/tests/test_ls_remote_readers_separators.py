"""The four ls-remote readers in grip read by "\\n", never str.splitlines (U+2028, U+0085 sit inside a ref name).

A bare remote holds refs/heads/trunk at A, a lookalike refs/heads/trunk<sep>x at B, and refs/heads/trunk<sep><C>,
where C is a commit no ref on the remote points at.

Rows:
R1 _remote_ref_matches for the lookalike returns it whole, at B (not a cut refs/heads/trunk plus a stray "x");
R2 _head_present_on_remote says C is not on the remote (the cut tail of a name is not a tip);
R3 _remote_ref_tips names each of the three refs whole at its own tip (trunk stays at A);
R4 _review_remote_target for the lookalike returns B (not cannot_measure_review_ref).
"""
from __future__ import annotations

import importlib
import subprocess

import pytest

SEPS = pytest.mark.parametrize("sep", ["\u2028", "\u0085"], ids=["u2028", "u0085"])


def _g(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def _world(tmp_path, sep):
    ws = tmp_path / "ws"
    subprocess.run(["git", "init", "-q", str(ws)], check=True)
    tips = []
    for n in ("a", "b", "c"):
        _g(ws, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", n)
        tips.append(_g(ws, "rev-parse", "HEAD"))
    a, b, c = tips
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _g(ws, "push", "-q", str(remote), f"{b}:refs/heads/keep")
    _g(remote, "update-ref", "-d", "refs/heads/keep")
    refs = {"refs/heads/trunk": a, f"refs/heads/trunk{sep}x": b, f"refs/heads/trunk{sep}{c}": a}
    for name, sha in refs.items():
        _g(remote, "update-ref", name, sha)
    return importlib.import_module("gr2.python_cli.grip"), ws, str(remote), refs, b, c


@SEPS
def test_r1_remote_ref_matches_reads_a_lookalike_whole(tmp_path, sep):
    grip, ws, remote, _, b, _ = _world(tmp_path, sep)
    assert grip._remote_ref_matches(ws, remote, f"refs/heads/trunk{sep}x") == [(b, f"refs/heads/trunk{sep}x")]


@SEPS
def test_r2_head_present_on_remote_is_not_fooled_by_a_name_tail(tmp_path, sep):
    grip, ws, remote, _, b, c = _world(tmp_path, sep)
    assert grip._head_present_on_remote(ws, remote, c) is False
    assert grip._head_present_on_remote(ws, remote, b) is True


@SEPS
def test_r3_remote_ref_tips_names_each_ref_whole(tmp_path, sep):
    grip, ws, remote, refs, _, _ = _world(tmp_path, sep)
    assert grip._remote_ref_tips(ws, remote) == refs


@SEPS
def test_r4_review_remote_target_reads_a_lookalike(tmp_path, sep):
    grip, ws, remote, _, b, _ = _world(tmp_path, sep)
    assert grip._review_remote_target(ws, remote, f"refs/heads/trunk{sep}x") == b
