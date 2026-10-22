"""Git at a full-mode root sees what gr2 sees.

In full mode the store's gitlink for a member is the member's own commit, so plain git at the root should agree
with gr2 about the members:

  * S  a dirty member file: `git status --porcelain` at the root lists that member exactly when gr2's status shows
       it dirty; with every member clean neither lists anything
  * C  a member commit staged and committed by plain git at the root (`git add <member> && git commit`) leaves the
       same gitlink that `gr2 store commit` records for it
  * D  a member commit gr2 has not recorded yet: `git diff` at the root shows that member's gitlink moving exactly
       when gr2's status shows the member off its recorded gitlink
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from tests.test_one_pin_record import _advance, _cli
from tests.test_review_merge_gate import _git_identity, git  # noqa: F401  (fixture and helper)

MEMBERS = ("alpha", "beta")


@pytest.fixture
def full_root(tmp_path, monkeypatch):
    """A native root with two members, switched to full mode after workspace init (which refuses a full-mode root),
    then advanced and recorded by `gr2 store commit`, so its gitlinks are written in full mode."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    root, seeds = tmp_path / "ws", {}
    root.mkdir()
    for name in MEMBERS:
        remote, seed = tmp_path / f"{name}.git", tmp_path / f"seed-{name}"
        git(tmp_path, "init", "-q", "--bare", "-b", "main", remote)
        git(tmp_path, "clone", "-q", remote, seed)
        git(seed, "checkout", "-q", "-B", "main")
        (seed / "f").write_text(f"{name} 1\n")
        git(seed, "add", "f")
        git(seed, "commit", "-qm", f"{name} 1")
        if name == "alpha":   # a second commit before the first record, so alpha's HEAD~1 always exists
            (seed / "f").write_text("alpha 1b\n")
            git(seed, "commit", "-qam", "alpha 1b")
        git(seed, "push", "-q", "origin", "main")
        git(tmp_path, "clone", "-q", f"file://{remote}", root / name)
        seeds[name] = seed
    assert _cli("store", "init", str(root)).exit_code == 0
    monkeypatch.chdir(root)
    assert _cli("store", "commit", "-m", "record").exit_code == 0
    assert _cli("workspace", "init", str(root)).exit_code == 0
    (root / "grip.toml").write_text('mode = "full"\n' + (root / "grip.toml").read_text())
    for name, seed in seeds.items():
        _advance(seed, f"{name} 2")
    assert _cli("sync", "run", str(root)).exit_code == 0
    assert _cli("store", "commit", "-m", "advance").exit_code == 0
    assert tomllib.loads(git(root, "show", "HEAD:grip.toml")).get("mode") == "full"
    return root


def _git_dirty(root: Path) -> set[str]:
    """The members `git status --porcelain` at the root lists."""
    listed = {line.split()[-1] for line in git(root, "status", "--porcelain").splitlines() if line}
    return {name for name in MEMBERS if name in listed}


def _gr2_dirty(root: Path) -> set[str]:
    """The members gr2's status shows dirty."""
    result = _cli("repo", "status", "--json")
    assert result.exit_code == 0, result.output
    rows = [row for row in json.loads(result.output) if Path(row["path"]) in {root / name for name in MEMBERS}]
    assert {row["repo"] for row in rows} == set(MEMBERS), rows
    return {row["repo"] for row in rows if row["status"]["dirty"]}


def test_s_git_status_at_the_root_lists_a_member_exactly_when_gr2_shows_it_dirty(full_root):
    assert git(full_root, "status", "--porcelain") == "", "a clean full root shows nothing to plain git"
    assert _gr2_dirty(full_root) == set(), "nor to gr2"
    (full_root / "alpha" / "f").write_text("alpha dirty\n")
    assert _gr2_dirty(full_root) == {"alpha"}
    assert _git_dirty(full_root) == {"alpha"}


def test_c_a_member_committed_by_git_at_the_root_gets_the_gitlink_gr2_records(full_root):
    (full_root / "alpha" / "f").write_text("alpha 3\n")
    git(full_root / "alpha", "commit", "-qam", "alpha 3")
    git(full_root, "add", "alpha")
    git(full_root, "commit", "-qm", "git records")
    by_git = git(full_root, "ls-tree", "HEAD", "alpha").split()[2]
    assert by_git == git(full_root / "alpha", "rev-parse", "HEAD")
    # gr2 gets its own pass: the root goes back to before git's commit, alpha's checkout stays
    git(full_root, "reset", "-q", "HEAD~1")
    before = git(full_root, "rev-parse", "HEAD")
    assert git(full_root, "ls-tree", "HEAD", "alpha").split()[2] != by_git
    assert _cli("store", "commit", "-m", "gr2 records").exit_code == 0
    assert git(full_root, "rev-parse", "HEAD~1") == before, "gr2 made a new root commit"
    assert git(full_root, "ls-tree", "HEAD", "alpha").split()[2] == by_git


def _gr2_rows(root: Path) -> list[dict]:
    """The member rows of `gr2 store status --json`."""
    result = _cli("store", "status", "--json")
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    rows = rows.get("members", rows) if isinstance(rows, dict) else rows
    return [row for row in rows if row["name"] in MEMBERS]


def _gr2_ahead(root: Path) -> set[str]:
    """The members `gr2 store status --json` shows at a commit other than their recorded gitlink."""
    return {row["name"] for row in _gr2_rows(root) if row["head"] != row["gitlink"]}


def test_d_a_member_commit_gr2_has_not_recorded_shows_in_root_git_diff_and_gr2_status(full_root):
    assert git(full_root, "diff", "--name-only") == "", "at the recorded root plain git sees no change"
    rows = _gr2_rows(full_root)
    assert {row["name"] for row in rows} == set(MEMBERS)
    for row in rows:
        assert row["pin"] == row["gitlink"], f"{row['name']}: the fixture's store commit leaves line == gitlink"
    assert _gr2_ahead(full_root) == set(), "nor does gr2"
    (full_root / "alpha" / "f").write_text("alpha 3\n")
    git(full_root / "alpha", "commit", "-qam", "alpha 3")
    assert _gr2_ahead(full_root) == {"alpha"}
    assert git(full_root, "diff", "--name-only").split() == ["alpha"]
