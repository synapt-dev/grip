"""A cwd-only store verb run on a member names the workspace root above it, never "store init here".

THE DEFECT: from inside a member checkout of a compat store,
`gr2 store push` (cwd) and `gr2 store commit -C <member>` exit 5 with "no grip.toml at <member> and no
committed spec at HEAD; ... run gr2 store init here", which, if followed, nests a second store inside a
member: the case `_store_root_upward`'s docstring is written to avoid.

THE CONTRACT these rows pin (advice text only):
  * on a root that sits inside a workspace, the refusal keeps its "no grip.toml at <root>" prefix and its
    exit 5, names the workspace root above, and gives no init advice; nothing is written;
  * on a bare directory with no workspace above it, the init advice is unchanged (it is right there).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_store_break_attempts import _bare_remote, _cli, _clone_member, _git, _git_out
from tests.test_store_git_native_smoke import gr2

FIXED = "2026-10-19T12:00:00Z"


def _ws(base: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
        monkeypatch.setenv(key, FIXED)
    ws = base / "ws"
    for name in ("alpha", "beta"):
        _git(_clone_member(base, name), "push", "-q", "origin", "main")
    code, out = _cli("store", "init", str(ws), "--member", "alpha", "--member", "beta")
    assert code == 0, out
    return ws


def _state(ws: Path) -> tuple[list[str], list[str], list[str]]:
    """Everything a verb could have written: the root's refs, the member's refs, the member's files."""
    member = ws / "alpha"
    files = sorted(str(p.relative_to(member)) for p in member.rglob("*") if ".git" not in p.parts)
    return (_git_out(ws, "for-each-ref", "--format=%(refname) %(objectname)").splitlines(),
            _git_out(member, "for-each-ref", "--format=%(refname) %(objectname)").splitlines(), files)


def _assert_names_root_above(code: int, out: str, ws: Path) -> None:
    member = (ws / "alpha").resolve()
    assert code == 5, out
    assert f"no grip.toml at {member}" in out, out
    assert f"inside the store at {ws.resolve()}" in out, out
    assert f"-C {ws.resolve()}" in out, out
    assert "store init here" not in out, out


def test_store_push_from_inside_a_member_names_the_root_above(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    before = _state(ws)
    monkeypatch.chdir(ws / "alpha")
    code, out = _cli("store", "push")
    _assert_names_root_above(code, out, ws)
    assert _state(ws) == before
    assert not (ws / "alpha" / "grip.toml").exists()


def test_store_commit_naming_the_member_names_the_root_above(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    before = _state(ws)
    monkeypatch.chdir(ws)
    code, out = _cli("store", "commit", "-C", str(ws / "alpha"), "-m", "rec")
    _assert_names_root_above(code, out, ws)
    assert _state(ws) == before
    assert not (ws / "alpha" / "grip.toml").exists()


def test_a_bare_directory_with_no_workspace_above_keeps_the_init_advice(tmp_path, monkeypatch):
    bare = tmp_path / "bare"
    bare.mkdir()
    _git(bare, "init", "-q")
    monkeypatch.chdir(bare)
    code, out = _cli("store", "status")
    assert code == 5, out
    assert f"no grip.toml at {bare.resolve()}" in out or f"no grip.toml at {bare}" in out, out
    assert "store init here" in out, out
    assert "is inside the" not in out, out


def test_a_directory_nested_in_a_member_names_the_store_root_above(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    sub = ws / "alpha" / "a" / "b"
    sub.mkdir(parents=True)
    before = _state(ws)
    monkeypatch.chdir(ws)
    code, out = _cli("store", "status", "-C", str(sub))
    assert code == 5, out
    assert f"inside the store at {ws.resolve()}" in out, out
    assert "store init here" not in out, out
    assert _state(ws) == before


def _tree(path: Path) -> dict[str, bytes]:
    """Every file under `path`, `.git` included, as bytes: what a refused verb must leave byte-identical."""
    return {str(p.relative_to(path)): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}


def test_store_add_naming_a_member_names_the_store_root_above(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    gamma = _bare_remote(tmp_path, "gamma")
    before = _tree(ws)
    monkeypatch.chdir(ws)
    code, out = _cli("store", "add", "-C", str(ws / "alpha"), f"gamma={gamma}")
    assert code == 4, out
    assert f"inside the store at {ws.resolve()}" in out, out
    assert f"-C {ws.resolve()}" in out, out
    assert "store init" not in out, out
    assert _tree(ws) == before


def test_store_add_naming_a_bare_directory_keeps_the_init_advice(tmp_path, monkeypatch):
    bare = tmp_path / "bare"
    bare.mkdir()
    gamma = _bare_remote(tmp_path, "gamma")
    monkeypatch.chdir(tmp_path)
    code, out = _cli("store", "add", "-C", str(bare), f"gamma={gamma}")
    assert code == 4, out
    assert "is not a store; run store init first" in out, out
    assert "is inside the" not in out, out
    assert list(bare.iterdir()) == []


def test_store_migrate_reviews_inside_a_member_refuses_without_a_traceback(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    before = _tree(ws)
    result = gr2(ws / "alpha", "migrate-reviews", "-C", ".", check=False)
    assert result.returncode == 5, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    assert result.stderr.startswith("refused: "), result.stderr
    assert _tree(ws) == before


def _griplink_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A griplink root R (`store init --mode full`) with one member `api` added by top-level gr2 add."""
    for key in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
        monkeypatch.setenv(key, FIXED)
    for key, value in (("GIT_CONFIG_COUNT", "1"), ("GIT_CONFIG_KEY_0", "protocol.file.allow"),
                       ("GIT_CONFIG_VALUE_0", "always")):
        monkeypatch.setenv(key, value)  # gr2 add clones the member over a local path
    url = _clone_member(tmp_path, "api")
    _git(url, "push", "-q", "origin", "main")
    origin = _git_out(url, "remote", "get-url", "origin")
    monkeypatch.chdir(tmp_path)
    code, out = _cli("store", "init", "--mode", "full", "R")
    assert code == 0, out
    root = tmp_path / "R"
    monkeypatch.chdir(root)
    code, out = _cli("add", origin, "api")
    assert code == 0, out
    return root


def test_store_push_inside_a_griplink_member_names_the_griplink_root_without_a_store_verb(tmp_path, monkeypatch):
    root = _griplink_root(tmp_path, monkeypatch)
    refs = _git_out(root, "for-each-ref", "--format=%(refname) %(objectname)")
    monkeypatch.chdir(root / "api")
    code, out = _cli("store", "push")
    assert code == 5, out
    assert f"inside the griplink workspace at {root.resolve()}" in out, out
    assert "store init here" not in out, out
    assert "-C " not in out, out
    assert _git_out(root, "for-each-ref", "--format=%(refname) %(objectname)") == refs
