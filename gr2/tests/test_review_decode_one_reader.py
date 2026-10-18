"""Decoding a review field tree opens one reader: at most three git processes per decode, the same
record as the per-call reader, an oversized blob refused, and the reader ended when a decode raises."""
from __future__ import annotations

import json

import pytest

from gr2.python_cli import gitops, grip, review_field_tree as fd
from gr2.python_cli.app import app
from tests.test_review_publish_all import _git_identity, git, runner, REVIEW_REF_PREFIX  # noqa: F401


def _build(tmp_path, monkeypatch, m):
    author = tmp_path / "author"
    author.mkdir()
    rows, remotes = [], {}
    for i in range(m):
        name = f"m{i}"
        remote = tmp_path / f"{name}.git"
        git(tmp_path, "init", "--bare", "-b", "main", remote)
        seed = tmp_path / f"seed-{name}"
        seed.mkdir()
        git(seed, "init", "-b", "main")
        (seed / "p.txt").write_text(f"{name} base\n")
        git(seed, "add", "p.txt")
        git(seed, "commit", "-m", "base")
        git(seed, "push", remote, "main")
        git(author, "clone", "--no-local", "--branch", "main", remote, name)
        base = git(author / name, "rev-parse", "HEAD")
        git(author / name, "checkout", "-b", "feat")
        (author / name / "p.txt").write_text(f"{name} reviewed\n")
        git(author / name, "commit", "-am", "reviewed")
        rows.append({"key": name, "path": name, "remote": str(remote), "base": base,
                     "head": git(author / name, "rev-parse", "HEAD"), "ref": "refs/heads/main",
                     "source": str(author / name)})
        remotes[name] = remote
    assert runner.invoke(app, ["store", "init", str(author)]).exit_code == 0
    (tmp_path / "rows.json").write_text(json.dumps(rows))
    monkeypatch.chdir(author)
    assert runner.invoke(app, ["review", "bind", "--rows-json", str(tmp_path / "rows.json")]).exit_code == 0
    rv = "gr:" + git(author, "for-each-ref", "--format=%(refname)", REVIEW_REF_PREFIX).rsplit("/", 1)[1]
    assert runner.invoke(app, ["review", "publish", rv]).exit_code == 0
    ws = tmp_path / "rx"
    ws.mkdir()
    for name, remote in remotes.items():
        git(ws, "clone", "--no-local", "--single-branch", "--branch", "main", remote, name)
    assert runner.invoke(app, ["store", "init", str(ws)]).exit_code == 0
    return ws, rv, remotes


def _receive(tmp_path, monkeypatch, m):
    """Run receive --all over m members; count git processes started inside each decode and read every
    decoded tree a second time the per-call way (each pass its own GitReader), outside the count."""
    ws, rv, remotes = _build(tmp_path, monkeypatch, m)
    inside, per_decode, pairs = [False], [], []
    spawn, start, decode = gitops._spawn, gitops.start, grip._decode_field_tree

    def c_spawn(*a, **k):
        if inside[0]:
            per_decode[-1] += 1
        return spawn(*a, **k)

    def c_start(*a, **k):
        if inside[0]:
            per_decode[-1] += 1
        return start(*a, **k)

    def c_decode(repo, tree):
        per_decode.append(0)
        inside[0] = True
        try:
            got = decode(repo, tree)
        finally:
            inside[0] = False
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(fd, "BatchReader", fd.GitReader)
            pairs.append((got, decode(repo, tree)))
        return got

    monkeypatch.setattr(gitops, "_spawn", c_spawn)
    monkeypatch.setattr(gitops, "start", c_start)
    monkeypatch.setattr(grip, "_decode_field_tree", c_decode)
    out = runner.invoke(app, ["review", "receive", str(ws), rv, "--remote", str(remotes["m0"]), "--all"])
    assert out.exit_code == 0, out.output
    assert per_decode
    return per_decode, pairs


@pytest.mark.parametrize("m", [1, 3, 6])
def test_decode_starts_at_most_three_git_processes(tmp_path, monkeypatch, m):
    per_decode, _ = _receive(tmp_path, monkeypatch, m)
    assert max(per_decode) <= 3, per_decode


@pytest.mark.parametrize("m", [1, 3, 6])
def test_decode_matches_per_call_reader(tmp_path, monkeypatch, m):
    _, pairs = _receive(tmp_path, monkeypatch, m)
    for got, per_call in pairs:
        assert got == per_call


def test_decode_that_raises_mid_walk_ends_the_reader(tmp_path, monkeypatch):
    ws, rv, remotes = _build(tmp_path, monkeypatch, 1)
    started, readers = [], []
    start = gitops.start

    def c_start(*a, **k):
        proc = start(*a, **k)
        started.append(proc)
        return proc

    class Faulting(fd.BatchReader):
        def __init__(self, repo):
            super().__init__(repo)
            self.reads = 0
            readers.append(self)

        def blob(self, oid):
            self.reads += 1
            if self.reads == 2:
                raise fd.ReviewRecordError("injected fault mid-walk")
            return super().blob(oid)

    monkeypatch.setattr(gitops, "start", c_start)
    monkeypatch.setattr(fd, "BatchReader", Faulting)
    out = runner.invoke(app, ["review", "receive", str(ws), rv, "--remote", str(remotes["m0"]), "--all"])
    assert out.exit_code != 0
    assert readers and all(r.reads >= 2 for r in readers)
    assert all(r._cat is None for r in readers)  # the reader's own closed state
    assert started and all(p.poll() is not None for p in started), [p.args for p in started]


def test_blob_over_size_cap_refused_and_batch_ends(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    git(tmp_path, "init", "-q", repo)
    (repo / "big").write_text("x" * 200)
    oid = git(repo, "hash-object", "-w", "big")
    monkeypatch.setattr(fd, "MAX_BLOB_BYTES", 100)
    r = fd.BatchReader(repo)
    with pytest.raises(fd.ReviewRecordError, match="over the 100 byte limit"):
        r.blob(oid)
    assert r._cat is None  # the batch process was ended
