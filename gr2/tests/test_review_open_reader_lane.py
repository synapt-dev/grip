"""`review open` makes its lane from the host's review mirror, blobless and sparse.

A pinned review's lane used to be a plain `git clone <remote>`: a full clone per lane, every time
(a config review lane measured 2.2G with a one-file range). A READ lane now clones blobless from the
refreshed mirror under SYNAPT_REVIEW_CACHE_ROOT and checks out the same sparse set the ephemeral
lane uses (the profile, then every path the range touches). A lane a check runs in keeps the whole
clone, and a mirror that cannot serve this remote falls back to it.
"""
from __future__ import annotations

from tests.native_root_helper import native_root

import os
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from gr2.python_cli import app as gr2_app
from gr2.python_cli import grip, review_ephemeral


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", *a], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def _remote_with_bulk(tmp_path: Path, *, profile: bool, name: str = "alpha") -> tuple[str, str, str, str]:
    """A bare origin whose BASE carries a bulk dir (and, with profile, a .review-exclude that opts it
    out), plus a one-file range for a pre-push head. Returns (remote, base, head, range_patch)."""
    origin = tmp_path / f"{name}.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / f"work-{name}"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "config", "user.email", "a@e.invalid")
    _git(work, "config", "user.name", "a")
    (work / "f.txt").write_text("base\n")
    (work / "bulk").mkdir()
    (work / "bulk" / "big.txt").write_text("bulk\n" * 64)
    if profile:
        (work / ".review-exclude").write_text("/*\n!/bulk/\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "base")
    _git(work, "push", "-q", "origin", "main")
    base = _git(work, "rev-parse", "HEAD")
    (work / "f.txt").write_text("base\nreview change\n")
    _git(work, "commit", "-q", "-am", "review head")
    head = _git(work, "rev-parse", "HEAD")
    patch = subprocess.run(["git", "format-patch", f"{base}..{head}", "--stdout"],
                           cwd=work, text=True, capture_output=True, check=True).stdout
    return str(origin), base, head, patch


def _bound(tmp_path: Path, *, profile: bool = True) -> tuple[Path, str, str]:
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    native_root(ws)
    remote, base, head, patch = _remote_with_bulk(tmp_path, profile=profile)
    commit = grip.create_review_bind_commit(ws, [{
        "key": "alpha", "remote": remote, "path": "repos/alpha", "base": base, "head": head,
        "ref": "refs/heads/main", "range_patch": patch}])
    return ws, commit, remote


def test_a_reader_lane_is_blobless_sparse_from_the_mirror(tmp_path: Path) -> None:
    ws, commit, remote = _bound(tmp_path)
    lane = tmp_path / "lane"
    res = grip.reconstruct_review_lane(ws, commit, "alpha", lane, reader=True)
    assert res["bound_head_tree"] == res["reconstructed_tree"]
    assert (res["source"], res["profile"].startswith("in-repo .review-exclude")) == ("mirror", True)
    assert (lane / "f.txt").read_text() == "base\nreview change\n"   # the file the range touches
    assert not (lane / "bulk").exists()                               # the bulk the profile opts out
    assert _git(lane, "config", "--get", "remote.origin.partialclonefilter") == "blob:none"
    mirror = review_ephemeral.review_cache_root() / "alpha.git"
    assert _git(lane, "remote", "get-url", "origin") == f"file://{mirror}"
    # The mirror lives under this test's own cache root, never the host's.
    assert str(mirror).startswith(os.environ["SYNAPT_REVIEW_CACHE_ROOT"])


def test_a_repo_with_no_profile_reads_the_whole_tree_and_says_so(tmp_path: Path) -> None:
    ws, commit, _ = _bound(tmp_path, profile=False)
    lane = tmp_path / "lane"
    res = grip.reconstruct_review_lane(ws, commit, "alpha", lane, reader=True)
    assert res["bound_head_tree"] == res["reconstructed_tree"]
    assert (res["source"], res["profile"]) == ("mirror", "none")
    assert (lane / "bulk" / "big.txt").exists()


def test_a_lane_a_check_runs_in_stays_a_whole_clone_of_the_remote(tmp_path: Path) -> None:
    ws, commit, remote = _bound(tmp_path)
    lane = tmp_path / "lane"
    res = grip.reconstruct_review_lane(ws, commit, "alpha", lane)   # reader defaults to False
    assert res["bound_head_tree"] == res["reconstructed_tree"]
    assert (res["source"], res["profile"]) == ("remote", "none")
    assert (lane / "bulk" / "big.txt").exists()
    assert _git(lane, "remote", "get-url", "origin") == remote


def test_a_mirror_of_another_repo_with_the_same_name_is_not_used(tmp_path: Path) -> None:
    # It lacks this review's base, so the lane comes from the remote instead (objects are named by
    # content: a mirror that HAS the base serves the right bytes, whoever its origin is).
    ws, commit, remote = _bound(tmp_path)
    # Another repo, also named alpha, already mirrored at the same cache path.
    other = tmp_path / "elsewhere" / "alpha.git"
    other.parent.mkdir()
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(other))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(other), str(seed))
    _git(seed, "config", "user.email", "a@e.invalid")
    _git(seed, "config", "user.name", "a")
    (seed / "x.txt").write_text("x\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "x")
    _git(seed, "push", "-q", "origin", "main")
    mirror = review_ephemeral.review_cache_root() / "alpha.git"
    mirror.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "clone", "-q", "--mirror", str(other), str(mirror))
    lane = tmp_path / "lane"
    res = grip.reconstruct_review_lane(ws, commit, "alpha", lane, reader=True)
    assert res["bound_head_tree"] == res["reconstructed_tree"]
    assert res["source"] == "remote"
    assert _git(lane, "remote", "get-url", "origin") == remote


