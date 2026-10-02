"""Row 1 (git am reconstruction): a project-review gr commit can CARRY the
reconstruction range per repo, so a pre-push head is reconstructed from the commit
alone -- no hand `git am`, no clone that already holds the head, no head on any
remote. This ports the review-BIND commit's carry-the-range/reconstruct/assert-tree
model (grip.reconstruct_review_lane) to the PROJECT-review path.

Shape (b), self-describing commit (Stromus, 2026-09-05). The assertion is TREE ==
the pinned head's tree, NOT sha: `git am` re-stamps the committer identity and date
at apply time, so an honest reconstruction has a different sha until the committer-
date-match lane (row 2) lands. The reconstructed sha is recorded beside the pinned
sha so row 2 has its before/after.
"""
from __future__ import annotations

from tests.native_root_helper import native_root, workspace_kind_commit

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from gr2.prototypes import lane_workspace_prototype as lanes
from gr2.python_cli import grip, open_gr_review


@pytest.fixture
def _isolated_review_cache(tmp_path, monkeypatch):
    """open_gr_enter(sources=None) resolves each source through the SHARED per-host
    review mirror; point it at a per-test dir so a stale mirror from another test never
    answers, and drop the profile dir so there is no gripspace sparse fallback."""
    monkeypatch.setenv("SYNAPT_REVIEW_CACHE_ROOT", str(tmp_path / "review-cache"))
    monkeypatch.delenv("SYNAPT_REVIEW_PROFILE_DIR", raising=False)


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", *a], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def _base_remote_and_range(tmp_path: Path) -> tuple[str, str, str, str, str]:
    """A bare origin carrying only BASE, plus a range.patch for a PRE-PUSH head that
    exists in NO clone the caller keeps and on NO remote -- only as the patch bytes.
    Returns (remote_url, base_sha, head_sha, head_tree, range_patch_text)."""
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
    # the review head: committed locally, NEVER pushed
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


