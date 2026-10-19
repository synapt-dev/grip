"""A stamp's bind record and a pin's verify-before-commit read the review record through one batch reader: the
git processes they start do not grow with the record's size, and the stamp's view is the per-call reader's."""
from __future__ import annotations

import pytest

from gr2.python_cli import approvals, gitops, grip, review_field_tree as fd
from gr2.python_cli.app import app
from tests.test_review_decode_one_reader import _build
from tests.test_review_publish_all import _git_identity, git, runner, REVIEW_REF_PREFIX  # noqa: F401


def _counting(monkeypatch):
    """A list that grows by one for each git process started while counting[0] is True."""
    counting, started = [False], [0]
    spawn, start = gitops._spawn, gitops.start

    def c_spawn(*a, **k):
        started[0] += counting[0]
        return spawn(*a, **k)

    def c_start(*a, **k):
        started[0] += counting[0]
        return start(*a, **k)

    monkeypatch.setattr(gitops, "_spawn", c_spawn)
    monkeypatch.setattr(gitops, "start", c_start)
    return counting, started


def _bind_ref(tmp_path):
    author = tmp_path / "author"
    return author, git(author, "for-each-ref", "--format=%(refname)", REVIEW_REF_PREFIX).splitlines()[0]


@pytest.mark.parametrize("m", [1, 6])
def test_bind_record_starts_a_bounded_number_of_git_processes(tmp_path, monkeypatch, m):
    _build(tmp_path, monkeypatch, m)
    author, ref = _bind_ref(tmp_path)
    counting, started = _counting(monkeypatch)
    counting[0] = True
    approvals._bind_record(author, ref)
    counting[0] = False
    # rev-parse, one ls-tree -r -t, one cat-file --batch: one per object would be dozens at m=6
    assert started[0] <= 4, started[0]


def test_bind_record_matches_the_per_call_reader(tmp_path, monkeypatch):
    _build(tmp_path, monkeypatch, 3)
    author, ref = _bind_ref(tmp_path)
    batched = approvals._bind_record(author, ref)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fd, "BatchReader", fd.GitReader)
        assert approvals._bind_record(author, ref) == batched


@pytest.mark.parametrize("m", [1, 6])
def test_pin_verifies_its_record_in_a_bounded_number_of_git_processes(tmp_path, monkeypatch, m):
    counting, started = _counting(monkeypatch)
    verify, seen = fd.verify_tree, []

    def c_verify(*a, **k):
        before = started[0]
        counting[0] = True
        try:
            return verify(*a, **k)
        finally:
            counting[0] = False
            seen.append(started[0] - before)

    monkeypatch.setattr(fd, "verify_tree", c_verify)
    _build(tmp_path, monkeypatch, m)
    assert seen
    # fsck, one ls-tree -r -t, one cat-file --batch per verify, whatever the member count
    assert max(seen) <= 4, seen
