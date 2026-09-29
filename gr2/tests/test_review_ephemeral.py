"""Review-ephemeral lane materialization — blobless + sparse
from the persistent mirror, separate from the strict work-lane clone seam."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import commit as commit_ops
from gr2.python_cli import review_ephemeral


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", *a], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def _mirror_with_bulk(tmp_path: Path) -> tuple[Path, str, str]:
    """A bare mirror whose base..head range touches BOTH a kept path and a path
    under an excluded /bulk dir. Returns (mirror, base, head)."""
    origin = tmp_path / "src.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    src = tmp_path / "src"
    _git(tmp_path, "clone", "-q", str(origin), str(src))
    _git(src, "config", "user.email", "t@example.invalid")
    _git(src, "config", "user.name", "t")
    (src / "keep.txt").write_text("keep base\n")
    (src / "bulk").mkdir()
    (src / "bulk" / "big.txt").write_text("big base\n")
    (src / "bulk" / "other.txt").write_text("other (never in range)\n")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "base")
    _git(src, "push", "-q", "origin", "main")
    base = _git(src, "rev-parse", "HEAD")
    # head touches keep.txt AND bulk/big.txt (bulk is excluded by the profile)
    (src / "keep.txt").write_text("keep head\n")
    (src / "bulk" / "big.txt").write_text("big head\n")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "head")
    _git(src, "push", "-q", "origin", "main")
    head = _git(src, "rev-parse", "HEAD")
    mirror = tmp_path / "cache" / "myrepo.git"
    mirror.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "--mirror", str(origin), str(mirror))
    _git(mirror, "config", "uploadpack.allowFilter", "true")
    return mirror, base, head


def test_review_ephemeral_sparse_keeps_touched_excluded_path(tmp_path: Path, monkeypatch) -> None:
    mirror, base, head = _mirror_with_bulk(tmp_path)
    # profile (gripspace fallback) excludes /bulk
    pdir = tmp_path / "profiles"; pdir.mkdir()
    (pdir / "myrepo.exclude").write_text("/*\n!/bulk/\n")
    monkeypatch.setenv("SYNAPT_REVIEW_PROFILE_DIR", str(pdir))

    dest = tmp_path / "lane"
    info = review_ephemeral.materialize_review_ephemeral(
        mirror=mirror, dest=dest, head=head, base=base, repo_name="myrepo",
    )
    # the range touches bulk/big.txt -> present despite /bulk being excluded (union)
    assert (dest / "bulk" / "big.txt").is_file()
    # a non-range excluded file is ABSENT -> sparse really excludes
    assert not (dest / "bulk" / "other.txt").exists()
    # a kept path is present
    assert (dest / "keep.txt").is_file()
    # blobless: a partial clone (promisor remote)
    assert _git(dest, "config", "--get", "remote.origin.promisor") == "true"
    assert info["profile_source"].startswith("fallback")


def test_review_ephemeral_blobless_patch_id_equals_full(tmp_path: Path) -> None:
    mirror, base, head = _mirror_with_bulk(tmp_path)
    dest = tmp_path / "lean"
    review_ephemeral.materialize_review_ephemeral(
        mirror=mirror, dest=dest, head=head, base=base, repo_name="myrepo",
    )
    lean_pid = subprocess.run(f"git -C {dest} diff {base} {head} | git patch-id --stable",
                              shell=True, text=True, capture_output=True).stdout.split()[0]
    full = tmp_path / "full"
    _git(tmp_path, "clone", "-q", str(mirror), str(full))
    full_pid = subprocess.run(f"git -C {full} diff {base} {head} | git patch-id --stable",
                              shell=True, text=True, capture_output=True).stdout.split()[0]
    assert lean_pid == full_pid


def test_commit_refuses_a_review_ephemeral_lane_repo(tmp_path: Path) -> None:
    # Constraint 2: a review lane never becomes a work lane. A repo carrying the
    # review-ephemeral record refuses a commit, naming the kind.
    repo = tmp_path / "r"
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "t@e.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("x\n")
    _git(repo, "add", ".")
    (repo / ".git" / "grip-review.json").write_text('{"lane_kind": "review-ephemeral"}')
    with pytest.raises(commit_ops.CommitError, match="review-ephemeral"):
        commit_ops.create_commit(repo, "should refuse")


def test_work_lane_materialization_stays_strict(tmp_path: Path) -> None:
    """Constraint 1: the work-lane clone seam is NOT blobless and NOT reference-
    shared — a work lane clone has a complete history (no promisor) and its own
    object store (no alternates). Proves review-ephemeral did not relax it."""
    from gr2.python_cli import gitops
    origin = tmp_path / "o.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    src = tmp_path / "s"
    _git(tmp_path, "clone", "-q", str(origin), str(src))
    _git(src, "config", "user.email", "t@e.invalid"); _git(src, "config", "user.name", "t")
    (src / "a.txt").write_text("a\n"); _git(src, "add", "."); _git(src, "commit", "-q", "-m", "a")
    _git(src, "push", "-q", "origin", "main")
    (tmp_path / "ws" / ".grip").mkdir(parents=True)
    lane = tmp_path / "ws" / "lane"
    gitops.ensure_lane_checkout(source_repo_root=src, target_repo_root=lane,
                                branch="grip-review/open", workspace_root=tmp_path / "ws",
                                seed_commit=_git(src, "rev-parse", "HEAD"))
    # complete history: no promisor remote
    assert subprocess.run(["git", "-C", str(lane), "config", "--get", "remote.origin.promisor"],
                          capture_output=True, text=True).stdout.strip() == ""
    # independent object store: no alternates
    assert not (lane / ".git" / "objects" / "info" / "alternates").exists()
