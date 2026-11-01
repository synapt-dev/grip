"""BatchReader's with block ends its cat-file process however the block ends. A raise in the middle
of a blob body leaves the rest of the body on the pipe: git blocks writing it, so a plain close()
(stdin closed, then wait) waits forever. Each row runs the block on a thread with a bounded join, so
a hang is a red row, not a hung suite."""
from __future__ import annotations

import os
import subprocess
import threading
import time

import pytest

from gr2.python_cli import check_records
from gr2.python_cli import review_field_tree as fd
from tests.test_review_transport import git


class _Raised(Exception):
    pass


def _repo(tmp_path, size):
    repo = tmp_path / "r"
    git(tmp_path, "init", "-q", str(repo))
    (repo / "big").write_bytes(os.urandom(size))
    return repo, git(repo, "hash-object", "-w", "big")


@pytest.fixture(autouse=True)
def _reap_own_git(monkeypatch):
    """Kill and reap every git this row started, so a git one row leaves hung is never counted by the next."""
    started = []
    orig = fd.gitops.start

    def start(*a, **k):
        proc = orig(*a, **k)
        started.append(proc)
        return proc

    monkeypatch.setattr(fd.gitops, "start", start)
    yield
    for proc in started:
        if proc.poll() is None:
            proc.kill()
        proc.wait(5)


def _git_children():
    found = subprocess.run(["pgrep", "-P", str(os.getpid()), "-x", "git"], capture_output=True, text=True)
    return found.stdout.split()


