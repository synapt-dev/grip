"""Field trees under a caller's schema, and the readers that fetch a stored tree's
objects. Each test names the rule it holds."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import threading
import time
import zlib
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


def _lying_blob(repo: Path, size: int = 2, body: bytes = b"abc\n") -> str:
    """A loose blob stored as ``body`` under its own id, but with a header claiming ``size`` bytes:
    git cat-file --batch sends the header as stored, then the whole body and the newline."""
    oid = git(repo, "hash-object", "-w", "--stdin", data=body)
    path = repo / "objects" / oid[:2] / oid[2:]
    path.chmod(0o644)
    path.write_bytes(zlib.compress(b"blob %d\0" % size + body))
    return oid


def _answer(batch: fd.BatchReader, oid: str) -> bytes | None:
    try:
        return batch.blob(oid)
    except fd.ReviewRecordError:
        return None


def test_a_header_that_understates_the_body_is_refused(repo: Path) -> None:
    """Rule: the byte after a blob's body must be the newline that ends it; a header that understates the
    body is refused as truncated, not answered with the bytes it claims."""
    lying = _lying_blob(repo)
    with fd.BatchReader(repo) as batch:
        with pytest.raises(fd.ReviewRecordError, match="truncated"):
            batch.blob(lying)


def _object_id(body: bytes, like: str) -> str:
    """The blob id of ``body``, in the hash of the id it is checked against (40 hex: sha1, 64: sha256)."""
    algo = hashlib.sha1 if len(like) == 40 else hashlib.sha256
    return algo(b"blob %d\0" % len(body) + body).hexdigest()


def test_no_request_after_a_lying_header_is_answered_with_another_objects_bytes(repo: Path) -> None:
    """Rule: after a header that understates its body, every later request on that reader, and on a new
    one, is answered with its own object's bytes: each answer re-hashes to the id that was asked, and a
    refusal is the only other outcome."""
    lying = _lying_blob(repo)
    zz = git(repo, "hash-object", "-w", "--stdin", data=b"zz\n")
    qq = git(repo, "hash-object", "-w", "--stdin", data=b"qq\n")

    with fd.BatchReader(repo) as batch:
        answers = [(oid, _answer(batch, oid)) for oid in (lying, zz, zz, qq)]
    with fd.BatchReader(repo) as batch:
        answers.append((qq, _answer(batch, qq)))
    wrong = [(oid, body, _object_id(body, oid)) for oid, body in answers
             if body is not None and _object_id(body, oid) != oid]
    assert wrong == [], wrong
    assert [body for _, body in answers[1:]] == [b"zz\n", b"zz\n", b"qq\n", b"qq\n"], answers


def test_a_header_one_short_of_a_body_ending_in_two_newlines_is_refused(repo: Path) -> None:
    """Rule: a body that happens to hold a newline where the header says it ends is still refused, and
    the newline git sends after it is not read as the next answer's header."""
    lying = _lying_blob(repo, 3, b"ab\n\n")
    zz = git(repo, "hash-object", "-w", "--stdin", data=b"zz\n")
    qq = git(repo, "hash-object", "-w", "--stdin", data=b"qq\n")
    with fd.BatchReader(repo) as batch:
        assert [_answer(batch, oid) for oid in (lying, zz, qq, qq)] == [None, b"zz\n", b"qq\n", b"qq\n"]


def test_a_writer_of_one_object_cannot_choose_a_later_answer(repo: Path) -> None:
    """Rule: a body that carries a forged answer for another object after the size its header claims is
    refused, and the forged bytes never answer that object."""
    zz = git(repo, "hash-object", "-w", "--stdin", data=b"zz\n")
    lying = _lying_blob(repo, 1, b"x\n%s blob 3\nEVL\n" % zz.encode())
    with fd.BatchReader(repo) as batch:
        assert [_answer(batch, oid) for oid in (lying, zz, zz)] == [None, b"zz\n", b"zz\n"]


def test_a_refusal_ends_a_process_blocked_writing_a_long_body(repo: Path) -> None:
    """Rule: a refusal kills the batch process before closing it; git blocked writing the rest of an
    overlong body into a full pipe never exits on its own. The read runs on a thread, so a hang fails
    the row instead of the run."""
    lying = _lying_blob(repo, 2, b"a" * (4 << 20))
    one = git(repo, "hash-object", "-w", "--stdin", data=b"x")
    batch = fd.BatchReader(repo)
    got: list = []
    t = threading.Thread(target=lambda: got.append(_answer(batch, lying)), daemon=True)
    t.start()
    t.join(15)
    try:
        assert not t.is_alive(), "the refusal hung waiting on the batch process"
    finally:
        if t.is_alive() and batch._cat is not None:
            batch._cat.kill()
            t.join(5)
    assert got == [None]
    assert batch.blob(one) == b"x"  # a new process serves the next read
    batch.close()


