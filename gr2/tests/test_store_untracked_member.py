"""Rows for a member whose only changes are UNTRACKED files.

`store commit` and `store migrate` record each member's HEAD, and an untracked file cannot be
in a HEAD, so neither counts untracked files as dirty. Before this, both refused at exit 3 with
"commit or stash changes first", and a plain `git stash` does not stash untracked files, so a
desk whose only changes were scratch files could not record at all.

`store checkout` keeps the refusal: an untracked file at a path the target pin tracks makes
git's own checkout fail after the root has moved. Its advice now names `git stash -u`, which
does clear it.
"""

from __future__ import annotations

import json
from pathlib import Path

from gr2.python_cli import grip as grip_mod

from tests.test_store_break_attempts import (  # noqa: E402
    _assert_init_ran,
    _cli,
    _git,
    _git_out,
    _head,
    two_member_ws,
)


def _gitlink(root: Path, member: str) -> str:
    entry = _git_out(root, "ls-tree", "HEAD", "--", member)
    assert entry.startswith("160000 commit "), f"{member} must be a gitlink in the root, ls-tree said {entry!r}"
    return entry.split()[2]


def _initialised(root: Path) -> None:
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)


def test_commit_records_a_member_whose_only_changes_are_untracked(two_member_ws: Path) -> None:
    root = two_member_ws
    _initialised(root)
    _git(root / "alpha", "commit", "-q", "--allow-empty", "-m", "alpha second")
    _git(root / "alpha", "push", "-q", "origin", "main")
    (root / "alpha" / "scratch.txt").write_text("not part of any pin")
    assert _git_out(root / "alpha", "status", "--porcelain") == "?? scratch.txt"

    rc, out = _cli("store", "commit", "-m", "record with scratch present")

    assert rc == 0, out
    assert _gitlink(root, "alpha") == _head(root / "alpha")
    assert (root / "alpha" / "scratch.txt").read_text() == "not part of any pin"


def test_commit_still_refuses_a_tracked_modification(two_member_ws: Path) -> None:
    """Control: the flag narrows the check to tracked changes; it does not remove it."""
    root = two_member_ws
    _initialised(root)
    tracked = _git_out(root / "alpha", "ls-files").splitlines()[0]
    (root / "alpha" / tracked).write_text("changed and not committed")

    rc, out = _cli("store", "commit", "-m", "must refuse")

    assert rc == 3, out
    assert "alpha is dirty" in out


def test_migrate_ignores_untracked_files(two_member_ws: Path) -> None:
    root = two_member_ws
    grip_mod.grip_init(root)
    grip_mod.grip_snapshot(root, {"alpha": root / "alpha", "beta": root / "beta"}, message="alpha snapshot")
    pins = {name: _head(root / name) for name in ("alpha", "beta")}
    (root / "alpha" / "scratch.txt").write_text("not part of any pin")

    rc, out = _cli("store", "migrate", "--json")

    assert rc == 0, out
    json.loads(out)
    for name, pin in pins.items():
        assert _gitlink(root, name) == pin


def test_checkout_refusal_names_advice_that_clears_untracked_files(two_member_ws: Path) -> None:
    root = two_member_ws
    _initialised(root)
    assert _cli("store", "commit", "-m", "first record")[0] == 0
    target = _head(root)
    (root / "alpha" / "scratch.txt").write_text("would block a checkout")

    rc, out = _cli("store", "checkout", target[:12])

    assert rc == 3, out
    assert "alpha is dirty" in out
    assert "git stash -u" in out
    # The advice is true: following it clears the refusal.
    _git(root / "alpha", "stash", "-u", "-q")
    rc, out = _cli("store", "checkout", target[:12])
    assert rc == 0, out
