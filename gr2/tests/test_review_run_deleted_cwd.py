"""`gr2 review run` from a working directory that no longer exists is a named refusal, not a traceback.

`Path.cwd()` raises FileNotFoundError for a deleted directory, outside the ReviewRunRefused path, so the CLI
printed a Python traceback. The process below really deletes its own working directory before it runs the CLI.
`-P` keeps the deleted cwd off sys.path: on macOS `python -c` puts it there and an import dies with SystemError
before the code under test is reached.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from gr2.python_cli import review_run as rr

GR2_ROOT = Path(__file__).parents[1]

_DELETED_CWD_CLI = """
import os, runpy, sys
doomed = sys.argv[1]
os.chdir(doomed)
os.rmdir(doomed)
sys.argv = [os.path.join(os.path.dirname(sys.executable), "gr2")] + sys.argv[2:]  # absolute, as an installed console script has it
runpy.run_module("gr2.python_cli.app", run_name="__main__")
"""


def _run_from_deleted_cwd(tmp_path: Path, *cli_args: str) -> subprocess.CompletedProcess[str]:
    doomed = tmp_path / "doomed"
    doomed.mkdir()
    env = {**os.environ, "PYTHONPATH": str(GR2_ROOT)}
    return subprocess.run([sys.executable, "-P", "-c", _DELETED_CWD_CLI, str(doomed), *cli_args],
                          cwd=tmp_path, text=True, capture_output=True, env=env)


def test_review_run_from_a_deleted_cwd_refuses_by_name(tmp_path):
    result = _run_from_deleted_cwd(tmp_path, "review", "run")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "Traceback" not in result.stderr and "FileNotFoundError" not in result.stderr
    assert "no_review_context" in result.stderr + result.stdout or "no enclosing review reconstruction" in result.stderr
    assert "LANE_DIR" in result.stderr


def test_resolve_run_lane_refuses_when_the_cwd_was_deleted(tmp_path):
    import pytest

    doomed = tmp_path / "doomed"
    doomed.mkdir()
    old = os.getcwd()
    os.chdir(doomed)
    os.rmdir(doomed)
    try:
        with pytest.raises(rr.ReviewRunRefused) as caught:
            rr.resolve_run_lane(None)
    finally:
        os.chdir(old)
    assert caught.value.code == "no_review_context" and "LANE_DIR" in caught.value.detail


def test_an_explicit_lane_dir_needs_no_cwd_and_still_works_from_a_deleted_cwd(tmp_path):
    lane = tmp_path / "lane"
    lane.mkdir()
    (lane / rr._MARKER_NAME).write_text('{"kind":"review-open"}')
    doomed = tmp_path / "doomed"
    doomed.mkdir()
    old = os.getcwd()
    os.chdir(doomed)
    os.rmdir(doomed)
    try:
        assert rr.resolve_run_lane(lane) == lane.resolve()
    finally:
        os.chdir(old)
