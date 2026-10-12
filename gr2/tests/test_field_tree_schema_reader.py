"""Field trees under a caller's schema, and the readers that fetch a stored tree's
objects. Each test names the rule it holds."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import review_field_tree as fd


def git(repo: Path, *args: str, data: bytes | None = None) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], input=data, capture_output=True,
                          check=True).stdout.decode().strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "store.git"
    subprocess.run(["git", "init", "-q", "--bare", str(r)], check=True)
    return r


NOTE_PROTO = """syntax = "proto3";
package example.notes.v1;

message Note {
  string title = 1;
  repeated Line lines = 2;
  bytes attachment = 3;
  Line footer = 4;
}

message Line {
  string text = 1;
  string author = 2;
}
"""

NOTE = {"title": "a note", "attachment": b"\x00\xffraw",
        "lines": [{"text": "first", "author": "ann"}, {"text": "second"}, {"text": "third", "author": "bo"}],
        "footer": {"text": "end"}}


def member(key: str, **extra) -> dict:
    m = {"key": key, "path": key, "remote": f"https://example.invalid/{key}.git",
         "base": "1" * 40, "commit": "2" * 40, "remote_head": "1" * 40,
         "title": f"title {key}", "body": f"body {key}"}
    m.update(extra)
    return m


RECORD = {"schema": "grip-review-bind-v2", "kind": "review", "policy": "clean",
          "members": [member("beta", range_patch=b"From 2222 patch\n"), member("alpha"), member("gamma")]}


def test_a_caller_schema_round_trips_through_the_writer(repo: Path) -> None:
    """Rule: the writer, reader and verify take the caller's parsed schema; names come
    from it, and the record reads back equal by number."""
    notes = fd.parse_proto(NOTE_PROTO)
    tree = fd.write_record(repo, NOTE, "Note", notes)
    top = [line.split("\t")[1] for line in git(repo, "ls-tree", tree).splitlines()]
    assert top == ["001.2_title", "002.2r_lines", "003.2_attachment", "004.2_footer"]
    assert fd.read_record(repo, tree, "Note", notes) == NOTE
    fd.verify_tree(repo, tree, "Note", notes)
    assert fd.to_protobuf(repo, tree) == fd.encode(NOTE, "Note", notes)


def _trees(repo: Path, root: str) -> list[str]:
    return [root] + [line.split()[2] for line in git(repo, "ls-tree", "-r", "-t", root).splitlines()
                     if line.split()[1] == "tree"]


def test_the_batch_reader_returns_what_the_default_reader_returns(repo: Path) -> None:
    """Rule: a reader changes how objects are fetched, never what is read."""
    tree = fd.write_record(repo, RECORD)
    unknown = git(repo, "hash-object", "-w", "--stdin", data=b"from a newer writer")
    entries = fd._ls(repo, tree) + [("100644", "blob", unknown, "099.2_future")]
    widened = fd._mktree(repo, entries)
    for root in (tree, widened):
        plain = fd.GitReader(repo)
        with fd.BatchReader(repo) as batch:
            for t in _trees(repo, root):
                assert batch.ls(t) == plain.ls(t)
            blobs = [line.split()[2] for line in git(repo, "ls-tree", "-r", root).splitlines()]
            assert [batch.blob(b) for b in blobs] == [plain.blob(b) for b in blobs]
            assert fd.to_protobuf(repo, root, batch) == fd.to_protobuf(repo, root)
            assert fd.read_record(repo, root, reader=batch) == fd.read_record(repo, root)
            fd.verify_tree(repo, root, reader=batch)


@pytest.fixture
def git_spawns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Count every git process started, through a git on PATH that logs and hands on."""
    real = shutil.which("git")
    bindir, log = tmp_path / "bin", tmp_path / "spawns.log"
    bindir.mkdir()
    shim = bindir / "git"
    shim.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nexec "{real}" "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{Path(real).parent}")

    def count() -> int:
        n = len(log.read_text().splitlines()) if log.exists() else 0
        log.write_text("")
        return n
    return count


def test_the_batch_reader_starts_two_git_processes_per_read(repo: Path, git_spawns) -> None:
    """Rule: one read through the batch reader costs two git processes, however many
    objects the record holds; the default costs one per object."""
    tree = fd.write_record(repo, RECORD)
    objects = len(git(repo, "ls-tree", "-r", "-t", tree).splitlines()) + 1
    git_spawns()
    fd.read_record(repo, tree)
    assert git_spawns() == objects
    with fd.BatchReader(repo) as batch:
        fd.read_record(repo, tree, reader=batch)
    assert git_spawns() == 2


def test_a_refused_object_leaves_the_batch_reader_serving_the_next_read(repo: Path) -> None:
    """Rule: the readers differ only in how many git processes they start. A refusal on one object (a tree, or
    a gitlink entry naming a commit that is present) must leave the batch reader reading the next one
    correctly, as the default reader does."""
    tree = fd.write_record(repo, RECORD)
    blob = git(repo, "hash-object", "-w", "--stdin", data=b"world\n")
    with fd.BatchReader(repo) as batch:
        with pytest.raises(fd.ReviewRecordError):
            batch.blob(tree)
        assert batch.blob(blob) == b"world\n"
    commit = git(repo, "commit-tree", "-m", "present", tree)
    bad = fd._mktree(repo, [("160000", "commit", commit, "001.2")])
    with fd.BatchReader(repo) as batch:
        with pytest.raises(fd.ReviewRecordError):
            fd.to_protobuf(repo, bad, batch)
        assert fd.to_protobuf(repo, tree, batch) == fd.to_protobuf(repo, tree)
