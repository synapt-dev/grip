"""ls-remote answers a pattern by tail: asking for refs/heads/dev also lists refs/heads/a/refs/heads/dev, which sorts
first. A reader that takes the first token of the whole listing reads that lookalike's tip as the asked ref's. Each
reader here must take the row whose name is exactly the asked ref, including when the lookalike's name holds U+2028 or
U+0085, which str.split() cuts at."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from gr2.python_cli import gitops, review
from gr2.python_cli import review_rebind as rb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_review_rebind import _frozen_range, _git, _move_base_unrelated, _origin_with_base  # noqa: E402

SEPS = pytest.mark.parametrize("sep", ["", "\u2028", "\u0085"], ids=["tail", "u2028", "u0085"])


def _lookalike(origin: Path, asked: str, sep: str) -> str:
    """Point refs/heads/a<sep>/<asked> at a commit other than any tip of interest; returns that commit."""
    other = _git(origin, "commit-tree", "-m", "lookalike", _git(origin, "rev-parse", "HEAD^{tree}"))
    _git(origin, "update-ref", f"refs/heads/a{sep}/{asked}", other)
    names = [n for _, n in gitops.ls_remote_rows(_git(origin, "ls-remote", str(origin), asked))]
    assert names[0] != asked, names  # the lookalike is listed, and listed first
    return other


@SEPS
def test_ref_state_reads_absent_past_a_lookalike(tmp_path, sep):
    origin, _ = _origin_with_base(tmp_path)
    _lookalike(origin, "refs/heads/feat/x", sep)
    assert rb._ref_state(origin, str(origin), "refs/heads/feat/x") == "ABSENT on remote"


@SEPS
def test_intended_ref_absent_past_a_lookalike_is_not_public(tmp_path, sep):
    origin, base = _origin_with_base(tmp_path)
    frozen = _frozen_range(tmp_path, origin, base)
    _lookalike(origin, "refs/heads/feat/x", sep)
    res = rb.rebind(frozen, tmp_path / "author", "refs/remotes/origin/dev", tmp_path / "frozen-v2")
    assert res.outcome == "base_unchanged"


@SEPS
def test_live_base_is_the_asked_branch_not_a_lookalike(tmp_path, sep):
    origin, base = _origin_with_base(tmp_path)
    frozen = _frozen_range(tmp_path, origin, base)
    _lookalike(origin, "refs/heads/dev", sep)
    res = rb.rebind(frozen, tmp_path / "author", "refs/remotes/origin/dev", tmp_path / "frozen-v2")
    assert res.outcome == "base_unchanged"
    assert not (tmp_path / "frozen-v2").exists()


@SEPS
def test_host_pr_head_oid_is_the_pr_ref_not_a_lookalike(tmp_path, sep):
    origin, head = _origin_with_base(tmp_path)
    _git(origin, "update-ref", "refs/pull/7/head", head)
    other = _lookalike(origin, "refs/pull/7/head", sep)
    clone = tmp_path / "src"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    assert other != head
    assert review.host_pr_head_oid(clone, 7) == head


def test_exact_oid_none_when_absent_and_refuses_two_rows():
    a, b = "1" * 40, "2" * 40
    listing = f"{a}\trefs/heads/x/refs/heads/dev\n"
    assert gitops.ls_remote_oid(listing, "refs/heads/dev") is None
    assert gitops.ls_remote_oid(listing + f"{b}\trefs/heads/dev\n", "refs/heads/dev") == b
    with pytest.raises(ValueError):
        gitops.ls_remote_oid(f"{a}\trefs/heads/dev\n{b}\trefs/heads/dev\n", "refs/heads/dev")


def test_rebind_refuses_a_listing_naming_the_ref_twice(tmp_path, monkeypatch):
    origin, _ = _origin_with_base(tmp_path)
    twice = f"{'1' * 40}\trefs/heads/dev\n{'2' * 40}\trefs/heads/dev\n"
    real = rb._git

    def fake(repo, *args):
        if args[:1] == ("ls-remote",):
            return subprocess.CompletedProcess(args, 0, twice, "")
        return real(repo, *args)

    monkeypatch.setattr(rb, "_git", fake)
    with pytest.raises(rb.RebindRefused) as ei:
        rb._ref_state(origin, str(origin), "refs/heads/dev")
    assert ei.value.code == "ls_remote_ambiguous"


@pytest.mark.parametrize("step", ["freeze", "rebind"])
def test_two_row_listing_leaves_no_out_dir(tmp_path, monkeypatch, step):
    origin, base = _origin_with_base(tmp_path)
    frozen = _frozen_range(tmp_path, origin, base)
    _move_base_unrelated(origin)
    clone = tmp_path / "author"
    twice = f"{'1' * 40}\trefs/heads/feat/x\n{'2' * 40}\trefs/heads/feat/x\n"
    real = rb._git

    def fake(repo, *args, **kw):
        if args[:1] == ("ls-remote",) and args[-1] == "refs/heads/feat/x":
            return subprocess.CompletedProcess(args, 0, twice, "")
        return real(repo, *args, **kw)

    monkeypatch.setattr(rb, "_git", fake)
    out = tmp_path / "v2"
    with pytest.raises(rb.RebindRefused) as ei:
        if step == "freeze":
            rb.freeze(clone, "refs/remotes/origin/dev", out, title="t", body="b")
        else:
            rb.rebind(frozen, clone, "refs/remotes/origin/dev", out, allow_public_ref=True)
    assert ei.value.code == "ls_remote_ambiguous"
    assert not out.exists()