def test_a_one_byte_blob_and_an_empty_blob_still_read_through_the_batch_reader(repo: Path) -> None:
    """Rule: the newline check holds for the shortest bodies; a 1-byte blob and an empty blob read as stored."""
    one = git(repo, "hash-object", "-w", "--stdin", data=b"x")
    empty = git(repo, "hash-object", "-w", "--stdin", data=b"")
    nl = git(repo, "hash-object", "-w", "--stdin", data=b"\n")
    with fd.BatchReader(repo) as batch:
        assert [batch.blob(one), batch.blob(empty), batch.blob(nl), batch.blob(one)] == [b"x", b"", b"\n", b"x"]


# A stand-in for cat-file --batch that sends one fixed answer (hex in argv) for every request and stays
# alive until its stdin closes or it is killed.
_ANSWER = """
import sys
answer = bytes.fromhex(sys.argv[1])
for _ in sys.stdin.buffer:
    sys.stdout.buffer.write(answer)
    sys.stdout.buffer.flush()
"""


def _answering(monkeypatch, answer: bytes) -> list[subprocess.Popen]:
    started: list[subprocess.Popen] = []

    def start(cwd, *args, **kwargs):
        started.append(subprocess.Popen([sys.executable, "-c", _ANSWER, answer.hex()],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE))
        return started[-1]

    monkeypatch.setattr(fd.gitops, "start", start)
    return started


def test_an_abbreviated_id_is_refused_before_any_git_process_starts(repo: Path, git_spawns) -> None:
    """Rule: blob() is only ever asked by an id ls-tree named (full hex), so the header can be compared byte
    for byte; an abbreviated id is refused at entry, and the full id still reads."""
    oid = git(repo, "hash-object", "-w", "--stdin", data=b"abc\n")
    git_spawns()
    with fd.BatchReader(repo) as batch:
        with pytest.raises(fd.ReviewRecordError, match="not a full object id"):
            batch.blob(oid[:7])
        assert batch._cat is None and git_spawns() == 0
        assert batch.blob(oid) == b"abc\n"


def test_a_header_naming_another_object_is_refused_before_its_body_is_read(repo: Path, monkeypatch) -> None:
    """Rule: an answer whose header names another object is refused on the header alone, and the process is
    killed; the reader does not wait for a body. The stand-in never sends one, so a reader that waits runs
    into the deadline instead."""
    asked = git(repo, "hash-object", "-w", "--stdin", data=b"abc\n")
    other = git(repo, "hash-object", "-w", "--stdin", data=b"zz\n")
    monkeypatch.setattr(fd.BatchReader, "DEADLINE", 2.0)
    started = _answering(monkeypatch, other.encode() + b" blob 1048576\n")
    with fd.BatchReader(repo) as batch:
        t0 = time.monotonic()
        with pytest.raises(fd.ReviewRecordError, match="answered for another object"):
            batch.blob(asked)
        assert time.monotonic() - t0 < 1.0
        assert batch._cat is None and started[0].returncode is not None


def test_another_kind_whose_body_does_not_end_in_a_newline_is_refused_and_abandoned(repo: Path, monkeypatch) -> None:
    """Rule: only a blob's body can be checked against the id asked, so an object of another kind is refused
    on its header and the process is killed and closed, its body left unread."""
    asked = git(repo, "hash-object", "-w", "--stdin", data=b"abc\n")
    started = _answering(monkeypatch, asked.encode() + b" tree 2\nabX")
    with fd.BatchReader(repo) as batch:
        with pytest.raises(fd.ReviewRecordError, match="out of step"):
            batch.blob(asked)
        assert batch._cat is None and started[0].returncode is not None


def test_missing_for_another_object_is_refused_and_abandoned(repo: Path, monkeypatch) -> None:
    """Rule: a "missing" answer counts only for the object asked; one naming another object means the stream
    is out of step, so it is refused as such and the process is killed and closed."""
    asked = git(repo, "hash-object", "-w", "--stdin", data=b"abc\n")
    other = git(repo, "hash-object", "-w", "--stdin", data=b"zz\n")
    started = _answering(monkeypatch, other.encode() + b" missing\n")
    with fd.BatchReader(repo) as batch:
        with pytest.raises(fd.ReviewRecordError, match="answered for another object"):
            batch.blob(asked)
        assert batch._cat is None and started[0].returncode is not None


