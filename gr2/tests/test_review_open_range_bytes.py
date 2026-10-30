"""A range that is not UTF-8 is carried byte for byte past the pin: `review show` prints
it without a crash, `review open` rebuilds the lane so every file's blob id equals the source's
and the lane tree equals the head tree, and `review verify` hashes the stored bytes.
A UTF-8 range reads exactly as before."""
from __future__ import annotations

from tests.native_root_helper import native_root

import hashlib
import os
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from gr2.python_cli import app as gr2_app
from gr2.python_cli import grip


def _git(cwd: Path, *a: str, input: bytes | None = None) -> bytes:
    return subprocess.run(["git", *a], cwd=cwd, input=input, capture_output=True, check=True).stdout


def _pinned(tmp_path: Path, name: str, content: bytes, *, utf8: bool = False) -> dict:
    """A bare origin at BASE, a work clone whose head adds `name` holding `content`, pinned --from-range."""
    origin = tmp_path / "alpha.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "trunk", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "config", "user.email", "a@e.invalid")
    _git(work, "config", "user.name", "a")
    (work / "f.txt").write_text("base\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "base")
    _git(work, "push", "-q", "origin", "HEAD:trunk")
    base = _git(work, "rev-parse", "HEAD").decode().strip()
    (work / name).write_bytes(content)
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "add file")
    head = _git(work, "rev-parse", "HEAD").decode().strip()
    patch = _git(work, "format-patch", "--binary", f"{base}..{head}", "--stdout")
    # The subject before the instrument: the range is (or for the control, is not) outside UTF-8.
    try:
        patch.decode("utf-8")
        assert utf8, "fixture range is valid UTF-8; it witnesses nothing"
    except UnicodeDecodeError:
        assert not utf8, "control range is not UTF-8"
    range_file = tmp_path / "range.patch"
    range_file.write_bytes(patch)
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    native_root(ws)
    res = CliRunner().invoke(gr2_app.app, [
        "review", "pin", str(ws), "--repo", "alpha", "--remote", str(origin), "--base", base, "--head", head,
        "--ref", "refs/heads/trunk", "--from-range", str(range_file)])
    assert res.exit_code == 0, res.output
    commit = res.output.strip()[len("gr:"):]
    stored = _git(grip._bind_dir(ws), "cat-file", "blob", f"{commit}:004.2r_members/0000001/008.2_range_patch")
    assert stored == patch
    return {"ws": ws, "work": work, "head": head, "commit": commit, "name": name, "patch": patch}


def _open_matches_source(w: dict, lane: Path) -> None:
    grip.reconstruct_review_lane(w["ws"], w["commit"], "alpha", lane)
    got = _git(lane, "hash-object", "--", w["name"]).decode().strip()
    want = _git(w["work"], "hash-object", "--", w["name"]).decode().strip()
    assert got == want
    assert _git(lane, "rev-parse", "HEAD^{tree}") == _git(w["work"], "rev-parse", f"{w['head']}^{{tree}}")


def test_show_lists_a_file_whose_name_is_not_utf8(tmp_path: Path) -> None:
    w = _pinned(tmp_path, os.fsdecode(b"caf\xe9.txt"), b"caf\xe9\n")
    res = CliRunner().invoke(gr2_app.app, ["review", "show", str(w["ws"]), "gr:" + w["commit"]])
    assert res.exit_code == 0, res.output
    assert res.exception is None
    files = [line for line in res.output.splitlines() if line.startswith("  files: ")]
    assert files, res.output  # files come from the head objects, not the range text; show must not crash on it


def test_open_rebuilds_a_latin1_hunk_byte_for_byte(tmp_path: Path) -> None:
    w = _pinned(tmp_path, "latin1.txt", b"caf\xe9\n")
    _open_matches_source(w, tmp_path / "lane")


def test_open_rebuilds_a_high_byte_hunk_byte_for_byte(tmp_path: Path) -> None:
    high = bytes(range(0x80, 0x100))
    w = _pinned(tmp_path, "high.dat", b"\n".join(high[i:i + 16] for i in range(0, len(high), 16)) + b"\n")
    _open_matches_source(w, tmp_path / "lane")


def test_verify_hashes_the_stored_range_bytes(tmp_path: Path) -> None:
    w = _pinned(tmp_path, "latin1.txt", b"caf\xe9\n")
    rows = grip.verify_review_commit(w["ws"], w["commit"])["rows"]
    assert rows[0]["range_sha256"] == hashlib.sha256(w["patch"]).hexdigest()


def test_a_utf8_range_opens_and_verifies_as_before(tmp_path: Path) -> None:
    w = _pinned(tmp_path, "plain.txt", "café\n".encode(), utf8=True)
    _open_matches_source(w, tmp_path / "lane")
    rows = grip.verify_review_commit(w["ws"], w["commit"])["rows"]
    assert rows[0]["range_sha256"] == hashlib.sha256(w["patch"]).hexdigest()


def test_pin_then_open_through_the_cli_rebuilds_a_latin1_hunk(tmp_path: Path) -> None:
    w = _pinned(tmp_path, "latin1.txt", b"caf\xe9\n")  # pinned through `review pin` on the CLI
    lane = tmp_path / "lane"
    res = CliRunner().invoke(gr2_app.app, [
        "review", "open", str(w["ws"]), "gr:" + w["commit"], "--lane-dir", str(lane)])
    assert res.exit_code == 0, res.output
    assert "tree_match: True" in res.output.splitlines(), res.output
    got = _git(lane, "hash-object", "--", w["name"]).decode().strip()
    assert got == _git(w["work"], "hash-object", "--", w["name"]).decode().strip()
