"""gr2's ancestry answers read the real history: a replace ref (refs/replace/<sha>) never fakes descent.

A replace ref swaps a commit for another on every read git makes, so a feature tip replaced by a commit whose parent is
the target tip would read as already containing it. A "<feature> <target>" line in .git/info/grafts does the same and
is not turned off by GIT_NO_REPLACE_OBJECTS. gitops.is_ancestor runs the check with GIT_NO_REPLACE_OBJECTS=1 and
GIT_GRAFT_FILE pointed at an empty file."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from gr2.python_cli import branch_update, gitops


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name + "\n")
    _git(repo, "add", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", name)
    return _git(repo, "rev-parse", "HEAD")


def _setup(tmp_path: Path) -> tuple[Path, str, str]:
    """origin with main at T (past A) and feature at F (on A, pushed); `work` is on feature. Returns (work, F, T)."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "init", "-q", "-b", "main", str(work))
    _git(work, "remote", "add", "origin", str(origin))
    _commit(work, "a")
    _git(work, "push", "-q", "origin", "main")
    _git(work, "checkout", "-q", "-b", "feature")
    feature = _commit(work, "f")
    _git(work, "push", "-q", "origin", "feature")
    _git(work, "checkout", "-q", "main")
    target = _commit(work, "t")
    _git(work, "push", "-q", "origin", "main")
    _git(work, "checkout", "-q", "feature")
    return work, feature, target


def _replace_with_fake_child(work: Path, feature: str, target: str) -> None:
    """refs/replace/<feature> -> a commit with feature's tree whose only parent is target."""
    tree = _git(work, "rev-parse", f"{feature}^{{tree}}")
    fake = subprocess.run(["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "commit-tree", tree, "-p", target, "-m", "fake"],
                          check=True, capture_output=True, text=True).stdout.strip()
    _git(work, "replace", feature, fake)
    # The probe is real: plain git (replace refs honoured) now reads target as an ancestor of feature.
    assert subprocess.run(["git", "-C", str(work), "merge-base", "--is-ancestor", target, feature]).returncode == 0


def test_update_does_not_read_a_replaced_tip_as_up_to_date(tmp_path):
    work, feature, target = _setup(tmp_path)
    _replace_with_fake_child(work, feature, target)
    code, receipt = branch_update.update(work, "main")
    assert receipt["state"] != "up_to_date", receipt
    assert receipt["base"] == target


def test_update_control_without_replace_still_reads_up_to_date(tmp_path):
    work, feature, target = _setup(tmp_path)
    _git(work, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "merge", "-q", "--no-edit", "main")
    _git(work, "push", "-q", "origin", "feature")
    code, receipt = branch_update.update(work, "main")
    assert (code, receipt["state"]) == (branch_update.EXIT_DONE, "up_to_date"), receipt


def test_is_ancestor_true_false_none_and_replace(tmp_path):
    work, feature, target = _setup(tmp_path)
    base = _git(work, "rev-parse", f"{feature}^")
    assert gitops.is_ancestor(work, base, feature) is True
    assert gitops.is_ancestor(work, target, feature) is False
    assert gitops.is_ancestor(work, "0" * 40, feature) is None
    assert gitops.is_ancestor(work, base, "1" * 40) is None
    _replace_with_fake_child(work, feature, target)
    assert gitops.is_ancestor(work, target, feature) is False
    assert gitops.is_ancestor(work, target, feature, via_cwd=True) is False
    proc = gitops.ancestry_proc(work, target, feature)
    assert proc.returncode == 1


def _graft_onto(work: Path, feature: str, target: str) -> None:
    """.git/info/grafts gives feature the single parent target."""
    grafts = work / ".git" / "info" / "grafts"
    grafts.parent.mkdir(parents=True, exist_ok=True)
    grafts.write_text(f"{feature} {target}\n")
    # The probe is real: plain git reads target as an ancestor of feature, with replace refs off too.
    assert subprocess.run(["git", "-C", str(work), "merge-base", "--is-ancestor", target, feature],
                          env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}, capture_output=True).returncode == 0


def test_is_ancestor_ignores_grafts(tmp_path):
    work, feature, target = _setup(tmp_path)
    base = _git(work, "rev-parse", f"{feature}^")
    _graft_onto(work, feature, target)
    assert gitops.is_ancestor(work, target, feature) is False
    assert gitops.is_ancestor(work, target, feature, via_cwd=True) is False
    assert gitops.ancestry_proc(work, target, feature).returncode == 1
    # The real parent still reads as an ancestor: the graft file is ignored, not the history.
    assert gitops.is_ancestor(work, base, feature) is True


def test_update_does_not_read_a_grafted_tip_as_up_to_date(tmp_path):
    work, feature, target = _setup(tmp_path)
    _graft_onto(work, feature, target)
    code, receipt = branch_update.update(work, "main")
    assert receipt["state"] != "up_to_date", receipt
    assert receipt["base"] == target
