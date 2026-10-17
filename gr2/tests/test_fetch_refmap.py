"""A configured `remote.<name>.fetch` refspec writes no ref on a fetch that names its own refspec.

git-fetch(1), CONFIGURED REMOTE-TRACKING BRANCHES: a fetch by remote name with an explicit refspec also updates every
ref the configured refspecs map the fetched refs to, unless `--refmap=` is passed. Each row configures
`+<fetched ref>:refs/heads/keep` and runs one verb's fetch; `keep` must not move.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import merge_gate, set_check


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def clone(tmp_path):
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "dev", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "dev", str(work)], check=True)
    for n in ("one", "two"):
        (work / n).write_text(n)
        git(work, "add", n)
        git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", n)
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-q", "origin", "dev")
    git(work, "branch", "keep", "HEAD~1")
    return {"repo": work, "remote": remote, "keep": git(work, "rev-parse", "keep"), "tip": git(work, "rev-parse", "dev")}


def configure(repo: Path, src: str) -> None:
    git(repo, "config", "--replace-all", "remote.origin.fetch", f"+{src}:refs/heads/keep")


def test_set_check_remote_head_fetch_moves_no_configured_ref(clone):
    configure(clone["repo"], "refs/heads/dev")
    assert set_check._fetch_remote_head(clone["repo"], "origin", "dev") == clone["tip"]
    assert git(clone["repo"], "rev-parse", "keep") == clone["keep"]


def test_merge_gate_remote_tip_fetch_moves_no_configured_ref(clone):
    configure(clone["repo"], "refs/heads/dev")
    assert merge_gate._remote_tip(clone["repo"], "origin", "dev") == clone["tip"]
    assert git(clone["repo"], "rev-parse", "keep") == clone["keep"]


def test_merge_gate_tracking_refresh_writes_only_its_tracking_ref(clone):
    # The explicit refspec's own destination is the point of this fetch; the configured one is not.
    configure(clone["repo"], "refs/heads/dev")
    assert merge_gate._refresh_tracking(clone["repo"], str(clone["remote"]), "dev") is None
    assert git(clone["repo"], "rev-parse", "refs/remotes/origin/dev") == clone["tip"]
    assert git(clone["repo"], "rev-parse", "keep") == clone["keep"]


def test_merge_gate_review_scan_fetch_moves_no_configured_ref(clone):
    # The pin tie's review scan fetches each published review by the same remote name as the remote-tip fetch.
    from gr2.python_cli import declared_sets
    review = "refs/dev.synapt.grip/__reviews__/r1"
    git(clone["repo"], "push", "-q", "origin", f"dev:{review}")
    git(clone["repo"], "config", "--replace-all", "remote.origin.fetch", f"+{review}:refs/heads/keep")
    declaration = declared_sets.Declaration(sets=(), members={"m": {"path": "work", "remote": "origin"}})
    pin = git(clone["repo"], "commit-tree", "-m", "unmerged", f"{clone['tip']}^{{tree}}")
    assert merge_gate._pin_tie(clone["repo"].parent, declaration, "dev")("m", pin) is not None
    assert git(clone["repo"], "rev-parse", "keep") == clone["keep"]