def test_project_review_carries_range_and_reconstructs_tree(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    native_root(ws)
    remote, base, head, head_tree, range_patch = _base_remote_and_range(tmp_path)

    # create-project records the range for key "alpha" (base + remote + the patch).
    commit = grip.create_project_review_commit(
        ws,
        [{"key": "alpha", "repo": remote, "path": "repos/alpha", "base": base, "head": head}],
        ranges={"alpha": range_patch},
    )
    # the commit carries the range object subtree (self-describing).
    assert "alpha" in grip._tree_keys(ws, commit, "objects")

    # reconstruction: clone remote, checkout base, git am the carried range, assert
    # the resulting TREE equals the pinned head's tree; the sha is recorded, not asserted.
    lane_dir = tmp_path / "lane" / "alpha"
    result = grip.reconstruct_project_review_lane(ws, commit, "alpha", lane_dir)
    assert result["reconstructed_tree"] == head_tree
    assert result["bound_head"] == head            # the pinned head sha
    assert result["reconstructed_head"] != ""      # recorded for row 2's before/after
    # the reconstructed tree is what the range produces (new.txt present).
    assert (lane_dir / "new.txt").read_text() == "added by the review\n"


def test_ranges_referencing_an_unknown_key_is_refused(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    native_root(ws)
    remote, base, head, _tree, range_patch = _base_remote_and_range(tmp_path)
    with pytest.raises(grip.GripCorruptError, match="not in the pins"):
        grip.create_project_review_commit(
            ws,
            [{"key": "alpha", "repo": remote, "path": "repos/alpha", "base": base, "head": head}],
            ranges={"beta": range_patch},  # beta is not a pinned key
        )


def _base_remote_range_and_committers(tmp_path: Path):
    """Like _base_remote_and_range but a TWO-commit pre-push range whose commits have
    committer dates DIFFERENT from their author dates (as a rebased commit would), and
    also returns the committer TSV (name<TAB>email<TAB>ISO-date per commit, apply
    order). The date divergence is what makes the sha-faithful proof real: a plain
    `git am` or `--committer-date-is-author-date` would NOT reproduce these shas; only
    re-stamping the carried committer date does. Returns
    (remote, base, head, head_tree, range_patch, committers_tsv)."""
    origin = tmp_path / "beta.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "workc"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "config", "user.email", "dev@layne.pro")
    _git(work, "config", "user.name", "Layne Penney")
    (work / "f.txt").write_text("base\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "base")
    _git(work, "push", "-q", "origin", "main")
    base = _git(work, "rev-parse", "HEAD")

    def _commit(msg: str, adate: str, cdate: str) -> None:
        env = {"GIT_AUTHOR_DATE": adate, "GIT_COMMITTER_DATE": cdate}
        subprocess.run(["git", "add", "."], cwd=work, check=True)
        subprocess.run(["git", "commit", "-q", "-m", msg], cwd=work,
                       check=True, env={**__import__("os").environ, **env})

    (work / "f.txt").write_text("base\nreview change\n")
    (work / "new.txt").write_text("added by the review\n")
    _commit("first review commit", "2026-09-01T10:00:00-05:00", "2026-09-04T15:30:00-05:00")
    (work / "more.txt").write_text("second review file\n")
    _commit("second review commit", "2026-09-02T09:00:00-05:00", "2026-09-05T11:00:00-05:00")

    head = _git(work, "rev-parse", "HEAD")
    head_tree = _git(work, "rev-parse", "HEAD^{tree}")
    range_patch = subprocess.run(
        ["git", "format-patch", f"{base}..{head}", "--stdout"],
        cwd=work, text=True, capture_output=True, check=True).stdout
    committers = subprocess.run(
        ["git", "log", "--reverse", "--format=%cn%x09%ce%x09%cI", f"{base}..{head}"],
        cwd=work, text=True, capture_output=True, check=True).stdout
    return str(origin), base, head, head_tree, range_patch, committers


def test_carried_committers_reconstructs_the_exact_pinned_sha(tmp_path: Path) -> None:
    # Row 2 (committer-date match): with committer metadata carried, reconstruction is
    # SHA-faithful -- the rebuilt head equals the pinned pre-push head, not merely its
    # tree. The fixture's committer dates differ from its author dates, so this passes
    # ONLY because the carried committer date is re-stamped per commit.
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    native_root(ws)
    remote, base, head, head_tree, range_patch, committers = _base_remote_range_and_committers(tmp_path)

    commit = grip.create_project_review_commit(
        ws,
        [{"key": "beta", "repo": remote, "path": "repos/beta", "base": base, "head": head}],
        ranges={"beta": range_patch},
        committers={"beta": committers},
    )
    obj_names = grip._bind_git(ws, "ls-tree", "--name-only", f"{commit}:objects/beta").stdout.split()
    assert "committers" in obj_names  # the objects subtree carries the committer metadata

    lane_dir = tmp_path / "lane" / "beta"
    result = grip.reconstruct_project_review_lane(ws, commit, "beta", lane_dir)
    assert result["reconstructed_tree"] == head_tree
    assert result["reconstructed_head"] == head          # SHA-faithful, not just tree
    assert result["bound_head"] == head
    assert (lane_dir / "more.txt").read_text() == "second review file\n"


def test_create_refuses_committers_that_do_not_reproduce_the_pinned_head(tmp_path: Path) -> None:
    # If the carried committer metadata does not reproduce the pinned head sha (e.g. a
    # wrong committer date), create refuses at build time -- bad metadata cannot be
    # baked into the commit only to surface as a reconstruction failure at review open.
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    native_root(ws)
    remote, base, head, _tree, range_patch, committers = _base_remote_range_and_committers(tmp_path)
    # corrupt the first commit's committer date
    rows = committers.splitlines()
    cn, ce, _cd = rows[0].split("\t")
    rows[0] = f"{cn}\t{ce}\t2020-01-01T00:00:00+00:00"
    bad = "\n".join(rows) + "\n"
    with pytest.raises(grip.GripCorruptError, match="not the pinned head|does not describe"):
        grip.create_project_review_commit(
            ws,
            [{"key": "beta", "repo": remote, "path": "repos/beta", "base": base, "head": head}],
            ranges={"beta": range_patch},
            committers={"beta": bad},
        )


def test_committers_for_a_key_without_a_range_is_refused(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    native_root(ws)
    remote, base, head, _tree, _range, committers = _base_remote_range_and_committers(tmp_path)
    with pytest.raises(grip.GripCorruptError, match="without a carried range"):
        grip.create_project_review_commit(
            ws,
            [{"key": "beta", "repo": remote, "path": "repos/beta", "base": base, "head": head}],
            ranges=None,  # no range carried
            committers={"beta": committers},
        )


def test_reconstruct_project_review_lane_refuses_a_non_project_commit(tmp_path: Path) -> None:
    # A workspace-KIND commit carries no project-review range; reconstruction must
    # refuse it naming the schema, not blindly try to git am.
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    native_root(ws)
    remote, base, head, _tree, _range = _base_remote_and_range(tmp_path)
    wrong = workspace_kind_commit(
        ws, [{"key": "alpha", "remote": remote, "path": "repos/alpha", "commit": head, "base": base}])
    with pytest.raises(grip.GripCorruptError, match="project review"):
        grip.reconstruct_project_review_lane(ws, wrong, "alpha", tmp_path / "lane" / "alpha")


def test_open_gr_enter_reconstructs_a_carried_range_end_to_end(tmp_path: Path, _isolated_review_cache) -> None:
    # The verb path, end to end: a reviewer with NEITHER the pre-push head NOR a clone
    # that holds it opens the review from the carrying gr commit ALONE. open_gr_enter
    # with sources=None and no local_sources must reconstruct each carried range INSIDE
    # the verb (clone remote at base, git am the carried range, re-stamp), materialize
    # the reconstructed head, and record it -- the tree equal to the pinned head's tree,
    # asserted TREE not sha because git am re-stamps the committer. Until this test the
    # reconstruction was witnessed only at grip.reconstruct_project_review_lane (the
    # helper); the open_gr_enter carried branch (open_gr_review.py, `carried = ... if
    # sources is None`) was exercised by no committed test. The remote carries only base
    # (the head is NEVER pushed), so a silent fallback to the remote or to a full clone
    # would fail to produce the head at all -- reconstruction is the only path that can.
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    remote, base, head, head_tree, range_patch = _base_remote_and_range(tmp_path)
    (ws / ".grip" / "workspace_spec.toml").write_text(
        'schema_version = 1\nworkspace_name = "m1"\n\n'
        f'[[repos]]\nname = "alpha"\npath = "sources/alpha"\nurl = "{remote}"\n'
        '\n[[units]]\nname = "atlas"\npath = "agents/atlas"\nrepos = ["alpha"]\n'
    )
    native_root(ws)
    commit = grip.create_project_review_commit(
        ws,
        [{"key": "alpha", "repo": remote, "path": "repos/alpha", "base": base, "head": head}],
        ranges={"alpha": range_patch},
    )
    prior_cwd = tmp_path / "home"
    prior_cwd.mkdir()
    lanes.create_lane(argparse.Namespace(
        workspace_root=ws, owner_unit="atlas", lane_name="home", type="feature",
        repos="alpha", branch="main", source="test", default_commands=[],
    ))
    lanes.enter_lane(argparse.Namespace(
        workspace_root=ws, owner_unit="atlas", lane_name="home",
        actor="agent:atlas", notify_channel=False, recall=False,
    ))

    # sources=None, NO local_sources: the head exists nowhere but as the carried range.
    outcome = open_gr_review.open_gr_enter(
        ws, "atlas", "review-carry", commit, None, prior_cwd=prior_cwd, allow_local=True,
    )
    assert outcome.status == "opened", outcome
    lane_repo = outcome.review_root / "repos" / "alpha"
    # the reconstructed tree equals the pinned head's tree (the git am produced it)
    assert _git(lane_repo, "rev-parse", "HEAD^{tree}") == head_tree
    # the range's added file is present -> the range actually applied, not a no-op
    assert (lane_repo / "new.txt").read_text() == "added by the review\n"
    # the receipt records the reconstructed head beside the pinned head (row 2's
    # before/after) -- proof the carried branch, not the plain-clone branch, ran
    receipt = json.loads(open_gr_review.open_gr_receipt_path(outcome.review_root).read_text())
    alpha = next(r for r in receipt["repos"] if r["key"] == "alpha")
    assert "reconstructed_head" in alpha and alpha["reconstructed_head"] != ""
    assert alpha["head"] == head  # the pinned head sha is still what the gr commit binds
    assert lanes.require_current_lane(ws, "atlas")["lane_name"] == "review-carry"