@pytest.mark.parametrize("kind,size,refusal", [(b"blob", b"abc", "unreadable size"), (b"tree", b"abc", "out of step"),
                                               (b"blob", "٣".encode(), "unreadable size"),
                                               (b"blob", b"1_0", "unreadable size"), (b"tree", b"1_0", "out of step")])
def test_a_size_that_is_not_ascii_digits_is_refused_and_abandoned(repo: Path, monkeypatch, kind, size, refusal) -> None:
    """Rule: a header naming the asked id is git's only when its size is ASCII digits; any other size (letters,
    a non-ASCII digit, an underscore that int() would accept) is refused and the process is killed, never
    surfacing as a ValueError. Another kind is refused on its kind before its size is looked at."""
    asked = git(repo, "hash-object", "-w", "--stdin", data=b"abc\n")
    monkeypatch.setattr(fd.BatchReader, "DEADLINE", 2.0)
    started = _answering(monkeypatch, asked.encode() + b" " + kind + b" " + size + b"\n" + b"x" * 64 + b"\n")
    with fd.BatchReader(repo) as batch:
        with pytest.raises(fd.ReviewRecordError, match=refusal):
            batch.blob(asked)
        assert batch._cat is None and started[0].returncode is not None


def test_an_id_carrying_a_newline_is_refused_before_any_git_process_starts(repo: Path, git_spawns) -> None:
    """Rule: an id with a newline in it would be two requests on one stream; it is refused at entry with no
    git process, so no "missing" line for its tail is ever left behind for the next read."""
    oid = git(repo, "hash-object", "-w", "--stdin", data=b"abc\n")
    git_spawns()
    with fd.BatchReader(repo) as batch:
        with pytest.raises(fd.ReviewRecordError, match="not a full object id"):
            batch.blob(oid + "\n" + "0" * 40)
        assert batch._cat is None and git_spawns() == 0
        assert batch.blob(oid) == b"abc\n"


def test_two_fields_naming_the_asked_id_but_not_missing_are_refused_and_abandoned(repo: Path, monkeypatch) -> None:
    """Rule: of the two-field answers only git's "<id> missing" for the id asked keeps the process; any other
    second word under the right id is a stream out of step, refused and killed."""
    asked = git(repo, "hash-object", "-w", "--stdin", data=b"abc\n")
    started = _answering(monkeypatch, asked.encode() + b" bogus\n")
    with fd.BatchReader(repo) as batch:
        with pytest.raises(fd.ReviewRecordError, match="not a blob"):
            batch.blob(asked)
        assert batch._cat is None and started[0].returncode is not None


# A stand-in that answers its first request with a "tree 2" header over a body of ``ab\n`` and 4 MiB more,
# then waits on stdin: the byte where the header says the body ends happens to be a newline.
_LONG_TREE = """
import os, sys
sys.stdin.readline()
os.write(1, sys.argv[1].encode() + b" tree 2\\nab\\n" + b"a" * (4 << 20) + b"\\n")
sys.stdin.read()
"""


def test_another_kind_is_not_drained_even_when_its_cut_lands_on_a_newline(repo: Path, monkeypatch) -> None:
    """Rule: another kind's body is never read off the stream, so no byte of it can make the reader trust the
    stream: the process is killed on the header, and close() returns instead of waiting on a writer blocked
    on the full pipe."""
    asked = git(repo, "hash-object", "-w", "--stdin", data=b"abc\n")
    started: list[subprocess.Popen] = []

    def start(cwd, *args, **kwargs):
        started.append(subprocess.Popen([sys.executable, "-c", _LONG_TREE, asked],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE))
        return started[-1]

    monkeypatch.setattr(fd.gitops, "start", start)
    got: list[object] = []

    def read() -> None:
        batch = fd.BatchReader(repo)
        got.append(_answer(batch, asked))
        got.append(batch._cat is None)
        batch.close()
        got.append("closed")

    t = threading.Thread(target=read, daemon=True)
    t.start()
    t.join(8)
    hung = t.is_alive()
    started[0].kill()
    t.join(5)
    assert not hung and got == [None, True, "closed"]
