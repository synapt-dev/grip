"""`review bind --from-range`: a classic freeze-public-range.sh range.patch (the
pre-push head exists in NO clone the caller keeps and on NO remote, only as the
patch bytes) can be bound directly, WITHOUT a hand `git am` and WITHOUT an author
`--source` clone that holds the head.

The producer (`review bind`) owns the reconstruction: it derives the head-tree by
applying the range over base in a throwaway clone (grip._carry_objects_from_range,
the primitive the project tier already uses), carries the range in the object, and
`open-gr` reconstructs by `git am` and asserts TREE equality. This is the frozen-range
git-am exit point removed from the gr2 review producer (the R2 closing-fruit lane).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gr2.python_cli import app as gr2_app
from gr2.python_cli import grip


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", *a], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def _base_remote_and_range(tmp_path: Path) -> tuple[str, str, str, str, str]:
    """A bare origin whose `main` carries only BASE, plus a range.patch for a PRE-PUSH
    head that exists in no kept clone and on no remote. Returns
    (remote_url, base_sha, head_sha, head_tree, range_patch_text)."""
    origin = tmp_path / "alpha.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "config", "user.email", "a@e.invalid")
    _git(work, "config", "user.name", "a")
    (work / "f.txt").write_text("base\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "base")
    _git(work, "push", "-q", "origin", "main")
    base = _git(work, "rev-parse", "HEAD")
    (work / "f.txt").write_text("base\nreview change\n")
    (work / "new.txt").write_text("added by the review\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "review head")
    head = _git(work, "rev-parse", "HEAD")
    head_tree = _git(work, "rev-parse", "HEAD^{tree}")
    range_patch = subprocess.run(
        ["git", "format-patch", f"{base}..{head}", "--stdout"],
        cwd=work, text=True, capture_output=True, check=True).stdout
    return str(origin), base, head, head_tree, range_patch


def _init_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    grip.grip_init(ws)
    return ws


def test_bind_from_range_carries_the_range_and_reconstructs_the_tree(tmp_path: Path) -> None:
    ws = _init_ws(tmp_path)
    remote, base, head, head_tree, range_patch = _base_remote_and_range(tmp_path)

    commit = grip.create_review_bind_commit(
        ws,
        [{"key": "alpha", "remote": remote, "path": "repos/alpha",
          "base": base, "head": head, "ref": "refs/heads/main",
          "range_patch": range_patch}],
    )
    # WITNESS (kills the routing mutation): a range-bearing row carries the objects
    # subtree, exactly as a --source row does. Neuter the `elif range_patch` branch
    # in create_review_bind_commit and this key is absent.
    assert "alpha" in grip._tree_keys(ws, commit, "objects")

    lane_dir = tmp_path / "lane" / "alpha"
    result = grip.reconstruct_review_lane(ws, commit, "alpha", lane_dir)
    # git am re-stamps the committer, so the sha differs; the TREE is the contract.
    assert result["reconstructed_tree"] == head_tree
    assert result["bound_head"] == head
    assert (lane_dir / "new.txt").read_text() == "added by the review\n"


def test_cli_bind_from_range_then_open_gr_matches_tree(tmp_path: Path) -> None:
    ws = _init_ws(tmp_path)
    remote, base, head, head_tree, range_patch = _base_remote_and_range(tmp_path)
    range_file = tmp_path / "range.patch"
    range_file.write_text(range_patch)
    runner = CliRunner()

    res = runner.invoke(gr2_app.app, [
        "review", "bind", str(ws), "--repo", "alpha", "--remote", remote,
        "--base", base, "--head", head, "--ref", "refs/heads/main",
        "--from-range", str(range_file),
    ])
    assert res.exit_code == 0, res.output
    assert res.output.strip().startswith("gr:"), res.output
    sha = res.output.strip()[len("gr:"):]

    lane_dir = tmp_path / "lane"
    res2 = runner.invoke(gr2_app.app, [
        "review", "open-gr", str(ws), sha, "--repo", "alpha",
        "--lane-dir", str(lane_dir), "--enter", "--json",
    ])
    assert res2.exit_code == 0, res2.output
    assert head_tree in res2.output, res2.output  # reconstructed_tree == bound_head_tree
    assert (lane_dir / "new.txt").read_text() == "added by the review\n"


def test_bind_from_range_refuses_a_head_the_range_does_not_describe(tmp_path: Path) -> None:
    # Head defense (parity with --source's source_missing_head): the declared --head
    # must be the head the range was formatted from. A different 40-hex head that is
    # NOT on the remote (so it clears the head-already-on-remote refusal) must still
    # be refused HERE, because the object records --head and open-gr reports it.
    ws = _init_ws(tmp_path)
    remote, base, _head, _tree, range_patch = _base_remote_and_range(tmp_path)
    wrong_head = "d" * 40  # valid sha shape, not on the remote, not the range's From-head
    with pytest.raises(grip.GripReviewRefused, match="range_head_mismatch"):
        grip.create_review_bind_commit(
            ws,
            [{"key": "alpha", "remote": remote, "path": "repos/alpha",
              "base": base, "head": wrong_head, "ref": "refs/heads/main",
              "range_patch": range_patch}],
        )


def test_bind_from_range_refuses_a_range_with_no_from_header(tmp_path: Path) -> None:
    ws = _init_ws(tmp_path)
    remote, base, head, _tree, _range_patch = _base_remote_and_range(tmp_path)
    with pytest.raises(grip.GripReviewRefused, match="range_no_from_header"):
        grip.create_review_bind_commit(
            ws,
            [{"key": "alpha", "remote": remote, "path": "repos/alpha",
              "base": base, "head": head, "ref": "refs/heads/main",
              "range_patch": "this carries no From <sha> header\n"}],
        )


def test_source_and_range_are_mutually_exclusive_in_the_row(tmp_path: Path) -> None:
    ws = _init_ws(tmp_path)
    remote, base, head, _tree, range_patch = _base_remote_and_range(tmp_path)
    with pytest.raises(grip.GripCorruptError, match="mutually exclusive"):
        grip.create_review_bind_commit(
            ws,
            [{"key": "alpha", "remote": remote, "path": "repos/alpha",
              "base": base, "head": head, "ref": "refs/heads/main",
              "source": str(tmp_path / "work"), "range_patch": range_patch}],
        )


def test_cli_source_and_from_range_are_mutually_exclusive(tmp_path: Path) -> None:
    ws = _init_ws(tmp_path)
    remote, base, head, _tree, range_patch = _base_remote_and_range(tmp_path)
    range_file = tmp_path / "range.patch"
    range_file.write_text(range_patch)
    runner = CliRunner()
    res = runner.invoke(gr2_app.app, [
        "review", "bind", str(ws), "--repo", "alpha", "--remote", remote,
        "--base", base, "--head", head, "--ref", "refs/heads/main",
        "--source", str(tmp_path / "work"), "--from-range", str(range_file),
    ])
    assert res.exit_code != 0
    assert "mutually exclusive" in res.output


def _empty_commit_range(tmp_path: Path) -> tuple[str, str, str, str, str]:
    """A bare origin at BASE, plus a range whose MIDDLE commit is EMPTY.

    Shape: base -> empty commit -> real change. Returns
    (remote_url, base_sha, head_sha, head_tree, range_patch_text).
    """
    origin = tmp_path / "emptyorigin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "emptywork"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "config", "user.email", "a@e.invalid")
    _git(work, "config", "user.name", "a")
    (work / "f.txt").write_text("base\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "base")
    _git(work, "push", "-q", "origin", "main")
    base = _git(work, "rev-parse", "HEAD")

    _git(work, "commit", "-q", "--allow-empty", "-m", "an intentionally empty commit")
    empty = _git(work, "rev-parse", "HEAD")
    (work / "f.txt").write_text("base\nreview change\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "the real change")
    head = _git(work, "rev-parse", "HEAD")
    head_tree = _git(work, "rev-parse", "HEAD^{tree}")

    # The fixture is only a witness if the empty commit is really there and the
    # range really spans two commits -- assert the subject before the instrument.
    assert len({base, empty, head}) == 3, "fixture did not create three distinct commits"
    assert _git(work, "rev-list", "--count", f"{base}..{head}") == "2", (
        "fixture range must be two commits, one of them empty"
    )
    range_patch = subprocess.run(
        ["git", "format-patch", f"{base}..{head}", "--stdout"],
        cwd=work, text=True, capture_output=True, check=True).stdout
    return str(origin), base, head, head_tree, range_patch


def test_an_empty_commit_range_applies_through_both_reconstruction_paths(tmp_path: Path) -> None:
    """grip.py:92/114 -- both `git am --empty=keep` call sites in _apply_range_in_lane.

    A frozen range may carry an EMPTY commit. Plain `git am` STOPS at one and
    leaves a PARTIAL tree, so the reconstruction would silently record the wrong
    head-tree while reporting success. Both paths must carry the whole range:
    path 1 with no committer table, path 2 with one.
    """
    remote, base, head, head_tree, range_patch = _empty_commit_range(tmp_path)

    # path 1 -- grip.py:92, no committer table
    lane = tmp_path / "lane-no-committers"
    _git(tmp_path, "clone", "-q", remote, str(lane))
    _git(lane, "checkout", "-q", base)
    grip._apply_range_in_lane(lane, range_patch, None)
    assert _git(lane, "rev-list", "--count", f"{base}..HEAD") == "2", (
        "the empty commit was dropped: --empty=keep at grip.py:92 did not hold"
    )
    # am rewrites the commit (committer identity/date), so the SHA legitimately
    # differs. The empty commit SURVIVING is the claim under test, and the tree is
    # the content witness that survives the rewrite.
    assert "an intentionally empty commit" in _git(lane, "log", "--format=%s", f"{base}..HEAD"), (
        "the empty commit did not survive the reconstruction"
    )
    assert _git(lane, "rev-parse", "HEAD^{tree}") == head_tree, (
        "reconstruction stopped short: the tree is not the range terminal tree"
    )

    # path 2 -- grip.py:114, with a committer table (dates are rewritten, so the
    # sha legitimately differs; the TREE and the commit count are what must hold)
    lane2 = tmp_path / "lane-committers"
    _git(tmp_path, "clone", "-q", remote, str(lane2))
    _git(lane2, "checkout", "-q", base)
    rows = "\n".join([
        "T One\tt1@e.invalid\t2020-01-01T00:00:00+00:00",
        "T Two\tt2@e.invalid\t2020-01-02T00:00:00+00:00",
    ]) + "\n"
    grip._apply_range_in_lane(lane2, range_patch, rows)
    assert _git(lane2, "rev-list", "--count", f"{base}..HEAD") == "2", (
        "the empty commit was dropped: --empty=keep at grip.py:114 did not hold"
    )
    assert _git(lane2, "rev-parse", "HEAD^{tree}") == head_tree, (
        "committer-rewritten reconstruction produced a different tree"
    )