def test_review_open_prints_where_the_lane_came_from(tmp_path: Path) -> None:
    ws, commit, _ = _bound(tmp_path)
    lane = tmp_path / "lane"
    out = CliRunner().invoke(gr2_app.app, ["review", "open", str(ws), f"gr:{commit}", "--lane-dir", str(lane)])
    assert out.exit_code == 0, out.output
    assert "tree_match: True" in out.output
    assert "source: mirror  profile: in-repo .review-exclude" in out.output
    assert not (lane / "bulk").exists()


def _unwritable_cache_root(tmp_path: Path, monkeypatch) -> None:
    # A regular file where a directory must go: mkdir raises NotADirectoryError (an OSError) for every
    # user, root included, where a 0500 directory would not stop root.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x\n")
    monkeypatch.setenv("SYNAPT_REVIEW_CACHE_ROOT", str(blocker / "cache"))


def test_a_cache_root_that_cannot_be_written_falls_back_to_the_remote(tmp_path: Path, monkeypatch) -> None:
    ws, commit, remote = _bound(tmp_path)
    _unwritable_cache_root(tmp_path, monkeypatch)
    lane = tmp_path / "lane"
    res = grip.reconstruct_review_lane(ws, commit, "alpha", lane, reader=True)
    assert res["bound_head_tree"] == res["reconstructed_tree"]
    assert (res["source"], res["profile"]) == ("remote", "none")
    assert _git(lane, "remote", "get-url", "origin") == remote


def test_review_open_with_a_cache_root_that_cannot_be_written_exits_0(tmp_path: Path, monkeypatch) -> None:
    ws, commit, _ = _bound(tmp_path)
    _unwritable_cache_root(tmp_path, monkeypatch)
    lane = tmp_path / "lane"
    out = CliRunner().invoke(gr2_app.app, ["review", "open", str(ws), f"gr:{commit}", "--lane-dir", str(lane)])
    assert out.exit_code == 0, out.output
    assert "tree_match: True" in out.output
    assert "source: remote  profile: none" in out.output


def test_review_open_of_two_members_reads_each_from_the_mirror(tmp_path: Path) -> None:
    # The multi-member site (the way a multi-repo review opens) passes reader=True too.
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    native_root(ws)
    rows = []
    for name in ("alpha", "beta"):
        remote, base, head, patch = _remote_with_bulk(tmp_path, profile=True, name=name)
        rows.append({"key": name, "remote": remote, "path": f"repos/{name}", "base": base, "head": head,
                     "ref": "refs/heads/main", "range_patch": patch})
    commit = grip.create_review_bind_commit(ws, rows)
    lanes = tmp_path / "lanes"
    out = CliRunner().invoke(gr2_app.app, ["review", "open", str(ws), f"gr:{commit}", "--lane-dir", str(lanes)])
    assert out.exit_code == 0, out.output
    for name in ("alpha", "beta"):
        line = next((ln for ln in out.output.splitlines() if ln.startswith(f"{name}: ")), "")
        assert "tree_match=True" in line and "source=mirror" in line, out.output
        assert not (lanes / name / "bulk").exists(), out.output
