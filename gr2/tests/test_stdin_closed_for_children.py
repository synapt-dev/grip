"""A child spawned with no stdin argument reads EOF at once.

The session fixture in conftest.py points fd 0 at /dev/null. Without it, and
whenever pytest leaves fd 0 alone (with capture off, -s / --capture=no, and with
--capture=sys or tee-sys), a child that reads stdin under a live-pipe runner
(``cmd | pytest -s``) waits on that pipe and the run hangs; here the 10s timeout
turns that hang into a fail.
"""
from __future__ import annotations

import subprocess
import sys


def test_git_mktree_missing_gets_eof(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    result = subprocess.run(
        ["git", "mktree", "--missing"],
        cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    # mktree of empty input is the empty tree.
    assert result.stdout.strip() == "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def test_python_child_reads_empty_stdin():
    result = subprocess.run(
        [sys.executable, "-c", "import sys; print(len(sys.stdin.read()))"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0"
