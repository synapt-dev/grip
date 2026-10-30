"""`review pin` reads a range as the bytes git wrote. A hunk that is not UTF-8 (a .pb
descriptor, a Latin-1 text file) pins, and the stored range.patch blob is byte for
byte the patch: its blob id equals `git hash-object` of the same bytes. Both doors
(--from-range, a rows-json `range`, and --source) carry it; a range path that cannot
be read is refused by name with exit 2, never a traceback."""
from __future__ import annotations

from tests.native_root_helper import native_root

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from gr2.python_cli import app as gr2_app
from gr2.python_cli import grip


def _git(cwd: Path, *a: str, input: bytes | None = None) -> bytes:
    return subprocess.run(["git", *a], cwd=cwd, input=input, capture_output=True, check=True).stdout


def _world(tmp_path: Path, name: str, content: bytes) -> dict:
    """A bare origin at BASE and a work clone whose head adds `name` holding `content`."""
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
    _git(work, "commit", "-q", "-m", "add " + name)
    head = _git(work, "rev-parse", "HEAD").decode().strip()
    patch = _git(work, "format-patch", "--binary", f"{base}..{head}", "--stdout")
    # The subject before the instrument: the hunk really carries the non-UTF-8 byte.
    assert content in patch
    try:
        patch.decode("utf-8")
    except UnicodeDecodeError:
        pass
    else:
        raise AssertionError("fixture range is valid UTF-8; it witnesses nothing")
    range_file = tmp_path / "range.patch"
    range_file.write_bytes(patch)
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    native_root(ws)
    return {"ws": ws, "remote": str(origin), "work": work, "base": base, "head": head,
            "patch": patch, "range_file": range_file}


def _row_args(w: dict) -> list[str]:
    return ["review", "pin", str(w["ws"]), "--repo", "alpha", "--remote", w["remote"],
            "--base", w["base"], "--head", w["head"], "--ref", "refs/heads/trunk"]


def _stored_range(w: dict, res) -> bytes:
    assert res.exit_code == 0, res.output
    out = res.output.strip()
    assert out.startswith("gr:"), out
    bind = grip._bind_dir(w["ws"])
    return _git(bind, "cat-file", "blob", f"{out[len('gr:'):]}:004.2r_members/0000001/008.2_range_patch")


def _assert_byte_for_byte(w: dict, stored: bytes) -> None:
    assert stored == w["patch"]
    want = _git(w["work"], "hash-object", "--stdin", input=w["patch"]).decode().strip()
    got = _git(w["work"], "hash-object", "--stdin", input=stored).decode().strip()
    assert got == want


def test_from_range_binary_hunk_pins_byte_for_byte(tmp_path: Path) -> None:
    w = _world(tmp_path, "descriptor.pb", b"\x86\x01pkg\n")
    res = CliRunner().invoke(gr2_app.app, [*_row_args(w), "--from-range", str(w["range_file"])])
    _assert_byte_for_byte(w, _stored_range(w, res))


def test_from_range_latin1_hunk_pins_byte_for_byte(tmp_path: Path) -> None:
    w = _world(tmp_path, "latin1.txt", b"caf\xe9\n")
    res = CliRunner().invoke(gr2_app.app, [*_row_args(w), "--from-range", str(w["range_file"])])
    _assert_byte_for_byte(w, _stored_range(w, res))


def test_rows_json_range_latin1_hunk_pins_byte_for_byte(tmp_path: Path) -> None:
    w = _world(tmp_path, "latin1.txt", b"caf\xe9\n")
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps([{"key": "alpha", "remote": w["remote"], "base": w["base"], "head": w["head"],
                                 "ref": "refs/heads/trunk", "range": str(w["range_file"])}]))
    res = CliRunner().invoke(gr2_app.app, ["review", "pin", str(w["ws"]), "--rows-json", str(rows)])
    _assert_byte_for_byte(w, _stored_range(w, res))


def test_from_range_that_cannot_be_read_is_refused_by_name(tmp_path: Path) -> None:
    w = _world(tmp_path, "latin1.txt", b"caf\xe9\n")
    res = CliRunner().invoke(gr2_app.app, [*_row_args(w), "--from-range", str(tmp_path)])
    assert res.exit_code == 2, res.output
    assert "range_unreadable" in res.output
    assert "Traceback" not in res.output
    assert not isinstance(res.exception, (OSError, UnicodeError)), res.exception


def test_source_latin1_hunk_pins_byte_for_byte(tmp_path: Path) -> None:
    w = _world(tmp_path, "latin1.txt", b"caf\xe9\n")
    # --source captures with plain format-patch (no --binary); the text hunk is the same bytes either way.
    w["patch"] = _git(w["work"], "format-patch", f"{w['base']}..{w['head']}", "--stdout")
    res = CliRunner().invoke(gr2_app.app, [*_row_args(w), "--source", str(w["work"])])
    _assert_byte_for_byte(w, _stored_range(w, res))
