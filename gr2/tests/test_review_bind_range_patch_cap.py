"""A range patch larger than the blob limit is refused at bind, before anything is written; one at exactly
the limit binds and reads back."""
from __future__ import annotations

import json
import subprocess

import pytest

from gr2.python_cli import grip
from gr2.python_cli import review_field_tree as fd
from gr2.python_cli.app import app
from tests.test_review_publish_all import _git_identity, git, runner, REVIEW_REF_PREFIX  # noqa: F401


def _member(tmp_path):
    author = tmp_path / "author"
    author.mkdir()
    remote = tmp_path / "m0.git"
    git(tmp_path, "init", "--bare", "-b", "main", remote)
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-b", "main")
    (seed / "p.txt").write_text("base\n")
    git(seed, "add", "p.txt")
    git(seed, "commit", "-m", "base")
    git(seed, "push", remote, "main")
    git(author, "clone", "--no-local", "--branch", "main", remote, "m0")
    base = git(author / "m0", "rev-parse", "HEAD")
    git(author / "m0", "checkout", "-b", "feat")
    (author / "m0" / "p.txt").write_text("reviewed\n")
    git(author / "m0", "commit", "-am", "reviewed")
    head = git(author / "m0", "rev-parse", "HEAD")
    row = {"key": "m0", "path": "m0", "remote": str(remote), "base": base, "head": head,
           "ref": "refs/heads/main", "source": str(author / "m0")}
    assert runner.invoke(app, ["store", "init", str(author)]).exit_code == 0
    (tmp_path / "rows.json").write_text(json.dumps([row]))
    patch = subprocess.run(["git", "format-patch", f"{base}..{head}", "--stdout"], cwd=author / "m0",
                           capture_output=True, check=True).stdout
    return author, patch


def _bind(tmp_path, monkeypatch, author):
    monkeypatch.chdir(author)
    return runner.invoke(app, ["review", "bind", "--rows-json", str(tmp_path / "rows.json")])


def _objects(author):
    return sorted(git(author, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)").split())


def _patch_blob(author, patch):
    ref = git(author, "for-each-ref", "--format=%(refname)", REVIEW_REF_PREFIX)
    rows = git(author, "ls-tree", "-r", "-l", ref).splitlines()
    oids = [r.split()[2] for r in rows if r.split()[3] == str(len(patch))]
    assert len(oids) == 1, rows
    return oids[0]


def test_range_patch_over_the_cap_is_refused_at_bind_and_nothing_is_written(tmp_path, monkeypatch):
    author, patch = _member(tmp_path)
    before = _objects(author)
    monkeypatch.setattr(fd, "MAX_BLOB_BYTES", len(patch) - 1)
    out = _bind(tmp_path, monkeypatch, author)
    assert out.exit_code != 0
    want = str(grip.GripReviewRefused("range_patch_over_limit", f"<= {len(patch) - 1} bytes", f"{len(patch)} bytes"))
    assert want in out.output, out.output
    assert git(author, "for-each-ref", "--format=%(refname)", REVIEW_REF_PREFIX) == ""
    assert _objects(author) == before


def test_range_patch_row_over_the_cap_is_refused_at_bind_and_nothing_is_written(tmp_path, monkeypatch):
    # The same check on the range_patch row path: the frozen range itself, no source clone.
    author, patch = _member(tmp_path)
    row = json.loads((tmp_path / "rows.json").read_text())[0]
    del row["source"]
    (tmp_path / "range.patch").write_bytes(patch)
    row["range"] = str(tmp_path / "range.patch")
    (tmp_path / "rows.json").write_text(json.dumps([row]))
    before = _objects(author)
    monkeypatch.setattr(fd, "MAX_BLOB_BYTES", len(patch) - 1)
    out = _bind(tmp_path, monkeypatch, author)
    assert out.exit_code != 0
    want = str(grip.GripReviewRefused("range_patch_over_limit", f"<= {len(patch) - 1} bytes", f"{len(patch)} bytes"))
    assert want in out.output, out.output
    assert git(author, "for-each-ref", "--format=%(refname)", REVIEW_REF_PREFIX) == ""
    assert _objects(author) == before


def test_range_patch_at_exactly_the_cap_binds_and_reads_back(tmp_path, monkeypatch):
    author, patch = _member(tmp_path)
    monkeypatch.setattr(fd, "MAX_BLOB_BYTES", len(patch))
    out = _bind(tmp_path, monkeypatch, author)
    assert out.exit_code == 0, out.output
    oid = _patch_blob(author, patch)
    r = fd.BatchReader(author)
    try:
        assert r.blob(oid) == patch
    finally:
        r.close()


def test_range_patch_one_over_the_cap_is_refused_by_the_reader(tmp_path, monkeypatch):
    author, patch = _member(tmp_path)
    monkeypatch.setattr(fd, "MAX_BLOB_BYTES", len(patch))
    assert _bind(tmp_path, monkeypatch, author).exit_code == 0
    oid = _patch_blob(author, patch)
    monkeypatch.setattr(fd, "MAX_BLOB_BYTES", len(patch) - 1)
    r = fd.BatchReader(author)
    with pytest.raises(fd.ReviewRecordError, match=f"is {len(patch)} bytes, over the {len(patch) - 1} byte limit"):
        r.blob(oid)
    assert r._cat is None
