"""A member path that leaves the root is refused by both declaration readers, before any member is kept.

git itself refuses `git submodule add` at `../x` or `/abs`, but a hand-written .gitmodules or grip.toml can still name
one, and a member path is where gr2 later reads a gitlink and writes a checkout. An absolute path, or one with a `..`
part, is refused with `member_path_outside_root` and the path quoted; `a/b` stays a member.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import declared_sets

OUTSIDE = ("../x", "../outside", "/abs/path", "a/../x")
INSIDE = ("alpha", "a/b")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _gitmodules_root(tmp_path: Path, path: str) -> Path:
    """A root whose committed .gitmodules names one member `m` at `path`."""
    root = tmp_path / "root"
    root.mkdir()
    _git(root, "init", "-q", "-b", "trunk")
    (root / ".gitmodules").write_text(f'[submodule "m"]\n\tpath = {path}\n\turl = https://example.invalid/m.git\n')
    _git(root, "add", ".gitmodules")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "m")
    return root


def _toml(path: str) -> dict:
    return {"members": [{"name": "m", "path": path, "remotes": {"origin": "https://example.invalid/m.git"}}],
            "sets": [{"name": "core", "members": ["m"]}]}


@pytest.mark.parametrize("path", OUTSIDE)
def test_gitmodules_member_path_outside_root_refuses(tmp_path: Path, path: str) -> None:
    root = _gitmodules_root(tmp_path, path)
    with pytest.raises(declared_sets.DeclaredSetError) as refused:
        declared_sets.read(root)
    message = str(refused.value)
    assert message.startswith(f"member_path_outside_root: member 'm' path {path!r} in .gitmodules"), message


@pytest.mark.parametrize("path", INSIDE)
def test_gitmodules_member_path_inside_root_is_kept(tmp_path: Path, path: str) -> None:
    declaration = declared_sets.read(_gitmodules_root(tmp_path, path))
    assert declaration.members["m"]["path"] == path


@pytest.mark.parametrize("path", OUTSIDE)
def test_grip_toml_member_path_outside_root_refuses(path: str) -> None:
    with pytest.raises(declared_sets.DeclaredSetError) as refused:
        declared_sets.parse(_toml(path))
    message = str(refused.value)
    assert message.startswith(f"member_path_outside_root: member 'm' path {path!r} in grip.toml"), message


@pytest.mark.parametrize("path", INSIDE)
def test_grip_toml_member_path_inside_root_is_kept(path: str) -> None:
    declaration = declared_sets.parse(_toml(path))
    assert declaration.members["m"]["path"] == path
