"""A workspace gr commit refuses a remote that is an option or holds a control
character, on write (before anything is hashed) and on read (the stored value).

The rule is the review tree's: a remote that starts with "-" or holds any
ord(c) < 32 or ord(c) == 127 is not a remote.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import grip

_C = "a" * 40
_B = "b" * 40


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    grip_dir = ws / ".grip"
    grip_dir.mkdir(parents=True)
    for args in (("init", "-b", "main"), ("config", "user.name", "Grip"),
                 ("config", "user.email", "grip@example.com"),
                 ("commit", "--allow-empty", "-m", "init grip")):
        subprocess.run(["git", "-C", str(grip_dir), *args], check=True, capture_output=True, text=True)
    return ws


def _fields(remote: str) -> list[dict[str, str]]:
    return [{"key": "a", "remote": remote, "path": "repos/a", "commit": _C, "base": _B}]


def _write_stored(ws: Path, remote: str) -> str:
    """Write a workspace commit whose remote blob holds `remote`, past the writer."""
    fields = [f"100644 blob {grip._hash_blob(ws, v)}\t{n}"
              for n, v in (("remote", remote), ("path", "repos/a"), ("commit", _C), ("base", _B))]
    repos_tree = grip._mktree(ws, [f"040000 tree {grip._mktree(ws, fields)}\ta"])
    meta_tree = grip._mktree(ws, [
        f"100644 blob {grip._hash_blob(ws, grip._WORKSPACE_SCHEMA)}\tschema",
        f"100644 blob {grip._hash_blob(ws, 'workspace')}\tkind",
    ])
    root = grip._mktree(ws, [f"040000 tree {meta_tree}\t.grip", f"040000 tree {repos_tree}\trepos"])
    return grip._commit_tree(ws, root, parent=grip._current_head(ws), message="stored remote fixture")


def _objects(ws: Path) -> str:
    return subprocess.run(["git", "-C", str(ws / ".grip"), "count-objects", "-v"],
                          check=True, capture_output=True, text=True).stdout


@pytest.mark.parametrize("remote", ["--upload-pack=x", "https://example.invalid/r\x1b.git"])
def test_writer_refuses_a_remote_that_is_not_a_remote(tmp_path: Path, remote: str) -> None:
    ws = _ws(tmp_path)
    head = grip._current_head(ws)
    before = _objects(ws)
    with pytest.raises(grip.GripCorruptError, match="a remote is not a remote") as err:
        grip.create_workspace_commit(ws, _fields(remote))
    assert remote not in str(err.value)
    assert grip._current_head(ws) == head
    assert _objects(ws) == before


@pytest.mark.parametrize("remote", ["--upload-pack=x", "https://example.invalid/r\x1b.git"])
def test_reader_refuses_a_stored_remote_that_is_not_a_remote(tmp_path: Path, remote: str) -> None:
    ws = _ws(tmp_path)
    commit = _write_stored(ws, remote)
    with pytest.raises(grip.GripCorruptError, match="a remote is not a remote") as err:
        grip.read_workspace_commit(ws, commit)
    assert remote not in str(err.value)


def test_plain_remote_still_round_trips(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    commit = grip.create_workspace_commit(ws, _fields("https://example.invalid/r.git"))
    rows = grip.read_workspace_commit(ws, commit)
    assert [r["remote"] for r in rows] == ["https://example.invalid/r.git"]
