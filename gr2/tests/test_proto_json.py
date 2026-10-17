"""gr2 proto json: protobuf records as JSON, by number without a schema, and as a
git textconv so git diff shows JSON."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from gr2.python_cli import app as gr2_app
from gr2.python_cli import proto_json
from gr2.python_cli import review_field_tree as fd
from tests.conftest import make_cli_runner

RECORD = {"schema": "grip-review-bind-v2", "kind": "review", "policy": "clean",
          "members": [{"key": "alpha", "range_patch": b"\xff\x00patch"}, {"key": "beta"}]}


def git(repo: Path, *args: str, data: bytes | None = None) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], input=data, capture_output=True,
                          check=True).stdout.decode().strip()


def cli(*args: str):
    return make_cli_runner().invoke(gr2_app.app, ["proto", "json", *args])


def test_by_number_without_a_schema_and_nesting_never_guessed():
    out = proto_json.render(fd.encode(RECORD))
    assert out["1"] == "grip-review-bind-v2" and out["3"] == "clean"
    # field 4 occurs twice: a list; each member is a length-delimited value, never unpacked
    assert isinstance(out["4"], list) and len(out["4"]) == 2
    assert all(isinstance(v, (str, dict)) for v in out["4"])


def test_non_utf8_bytes_print_as_base64():
    buf = fd._emit(9, 2, b"\xff\xfe")
    assert proto_json.render(buf) == {"9": {"bytes_b64": "//4="}}


def test_varint_and_fixed_values_print_as_unsigned_integers():
    buf = fd._emit(1, 0, fd._varint(300)) + fd._emit(2, 5, (7).to_bytes(4, "little"))
    assert proto_json.render(buf) == {"1": 300, "2": 7}


def test_message_names_fields_and_nests_and_keeps_unknown_numbers():
    buf = fd.encode(RECORD) + fd._emit(77, 2, b"later")
    out = proto_json.render(buf, "ReviewBind")
    assert out["kind"] == "review"
    assert [m["key"] for m in out["members"]] == ["alpha", "beta"]
    assert out["members"][0]["range_patch"] == {"bytes_b64": "/wBwYXRjaA=="}
    assert out["77"] == "later"


def test_cli_reads_a_file_a_blob_and_a_field_tree(tmp_path: Path):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    buf = fd.encode(RECORD)
    f = tmp_path / "bind.pb"
    f.write_bytes(buf)
    blob = git(repo, "hash-object", "-w", "--stdin", data=buf)
    tree = fd.write_tree(repo, buf)
    want = proto_json.dumps(proto_json.render(buf, "ReviewBind"))
    for target in (str(f), blob, tree):
        r = cli(target, "--message", "ReviewBind", "--repo-path", str(repo))
        assert r.exit_code == 0, r.output
        assert r.stdout == want


def test_cli_refuses_what_is_neither_a_file_nor_an_object(tmp_path: Path):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    r = cli("no-such-thing", "--repo-path", str(repo))
    assert r.exit_code == 1 and r.stdout == ""


def test_cli_refuses_bytes_that_are_not_protobuf(tmp_path: Path):
    f = tmp_path / "x.pb"
    f.write_bytes(b"\x0f")  # wire type 7
    r = cli(str(f))
    assert r.exit_code == 1 and r.stdout == ""


def test_git_diff_shows_json_through_the_textconv(tmp_path: Path):
    """End to end: .gitattributes + diff.protoblob.textconv; git diff names the changed field."""
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    shim = tmp_path / "gr2-proto-json"
    shim.write_text(f"#!/bin/sh\nexec {sys.executable} -c "
                    "'import sys; from gr2.python_cli.app import app; sys.argv=[\"gr2\",\"proto\",\"json\",*sys.argv[1:]]; app()' \"$@\"\n")
    shim.chmod(0o755)
    (repo / ".gitattributes").write_text("*.pb diff=protoblob\n")
    git(repo, "config", "diff.protoblob.textconv", f"{shim} --message ReviewBind --textconv")
    (repo / "bind.pb").write_bytes(fd.encode(RECORD))
    git(repo, "add", ".")
    git(repo, "-c", "user.name=T", "-c", "user.email=t@x.invalid", "commit", "-qm", "one")
    (repo / "bind.pb").write_bytes(fd.encode({**RECORD, "policy": "dirty"}))
    diff = git(repo, "diff")
    assert '-  "policy": "clean",' in diff and '+  "policy": "dirty",' in diff, diff
    # control: without the attribute, git calls the same change binary
    (repo / ".gitattributes").write_text("")
    assert "Binary files" in git(repo, "diff", "--", "bind.pb")


def test_a_bad_file_under_textconv_prints_a_marker_and_the_rest_of_the_diff_survives(tmp_path: Path):
    """One file that is not protobuf must not stop git diff: its textconv prints a marker and
    exits 0, and the good file's change still shows. Without --textconv the same file exits 1."""
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    shim = tmp_path / "gr2-proto-json"
    shim.write_text(f"#!/bin/sh\nexec {sys.executable} -c "
                    "'import sys; from gr2.python_cli.app import app; sys.argv=[\"gr2\",\"proto\",\"json\",*sys.argv[1:]]; app()' \"$@\"\n")
    shim.chmod(0o755)
    (repo / ".gitattributes").write_text("*.pb diff=protoblob\n")
    git(repo, "config", "diff.protoblob.textconv", f"{shim} --message ReviewBind --textconv")
    (repo / "good.pb").write_bytes(fd.encode(RECORD))
    (repo / "bad.pb").write_bytes(b"\x0f")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=T", "-c", "user.email=t@x.invalid", "commit", "-qm", "one")
    (repo / "good.pb").write_bytes(fd.encode({**RECORD, "policy": "dirty"}))
    (repo / "bad.pb").write_bytes(b"\x0f\x0f")
    diff = git(repo, "diff")
    assert '+  "policy": "dirty",' in diff, diff
    assert "+# gr2 proto json: not rendered" in diff and "-# gr2 proto json: not rendered" in diff, diff
    # control: the same bad bytes without --textconv are refused
    f = tmp_path / "x.pb"
    f.write_bytes(b"\x0f")
    assert cli(str(f)).exit_code == 1


def test_field_number_zero_is_refused():
    buf = fd._tag(0, 0) + fd._varint(1)
    with pytest.raises(fd.ReviewRecordError, match="field number 0"):
        fd.fields(buf)


def test_an_unknown_message_is_a_named_refusal(tmp_path: Path):
    f = tmp_path / "b.pb"
    f.write_bytes(fd.encode(RECORD))
    r = cli(str(f), "--message", "NoSuchMessage")
    assert r.exit_code == 1 and r.stdout == ""
    assert "no message 'NoSuchMessage'" in r.stderr
