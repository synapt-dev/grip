"""`gr2 review repin` carries a frozen range byte for byte.

A range whose hunk is not UTF-8 (a Latin-1 file, a binary file) must be rebased and
re-frozen exactly as git wrote it, compared here by git hash-object; a UTF-8 range is
unchanged; a range.patch that cannot be read is refused by name, never a traceback.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gr2.python_cli import app as gr2_app
from gr2.python_cli import review_rebind as rb


def _git(r: Path, *a: str, input: bytes | None = None) -> bytes:
    return subprocess.run(["git", "-C", str(r), *a], input=input, capture_output=True, check=True).stdout


def _commit(r: Path, name: str, data: bytes, msg: str) -> None:
    (r / name).write_bytes(data)
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", msg, "--no-gpg-sign")


def _ident(r: Path) -> None:
    _git(r, "config", "user.email", "dev@layne.pro")
    _git(r, "config", "user.name", "Layne Penney")


def _world(tmp_path: Path, name: str, data: bytes, moved: bytes = b"moved\n") -> dict:
    """Origin on dev; a one-commit range adding ``name`` frozen against it; then dev
    moves by an unrelated commit carrying ``moved`` (so the moved commits are read too)."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "dev")
    _ident(origin)
    _commit(origin, "a.txt", b"one\n", "c0")
    author = tmp_path / "author"
    _git(tmp_path, "clone", "-q", str(origin), str(author))
    _ident(author)
    _git(author, "checkout", "-q", "-b", "feat/x")
    _commit(author, name, data, "add file")
    frozen = tmp_path / "frozen-v1"
    rb.freeze(author, "refs/remotes/origin/dev", frozen, title="feat: add file", body="Body.\n")
    _commit(origin, "unrelated.txt", moved, "unrelated move")
    _git(author, "fetch", "-q", "origin")
    return {"origin": origin, "author": author, "frozen": frozen, "out": tmp_path / "frozen-v2",
            "name": name, "data": data, "tmp": tmp_path}


def _repin(w: dict):
    return CliRunner().invoke(gr2_app.app, ["review", "repin", str(w["frozen"]), "--repo", str(w["author"]),
                                            "--out-dir", str(w["out"]), "--ref", "refs/remotes/origin/dev"])


def _assert_rebased_byte_for_byte(w: dict) -> bytes:
    res = _repin(w)
    assert res.exit_code == 0, (res.output, res.exception)
    assert "rebased" in res.output
    patch = (w["out"] / "range.patch").read_bytes()
    check = w["tmp"] / "check"
    _git(w["tmp"], "clone", "-q", str(w["origin"]), str(check))
    _ident(check)
    _git(check, "am", "-q", str(w["out"] / "range.patch"))
    want = _git(check, "hash-object", "--stdin", input=w["data"]).strip()
    got = _git(check, "hash-object", w["name"]).strip()
    assert got == want
    return patch


def test_repin_latin1_range_is_byte_for_byte(tmp_path: Path) -> None:
    w = _world(tmp_path, "latin1.txt", b"caf\xe9\n", moved=b"d\xe9plac\xe9\n")
    patch = _assert_rebased_byte_for_byte(w)
    assert b"caf\xe9" in patch


def test_repin_binary_hunk_is_byte_for_byte(tmp_path: Path) -> None:
    w = _world(tmp_path, "blob.bin", b"\x00\xff\xfe\x80", moved=b"\x86\x01pkg\n")
    _assert_rebased_byte_for_byte(w)


def test_repin_utf8_range_is_unchanged(tmp_path: Path) -> None:
    w = _world(tmp_path, "cafe.txt", "café\n".encode("utf-8"))
    patch = _assert_rebased_byte_for_byte(w)
    assert "café" in patch.decode("utf-8")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_repin_unreadable_range_is_refused_by_name(tmp_path: Path) -> None:
    w = _world(tmp_path, "a2.txt", b"two\n")
    (w["frozen"] / "range.patch").chmod(0)
    try:
        res = _repin(w)
    finally:
        (w["frozen"] / "range.patch").chmod(0o644)
    assert not w["out"].exists()
    assert res.exit_code == 2, (res.output, res.exception)
    assert "range_unreadable" in res.output
    assert "Traceback" not in res.output
    assert not isinstance(res.exception, (OSError, UnicodeError)), res.exception
