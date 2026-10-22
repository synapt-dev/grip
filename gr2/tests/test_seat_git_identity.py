"""A runner's exported git identity never reaches a gr2 test.

GIT_AUTHOR_* and GIT_COMMITTER_* outrank the user.name that the root conftest's
_isolated_git_config writes, so a runner that exports them (an agent seat does)
made every commit a test wrote carry the runner's name. The outer row runs the
inner one in a child pytest with a seat-shaped identity exported, so it fails on
any runner, CI included, when the root fixture stops clearing them.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_GR2_ROOT = Path(__file__).resolve().parents[1]
_SEAT = {
    "GIT_AUTHOR_NAME": "Seat", "GIT_AUTHOR_EMAIL": "seat@example.invalid",
    "GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z",
    "GIT_COMMITTER_NAME": "Seat", "GIT_COMMITTER_EMAIL": "seat@example.invalid",
    "GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z",
}


def test_a_commit_in_a_test_carries_the_fixture_identity(tmp_path):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    who = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%an <%ae>|%cn <%ce>"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert who == "Fixture <fixture@example.invalid>|Fixture <fixture@example.invalid>"
    assert [n for n in _SEAT if n in os.environ] == []


def test_a_seat_identity_exported_to_the_runner_does_not_reach_a_test(tmp_path):
    inner = f"{Path(__file__).resolve()}::test_a_commit_in_a_test_carries_the_fixture_identity"
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         f"--basetemp={tmp_path / 'inner'}", inner],
        cwd=_GR2_ROOT, env={**os.environ, **_SEAT}, capture_output=True, text=True,
        stdin=subprocess.DEVNULL,
    )
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]
