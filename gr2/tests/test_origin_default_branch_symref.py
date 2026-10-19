"""The lane guard reads the origin's default branch from `ls-remote --symref <url> HEAD`. ls-remote answers HEAD by
tail, so a symref refs/heads/a/HEAD is listed too; and splitlines cut a target holding U+2028 or U+0085 into a piece
that ends in HEAD. Only the row named exactly HEAD is the answer, and a listing that cannot say which row that is
reads as "cannot tell" (None)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gr2.prototypes import lane_workspace_prototype as lwp


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _origin(tmp_path: Path) -> tuple[Path, Path, str]:
    """A bare origin with one commit on main, and an empty repo with no origin/HEAD to read it from."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    tree = _git(origin, "mktree").strip()
    oid = _git(origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", "-m", "c", tree).strip()
    _git(origin, "update-ref", "refs/heads/main", oid)
    local = tmp_path / "local"
    _git(tmp_path, "init", "-q", str(local))
    return origin, local, oid


@pytest.mark.parametrize("sep", ["\u2028", "\u0085"], ids=["u2028", "u0085"])
def test_default_branch_whose_name_holds_a_separator_reads_whole(tmp_path, sep):
    origin, local, oid = _origin(tmp_path)
    branch = f"trunk/HEAD{sep}x"
    _git(origin, "update-ref", f"refs/heads/{branch}", oid)
    _git(origin, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    # splitlines puts "ref: refs/heads/trunk/HEAD" first, a row that ends in HEAD
    assert _git(origin, "ls-remote", "--symref", str(origin), "HEAD").splitlines()[0].endswith("trunk/HEAD")
    assert lwp._origin_default_branch(local, str(origin)) == branch


def test_tail_matched_symref_is_not_the_default_branch(tmp_path):
    origin, local, oid = _origin(tmp_path)
    _git(origin, "update-ref", "refs/heads/evil", oid)
    _git(origin, "symbolic-ref", "refs/heads/a/HEAD", "refs/heads/evil")
    _git(origin, "update-ref", "--no-deref", "HEAD", oid)  # detached: no HEAD symref row at all
    listing = _git(origin, "ls-remote", "--symref", str(origin), "HEAD")
    assert "ref: refs/heads/evil\trefs/heads/a/HEAD" in listing, listing
    assert lwp._origin_default_branch(local, str(origin)) is None


@pytest.mark.parametrize("listing", [
    "ref: refs/heads/main\tHEAD\nref: refs/heads/evil\tHEAD\n",
    "ref:refs/heads/main\tHEAD\n",
    "ref: refs/heads/main x\tHEAD\n",
    "ref: refs/heads/\tHEAD\n",
], ids=["two-rows", "no-space", "extra-field", "empty-branch"])
def test_listing_that_cannot_name_one_head_row_cannot_tell(tmp_path, monkeypatch, listing):
    _, local, _ = _origin(tmp_path)
    real = lwp.gitops.git

    def fake(cwd, *args, **kw):
        if "ls-remote" in args:
            return subprocess.CompletedProcess(args, 0, listing, "")
        return real(cwd, *args, **kw)

    monkeypatch.setattr(lwp.gitops, "git", fake)
    assert lwp._origin_default_branch(local, "file:///nowhere") is None


def test_plain_default_branch_still_reads(tmp_path):
    origin, local, _ = _origin(tmp_path)
    assert lwp._origin_default_branch(local, str(origin)) == "main"
