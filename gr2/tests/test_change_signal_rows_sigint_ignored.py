"""The `gr2 change` signal rows hold when the suite itself starts with SIGINT ignored.

A detached or non-interactive launcher (a job a non-interactive shell starts with `&` (as `nohup ... &` does), or a `trap '' INT` shell) starts
pytest with SIGINT set to SIG_IGN, and that ignore passes to every child. gr2 keeps an inherited
ignore, so a row that signals a gr2 child must start that child with SIGINT back at its default;
otherwise the row's own SIGINT never lands and c5 sees no interrupt at all.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent


def _ignore_sigint() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)


@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT inheritance through exec is POSIX-only")
def test_c5_passes_when_the_suite_runs_with_sigint_ignored(tmp_path: Path) -> None:
    args = [
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--basetemp={tmp_path / 'bt'}",
        str(TESTS / "test_change.py"), "-k", "c5",
    ]
    proc = subprocess.run(
        args, cwd=TESTS.parent, env=dict(os.environ), capture_output=True, text=True, timeout=300,
        preexec_fn=_ignore_sigint,
    )
    assert proc.returncode == 0, (proc.stdout[-3000:], proc.stderr[-2000:])
    assert "2 passed" in proc.stdout, proc.stdout[-3000:]
