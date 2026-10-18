"""Every read from the batch reader's cat-file process runs under a deadline. A loose object
whose header claims more bytes than its body holds keeps ``cat-file --batch`` alive after a
short answer, so an unbounded read waits forever; the reader refuses it as truncated instead.
The repo has no pytest-timeout, so the row guards itself: the read runs on a thread, and a
read still running after the guard fails the row and kills what it started."""
from __future__ import annotations

import hashlib
import os
import threading
import time
import zlib
from pathlib import Path

import pytest

from gr2.python_cli import gitops
from gr2.python_cli import review_field_tree as fd
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
    monkeypatch.setattr(gitops, "start", lambda *a: started.append(real_start(*a)) or started[-1])
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
