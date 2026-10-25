"""`store commit` and `store checkout` run from inside a member find the workspace root above them.

THE DEFECT (probe on dev bd748276): with cwd = a member's subdirectory
(alpha/a/b) both verbs took the cwd itself for the root, exited 5 with "no grip.toml at
<ws>/alpha/a/b and no committed spec at HEAD; ... run store init here", and the advice, if
followed, nested a second store inside a member. From the root both exit 0.

THE CONTRACT these rows pin:
  * from alpha/a/b each verb behaves exactly as from the root: exit 0, the same recorded
    spec and gitlinks (the root commit's tree), the same refs;
  * the walk is the review verbs' own (`app._resolve_workspace_root`), not a second one;
  * from a plain directory with no workspace above it, the refusal names where it started
    and the kinds of root it looked for, and no longer tells the caller to init there.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_store_break_attempts import _cli, _clone_member, _git, _git_out

FIXED = "2026-10-19T12:00:00Z"


def _ws(base: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
        monkeypatch.setenv(key, FIXED)
    ws = base / "ws"
    for name in ("alpha", "beta"):
        _git(_clone_member(base, name), "push", "-q", "origin", "main")
    code, out = _cli("store", "init", str(ws), "--member", "alpha", "--member", "beta")
    assert code == 0, out
    assert (ws / "grip.toml").is_file()
    return ws


def _sub(ws: Path) -> Path:
    sub = ws / "alpha" / "a" / "b"
    sub.mkdir(parents=True)
    return sub


def _refs(ws: Path) -> list[str]:
    return _git_out(ws, "for-each-ref", "--format=%(refname)").splitlines()


def _commit_from(ws: Path, cwd: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[int, str]:
    monkeypatch.chdir(cwd)
    return _cli("store", "commit", "-m", "rec", "--json")


def test_store_commit_from_member_subdirectory_is_the_root_commit(tmp_path, monkeypatch):
    root_ws = _ws(tmp_path / "r", monkeypatch)
    sub_ws = _ws(tmp_path / "s", monkeypatch)
    code_root, out_root = _commit_from(root_ws, root_ws, monkeypatch)
    code_sub, out_sub = _commit_from(sub_ws, _sub(sub_ws), monkeypatch)
    assert (code_sub, code_root) == (0, 0), out_sub + out_root
    assert "store init here" not in out_sub
    assert not (sub_ws / "alpha" / "a" / "b" / "grip.toml").exists()
    assert _git_out(sub_ws, "ls-tree", "HEAD", "alpha", "beta") == _git_out(root_ws, "ls-tree", "HEAD", "alpha", "beta")
    spec_sub = _git_out(sub_ws, "show", "HEAD:grip.toml").replace(str(tmp_path / "s"), "<B>")
    assert spec_sub == _git_out(root_ws, "show", "HEAD:grip.toml").replace(str(tmp_path / "r"), "<B>")
    assert _refs(sub_ws) == _refs(root_ws)


def test_store_checkout_head_from_member_subdirectory_is_the_root_checkout(tmp_path, monkeypatch):
    root_ws = _ws(tmp_path / "r", monkeypatch)
    sub_ws = _ws(tmp_path / "s", monkeypatch)
    sub = _sub(sub_ws)
    for ws in (root_ws, sub_ws):
        assert _commit_from(ws, ws, monkeypatch)[0] == 0
    monkeypatch.chdir(root_ws)
    code_root, out_root = _cli("store", "checkout", "HEAD", "--json")
    monkeypatch.chdir(sub)
    code_sub, out_sub = _cli("store", "checkout", "HEAD", "--json")
    assert (code_sub, code_root) == (0, 0), out_sub + out_root
    assert json.loads(out_sub)["members"] == json.loads(out_root)["members"]
    assert _refs(sub_ws) == _refs(root_ws)


def test_store_commit_with_no_workspace_above_names_start_and_root_kinds(tmp_path, monkeypatch):
    plain = tmp_path / "plain" / "x"
    plain.mkdir(parents=True)
    monkeypatch.chdir(plain)
    code, out = _cli("store", "commit", "-m", "rec")
    assert code == 5, out
    assert str(plain) in out
    assert "no workspace root at or above" in out
    assert "grip.toml" in out and "workspace_spec.toml" in out
    assert "store init here" not in out
