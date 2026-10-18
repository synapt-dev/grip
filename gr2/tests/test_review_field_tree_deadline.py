"""Every read from the batch reader's cat-file process runs under a deadline. A loose object
whose header claims more bytes than its body holds keeps ``cat-file --batch`` alive after a
short answer, so an unbounded read waits forever; the reader refuses it as truncated instead.
The repo has no pytest-timeout, so the row guards itself: the read runs on a thread, and a
read still running after the guard fails the row and kills what it started."""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path

import pytest

from gr2.python_cli import gitops
from gr2.python_cli import review_field_tree as fd
from tests.test_field_tree_schema_reader import _ANSWER
from tests.test_review_transport import git

GUARD = 15.0


def _short_body_blob(repo: Path) -> str:
    """A loose object whose header says ``blob 9`` over a 4-byte body. One-shot ``cat-file blob``
    reads it; ``cat-file --batch`` sends the header and 5 bytes and stays alive."""
    oid = git(repo, "hash-object", "-w", "--stdin", input="abc\n")
    loose = repo / git(repo, "rev-parse", "--git-path", f"objects/{oid[:2]}/{oid[2:]}")
    loose.chmod(0o644)
    loose.write_bytes(zlib.compress(b"blob 9\0abc\n"))
    return oid


def test_batch_reader_refuses_a_short_body_within_the_deadline(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    git(tmp_path, "init", "-q", str(repo))
    oid = _short_body_blob(repo)
    tree = git(repo, "mktree", input=f"100644 blob {oid}\t001.2_short\n")
    assert fd.GitReader(repo).blob(oid) == b"abc\n"  # the one-shot read takes it: only the batch stalls

    started = []
    real_start = gitops.start
    monkeypatch.setattr(gitops, "start", lambda *a, **k: started.append(real_start(*a, **k)) or started[-1])
    monkeypatch.setattr(fd.BatchReader, "DEADLINE", 1.0, raising=False)
    got: list = []

    def read() -> None:
        try:
            with fd.BatchReader(repo) as reader:
                got.append(fd.to_protobuf(repo, tree, reader))
        except BaseException as exc:
            got.append(exc)

    t = threading.Thread(target=read, daemon=True)
    t.start()
    t.join(GUARD)
    try:
        assert not t.is_alive(), f"the batch read hung past {GUARD:g}s"
    finally:
        for proc in started:
            proc.kill()
        t.join(5)
    assert isinstance(got[0], fd.ReviewRecordError), got[0]
    assert "truncated" in str(got[0])
    assert all(proc.poll() is not None for proc in started)  # the stalled process is gone


class _Feed:
    """A stand-in batch process whose stdout is a pipe fed on a thread: ``chunks`` with ``gap``
    seconds between them, and the pipe held open after them until the kill."""

    def __init__(self, chunks: list[bytes], gap: float):
        r, self._w = os.pipe()
        self.stdout, self.stdin, self.dead = os.fdopen(r, "rb"), open(os.devnull, "wb"), False

        def feed() -> None:
            try:
                for chunk in chunks:
                    if self.dead:
                        return
                    os.write(self._w, chunk)
                    time.sleep(gap)
            except OSError:
                pass

        threading.Thread(target=feed, daemon=True).start()

    def kill(self) -> None:
        self.dead = True
        try:
            os.close(self._w)
        except OSError:
            pass

    def wait(self) -> int:
        return 0


def _guarded_blob(monkeypatch, feed: _Feed, oid: str = "1" * 40) -> tuple[object, float]:
    """The blob read under a 1 s deadline, on a thread the row guards: a read still running after
    GUARD fails the row, and the kill ends it."""
    monkeypatch.setattr(fd.BatchReader, "DEADLINE", 1.0, raising=False)
    reader = fd.BatchReader(Path("."))
    reader._cat = feed
    got: list = []

    def read() -> None:
        try:
            got.append(reader.blob(oid))
        except BaseException as exc:
            got.append(exc)

    start = time.monotonic()
    t = threading.Thread(target=read, daemon=True)
    t.start()
    t.join(GUARD)
    try:
        assert not t.is_alive(), f"the batch read hung past {GUARD:g}s"
    finally:
        feed.kill()
        t.join(5)
    return got[0], time.monotonic() - start


def test_batch_reader_takes_a_slow_live_blob_past_the_deadline(monkeypatch):
    """The deadline bounds silence, not size: a 3 MiB blob that arrives 64 KiB every 0.05 s takes
    about 2.4 s, longer than the deadline, and is never silent for more than 0.05 s."""
    size = 3 << 20
    body = [b"\0" * 65536] * (size // 65536) + [b"\n"]
    oid = hashlib.sha1(b"blob %d\0" % size + b"\0" * size).hexdigest()
    got, took = _guarded_blob(monkeypatch, _Feed([b"%s blob %d\n" % (oid.encode(), size), *body], 0.05), oid)
    assert got == b"\0" * size, got
    assert took > 1.0  # it outlasted the deadline


def test_batch_reader_refuses_a_stalled_header_within_the_deadline(monkeypatch):
    """A process that never sends the header is refused at the deadline, by the silent interval."""
    got, took = _guarded_blob(monkeypatch, _Feed([], 0))
    assert isinstance(got, fd.ReviewRecordError), got
    assert "sent nothing for 1s" in str(got)
    assert took < 2.5


def _threads() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if t is not threading.main_thread()}


def test_batch_reader_refuses_within_the_deadline_while_a_child_holds_the_pipe(tmp_path, monkeypatch):
    """The batch process's child keeps stdout open after the kill: the refusal must not wait on it."""
    repo = tmp_path / "r"
    git(tmp_path, "init", "-q", str(repo))
    oid = _short_body_blob(repo)
    pid = tmp_path / "child.pid"
    stand_in = f"read l; printf '{oid} blob 9\\nabc\\n\\n'; sleep 20 & echo $! > {pid}; wait"
    monkeypatch.setattr(gitops, "start", lambda *a, **k: subprocess.Popen(
        ["sh", "-c", stand_in], stdin=subprocess.PIPE, stdout=subprocess.PIPE))
    monkeypatch.setattr(fd.BatchReader, "DEADLINE", 1.0, raising=False)
    before = _threads()
    start = time.monotonic()
    try:
        with pytest.raises(fd.ReviewRecordError, match="truncated"):
            with fd.BatchReader(repo) as reader:
                reader.blob(oid)
        took = time.monotonic() - start
        assert took < 5.0, f"the refusal took {took:.1f}s: it waited on the child, not the deadline"
    finally:
        with contextlib.suppress(ProcessLookupError):  # a regression waits the child out; let the timing report it
            os.kill(int(pid.read_text()), signal.SIGKILL)
    for t in _threads() - before:  # the read ends with the pipe
        t.join(5)
    assert not _threads() - before


@pytest.mark.parametrize("name, partialclone", [("origin", None), ("up", None), ("-x", "-x"), ("origin", "up")])
def test_batch_reader_refuses_a_partial_clone_absent_blob_without_fetching(tmp_path, monkeypatch, name, partialclone):
    """An absent blob in a partial clone is missing, at once: the read never reaches the remote. The refusal names
    the clone's own promisor remote, and the fetch it prints runs as printed."""
    if tuple(int(n) for n in re.findall(r"\d+", git(tmp_path, "version"))[:2]) < (2, 44):
        pytest.skip("git before 2.44 ignores GIT_NO_LAZY_FETCH")
    remote = tmp_path / "remote"
    git(tmp_path, "init", "-q", str(remote))
    git(remote, "config", "uploadpack.allowFilter", "true")
    (remote / "f").write_text("far\n")
    git(remote, "add", "f")
    git(remote, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "f")
    oid = git(remote, "rev-parse", "HEAD:f")
    clone = tmp_path / "my clone"  # a space in the path: the printed fetch must still run as printed
    git(tmp_path, "clone", "-q", "-o", name, "--no-checkout", "--filter=blob:none", remote.as_uri(), str(clone))
    if partialclone == name:  # the extension alone names the promisor, and an option-shaped name stays a name
        git(clone, "config", "extensions.partialclone", partialclone)
        git(clone, "config", "--unset", f"remote.{name}.promisor")
    elif partialclone:  # the extension names one remote, remote.<name>.promisor another: the extension wins
        git(clone, "remote", "add", partialclone, remote.as_uri())
        git(clone, "config", "extensions.partialclone", partialclone)
    remotes = {name, partialclone or name}
    # a fetch from the remote runs this upload-pack, which leaves a mark and then stalls
    mark = tmp_path / "fetched"
    pack = tmp_path / "upload-pack"
    pack.write_text(f"#!/bin/sh\ntouch {mark}\nsleep 3\nexec git-upload-pack \"$@\"\n")
    pack.chmod(0o755)
    for each in remotes:
        git(clone, "config", f"remote.{each}.uploadpack", str(pack))
    remote.rename(tmp_path / "gone")
    monkeypatch.setattr(fd.BatchReader, "DEADLINE", 2.0, raising=False)
    start = time.monotonic()
    with pytest.raises(fd.ReviewRecordError) as refused:
        with fd.BatchReader(clone) as reader:
            reader.blob(oid)
    assert time.monotonic() - start < 2.0
    assert not mark.exists(), "the read tried to fetch"
    assert f"{oid} is missing locally (partial clone?)" in str(refused.value)
    fetch = str(refused.value).split("bring it in with: ")[1]
    assert fetch == f"git -C {shlex.quote(str(clone))} fetch -- {shlex.quote(partialclone or name)} {oid}"
    (tmp_path / "gone").rename(remote)
    for each in remotes:
        git(clone, "config", "--unset", f"remote.{each}.uploadpack")
    subprocess.run(["sh", "-c", fetch], check=True, capture_output=True)  # exactly as printed
    assert fd.GitReader(clone).blob(oid) == b"far\n"  # the named command brings it in


def test_batch_reader_refuses_a_missing_blob_with_no_fetch_outside_a_partial_clone(tmp_path):
    """A repo with no promisor remote has nothing to fetch from: missing, and no command to paste."""
    repo = tmp_path / "r"
    git(tmp_path, "init", "-q", str(repo))
    oid = "1" * 40
    with pytest.raises(fd.ReviewRecordError) as refused:
        with fd.BatchReader(repo) as reader:
            reader.blob(oid)
    assert f"{oid} is missing" in str(refused.value)
    assert "fetch" not in str(refused.value)


def test_batch_reader_lists_and_checks_a_tree_clone_without_fetching(tmp_path):
    """In a tree:0 clone every tree is absent. Each reader's ls-tree and the record check's fsck stay local like
    the batch cat-file: the absent tree is refused, and nothing reaches the remote, which is gone."""
    if tuple(int(n) for n in re.findall(r"\d+", git(tmp_path, "version"))[:2]) < (2, 44):
        pytest.skip("git before 2.44 ignores GIT_NO_LAZY_FETCH")
    remote = tmp_path / "remote"
    git(tmp_path, "init", "-q", str(remote))
    git(remote, "config", "uploadpack.allowFilter", "true")
    (remote / "d").mkdir()
    (remote / "d" / "f").write_text("far\n")
    git(remote, "add", "d")
    git(remote, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "d")
    root = git(remote, "rev-parse", "HEAD^{tree}")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", "--no-checkout", "--filter=tree:0", remote.as_uri(), str(clone))
    # a fetch from the remote runs this upload-pack, which leaves a mark; the remote is gone, so it then fails
    mark = tmp_path / "fetched"
    pack = tmp_path / "upload-pack"
    pack.write_text(f"#!/bin/sh\ntouch {mark}\nexec git-upload-pack \"$@\"\n")
    pack.chmod(0o755)
    git(clone, "config", "remote.origin.uploadpack", str(pack))
    remote.rename(tmp_path / "gone")
    for reader in (fd.BatchReader(clone), fd.GitReader(clone)):
        with reader, pytest.raises(fd.ReviewRecordError) as refused:
            reader.ls(root)
        assert "git ls-tree failed" in str(refused.value)
        assert not mark.exists(), f"{type(reader).__name__} ls-tree tried to fetch"
    # fsck passes a tree the promisor holds; the check then reads it, and refuses it as absent
    for reader in (fd.BatchReader(clone), None):
        with pytest.raises(fd.ReviewRecordError) as refused:
            fd.verify_tree(clone, root, reader=reader)
        assert "git ls-tree failed" in str(refused.value)
        assert not mark.exists(), "the record check tried to fetch"
        if reader:
            reader.close()


def _far_clone(tmp_path: Path) -> tuple[Path, str]:
    """A blob:none clone of a one-file remote, and the file's blob id, absent here; the remote is then moved
    away, so a lazy fetch would fail rather than succeed quietly."""
    remote = tmp_path / "remote"
    git(tmp_path, "init", "-q", str(remote))
    git(remote, "config", "uploadpack.allowFilter", "true")
    (remote / "f").write_text("far\n")
    git(remote, "add", "f")
    git(remote, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "f")
    oid = git(remote, "rev-parse", "HEAD:f")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", "--no-checkout", "--filter=blob:none", remote.as_uri(), str(clone))
    remote.rename(tmp_path / "gone")
    return clone, oid


def test_batch_reader_keeps_its_process_after_a_real_missing_line_in_a_partial_clone(tmp_path):
    """A real '<oid> missing' (two fields, no size) is not refused by the size check that runs first: the hint
    comes out, and the same git process then serves a present blob (a missing answer leaves the stream in step)."""
    clone, oid = _far_clone(tmp_path)
    here = git(clone, "hash-object", "-w", "--stdin", input="here\n")
    with fd.BatchReader(clone) as batch:
        assert batch.blob(here) == b"here\n"
        first = batch._cat._proc
        with pytest.raises(fd.ReviewRecordError) as refused:
            batch.blob(oid)
        assert f"{oid} is missing locally (partial clone?)" in str(refused.value)
        assert "fetch -- origin " + oid in str(refused.value)
        assert batch._cat is not None and batch._cat._proc is first and first.poll() is None
        assert batch.blob(here) == b"here\n"
        assert batch._cat._proc is first


def test_batch_reader_refuses_a_missing_line_for_another_object_in_a_partial_clone(tmp_path, monkeypatch):
    """In a clone that does have a promisor, '<other> missing' is an out-of-step stream, not an absent object:
    the id check runs before the hint, so the reader is abandoned and no fetch is offered for either id."""
    clone, oid = _far_clone(tmp_path)
    other = "e" * 40
    started: list[subprocess.Popen] = []

    def start(cwd, *args, **kwargs):
        started.append(subprocess.Popen([sys.executable, "-c", _ANSWER, f"{other} missing\n".encode().hex()],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE))
        return started[-1]

    monkeypatch.setattr(gitops, "start", start)
    with fd.BatchReader(clone) as batch:
        with pytest.raises(fd.ReviewRecordError) as refused:
            batch.blob(oid)
        assert "answered for another object" in str(refused.value)
        assert "partial clone" not in str(refused.value) and "fetch" not in str(refused.value)
        assert batch._cat is None
    assert started and started[0].wait(5) is not None
