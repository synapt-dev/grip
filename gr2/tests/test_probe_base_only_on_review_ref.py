"""Probe: the base is in refs/review/x's history and on no branch or tag."""
from pathlib import Path

from tests.native_root_helper import native_root
from tests.test_review_pin_at_base import _commit, _git, app, runner


def _w(tmp_path: Path, base_on_trunk: bool) -> dict:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "trunk", str(remote))
    src = tmp_path / "src"
    _git(tmp_path, "init", "-q", "-b", "trunk", str(src))
    _commit(src, "a")
    _git(src, "remote", "add", "origin", str(remote))
    _git(src, "push", "-q", "origin", "trunk")
    base = _commit(src, "b")                     # B: on trunk only when base_on_trunk
    if base_on_trunk:
        _git(src, "push", "-q", "origin", "trunk")
    _git(src, "push", "-q", "origin", "HEAD:refs/review/x")
    _git(src, "checkout", "-qb", "feat")
    head = _commit(src, "h")
    patch = tmp_path / "range.patch"
    patch.write_text(_git(src, "format-patch", "--stdout", f"{base}..{head}") + "\n")
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(remote), str(other))
    _git(other, "fetch", "-q", "origin", "refs/review/x")
    _git(other, "checkout", "-q", "--detach", "FETCH_HEAD")
    tip = _commit(other, "c")                    # the ref moved past B
    _git(other, "push", "-q", "origin", "HEAD:refs/review/x")
    ws = native_root(tmp_path / "ws")
    _git(ws, "config", "user.name", "t")
    _git(ws, "config", "user.email", "t@e")
    return {"remote": remote, "ws": ws, "base": base, "head": head, "patch": patch, "tip": tip}


def _pin(w):
    return runner.invoke(app, ["review", "pin", str(w["ws"]), "--repo", "src",
                               "--remote", "file://" + str(w["remote"]),
                               "--base", w["base"], "--head", w["head"], "--ref", "refs/review/x",
                               "--from-range", str(w["patch"]), "--at-base"])


def test_control_base_on_trunk(tmp_path):
    r = _pin(_w(tmp_path, True))
    assert r.exit_code == 0, r.output


def test_base_only_in_review_ref_history(tmp_path):
    r = _pin(_w(tmp_path, False))
    assert r.exit_code == 0, r.output
