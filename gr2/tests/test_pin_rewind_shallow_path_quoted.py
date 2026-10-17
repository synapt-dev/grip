"""The shallow-checkout pin refusal names the member dir in a command the person runs (`git fetch --unshallow` in
<dir>), so the dir is printed shell-quoted: a member path with a space or shell text must paste as one word."""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli.grip_cli import PinRewindRefusal, _refuse_pin_rewind


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def test_the_shallow_refusal_quotes_the_member_dir(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q")
    for n in ("1", "2"):
        (origin / "f").write_text(n, encoding="utf-8")
        _git(origin, "add", "f")
        _git(origin, "commit", "-qm", n)
    path = tmp_path / "a dir; touch x"
    _git(tmp_path, "clone", "-q", "--depth", "1", origin.as_uri(), str(path))
    assert _git(path, "rev-parse", "--is-shallow-repository") == "true"
    head = _git(path, "rev-parse", "HEAD")
    member = {"name": "alpha", "path": str(path), "pin": "0" * 39 + "1"}
    with pytest.raises(PinRewindRefusal) as raised:
        _refuse_pin_rewind(member, path, head)
    said = str(raised.value)
    assert "cannot tell whether HEAD" in said, said
    assert f"run `git fetch --unshallow` in {shlex.quote(str(path))} and" in said, said
    assert f"in {path} and" not in said, said