def _with_block(monkeypatch, repo, body):
    """Run ``body(reader)`` inside ``with BatchReader`` on a thread joined for 10 s. Returns the box
    (reader, cat-file process, exception that left the block) and whether the block hung."""
    box: dict = {}
    orig = fd.gitops.start

    def start(*a, **k):
        box["proc"] = orig(*a, **k)
        return box["proc"]

    monkeypatch.setattr(fd.gitops, "start", start)

    def run():
        try:
            with fd.BatchReader(repo) as r:
                box["r"] = r
                box["got"] = body(r)
        except BaseException as exc:  # handed to the row below
            box["exc"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(10)
    hung = t.is_alive()
    if hung and "proc" in box:
        box["proc"].kill()  # let the thread go; the row is red either way
        t.join(5)
    return box, hung


def _gone_within(proc, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if proc.poll() is not None and not _git_children():
            return True
        time.sleep(0.05)
    return False


def test_a_raise_mid_body_ends_the_batch_process(tmp_path, monkeypatch):
    repo, oid = _repo(tmp_path, 1 << 20)  # far past the pipe buffer: git is still writing when the raise lands

    def partial(self, n=-1):
        self._stream.read1(4096)
        raise _Raised("mid-body")

    monkeypatch.setattr(fd._Deadlined, "read", partial)
    box, hung = _with_block(monkeypatch, repo, lambda r: r.blob(oid))
    assert not hung, "HUNG: __exit__ waited in close() while git was blocked writing the unread body"
    assert isinstance(box.get("exc"), _Raised)
    assert _gone_within(box["proc"], 2), f"git still alive: {_git_children()}"
    assert box["r"]._cat is None


def test_a_clean_exit_closes_the_batch_process_normally(tmp_path, monkeypatch):
    repo, oid = _repo(tmp_path, 4096)
    box, hung = _with_block(monkeypatch, repo, lambda r: r.blob(oid))
    assert not hung and "exc" not in box
    assert box["got"] == (repo / "big").read_bytes()
    assert _gone_within(box["proc"], 2)
    assert box["proc"].returncode == 0  # stdin closed and git exited on its own, not killed
    assert box["r"]._cat is None


def test_a_raise_between_blobs_ends_the_batch_process(tmp_path, monkeypatch):
    repo, oid = _repo(tmp_path, 4096)

    def body(r):
        r.blob(oid)
        raise _Raised("between blobs")

    box, hung = _with_block(monkeypatch, repo, body)
    assert not hung
    assert isinstance(box.get("exc"), _Raised)
    assert _gone_within(box["proc"], 2), f"git still alive: {_git_children()}"
    assert box["r"]._cat is None


def test_a_raise_mid_body_through_the_check_store_ends_the_batch_process(tmp_path, monkeypatch):
    # check_records._Store owns a BatchReader; its __exit__ must hand the raise on, not close() and wait
    monkeypatch.setenv("GR2_GIX", "0")  # these rows watch git's batch process; the gix reader starts none
    repo, oid = _repo(tmp_path, 1 << 20)
    box: dict = {}
    orig = fd.gitops.start

    def start(*a, **k):
        box["proc"] = orig(*a, **k)
        return box["proc"]

    def partial(self, n=-1):
        self._stream.read1(4096)
        raise _Raised("mid-body")

    monkeypatch.setattr(fd.gitops, "start", start)
    monkeypatch.setattr(fd._Deadlined, "read", partial)

    def run():
        try:
            with check_records._Store(repo) as store:
                box["store"] = store
                store.blob(oid)
        except BaseException as exc:  # handed to the row below
            box["exc"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(10)
    hung = t.is_alive()
    if hung and "proc" in box:
        box["proc"].kill()  # let the thread go; the row is red either way
        t.join(5)
    assert not hung, "HUNG: _Store.__exit__ waited in close() while git was blocked writing the unread body"
    assert isinstance(box.get("exc"), _Raised)
    assert _gone_within(box["proc"], 2), f"git still alive: {_git_children()}"
    assert box["store"]._reader._cat is None


def test_a_swallowed_raise_mid_body_in_read_remote_check_ends_the_batch_process(tmp_path, monkeypatch):
    # _read_remote_check turns an OSError into a fail status inside `with own:`, so the store exits
    # cleanly: the reader itself must end git when the body read raises, or close() waits on it
    monkeypatch.setenv("GR2_GIX", "0")  # these rows watch git's batch process; the gix reader starts none
    repo, oid = _repo(tmp_path, 300 << 10)
    git(repo, "add", "big")
    git(repo, "commit", "-qm", "c")
    head = git(repo, "rev-parse", "HEAD")
    box: dict = {}
    orig = fd.gitops.start
    def start(*a, **k):
        box["proc"] = orig(*a, **k)
        return box["proc"]
    def partial(self, n=-1):
        self._stream.read1(4096)
        raise OSError(5, "EIO mid-body")
    monkeypatch.setattr(fd.gitops, "start", start)
    monkeypatch.setattr(fd._Deadlined, "read", partial)
    monkeypatch.setattr(check_records, "_snapshot", lambda *a: "0" * len(head))
    monkeypatch.setattr(check_records, "_entries", lambda *a: {head[:2] + "/" + head[2:]: oid})
    member = dict(path=str(repo), remote="origin", key="m")
    t = threading.Thread(target=lambda: box.update(
        result=check_records.read_remote_check("origin", member, head, ["ci"])), daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive(), "HUNG: the swallowed raise let the store close() and wait on git blocked writing"
    assert box["result"]["status"] == "fail"
    assert "EIO mid-body" in box["result"]["reason"]
    assert _gone_within(box["proc"], 2), f"git still alive: {_git_children()}"
    assert _git_children() == []


def test_a_swallowed_raise_on_the_header_in_read_remote_check_ends_the_batch_process(tmp_path, monkeypatch):
    # the header read can raise before any body byte, with git blocked writing a >64 KiB answer:
    # read_remote_check swallows it inside `with own:` the same way, so the reader must end git there too
    monkeypatch.setenv("GR2_GIX", "0")  # these rows watch git's batch process; the gix reader starts none
    repo, oid = _repo(tmp_path, 300 << 10)
    git(repo, "add", "big")
    git(repo, "commit", "-qm", "c")
    head = git(repo, "rev-parse", "HEAD")
    box: dict = {}
    orig = fd.gitops.start
    def start(*a, **k):
        box["proc"] = orig(*a, **k)
        return box["proc"]
    def broken(self):
        raise OSError(5, "EIO on the header")
    monkeypatch.setattr(fd.gitops, "start", start)
    monkeypatch.setattr(fd._Deadlined, "readline", broken)
    monkeypatch.setattr(check_records, "_snapshot", lambda *a: "0" * len(head))
    monkeypatch.setattr(check_records, "_entries", lambda *a: {head[:2] + "/" + head[2:]: oid})
    member = dict(path=str(repo), remote="origin", key="m")
    t = threading.Thread(target=lambda: box.update(
        result=check_records.read_remote_check("origin", member, head, ["ci"])), daemon=True)
    t.start()
    t.join(1)
    waiting = _git_children() if t.is_alive() else None  # counted while the join still waits
    t.join(9)
    assert not t.is_alive(), f"HUNG: git children counted mid-join: {waiting}"
    assert not waiting, f"git children counted while the reader was live: {waiting}"
    assert box["result"]["status"] == "fail"
    assert "EIO on the header" in box["result"]["reason"]
    assert _gone_within(box["proc"], 2), f"git still alive: {_git_children()}"
    assert _git_children() == []
