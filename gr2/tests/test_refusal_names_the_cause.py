"""A refusal names the real cause and points at the workspace root.

`gr2 check run <path>` on a path that does not exist (or is not a repository) used to say the branch has no
upstream and no 'origin', which sends the user to configure a remote on a repo that is not there. And a
workspace verb run from inside a member (`gr2 review check .` with the cwd in a member clone) used to suggest
`gr2 workspace init <member>`, which would create a workspace nested inside a member of the real one. The rows
run the real CLI in a subprocess so stdout, stderr and the exit code are the ones a user sees.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

GR2_ROOT = Path(__file__).parents[1]

_CLI = """
import os, runpy, sys
sys.argv = [os.path.join(os.path.dirname(sys.executable), "gr2")] + sys.argv[1:]
runpy.run_module("gr2.python_cli.app", run_name="__main__")
"""

SPEC_TOML = 'workspace_name = "ws"\n\n[[repos]]\nname = "m1"\npath = "m1"\nurl = ""\n'
GRIP_TOML = 'schema_version = 1\nworkspace_name = "nativews"\n\n[[members]]\nname = "m1"\npath = "m1"\n'


def _gr2(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(GR2_ROOT)}
    return subprocess.run([sys.executable, "-P", "-c", _CLI, *args], cwd=cwd, text=True, capture_output=True, env=env)


def _check_run_reason(cwd: Path, path: str) -> str:
    result = _gr2(cwd, "check", "run", path, "--", "true")
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    return json.loads(result.stdout)["reason"]


def test_check_run_on_a_missing_path_names_the_path(tmp_path):
    reason = _check_run_reason(tmp_path, "nope")
    assert reason.startswith("repo_not_found:"), reason
    assert str((tmp_path / "nope").resolve()) in reason, reason
    assert "origin" not in reason and "remote_unresolved" not in reason


def test_check_run_on_a_directory_that_is_not_a_repository_says_so(tmp_path):
    (tmp_path / "plain").mkdir()
    reason = _check_run_reason(tmp_path, "plain")
    assert reason.startswith("not_a_repository:"), reason
    assert "plain" in reason and "origin" not in reason


def test_control_check_run_on_a_repo_without_origin_still_names_the_remote(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path / "r")], check=True)
    reason = _check_run_reason(tmp_path, "r")
    assert reason.startswith("remote_unresolved:") and "no 'origin'" in reason, reason


def _workspace(tmp_path: Path, kind: str) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root / "m1")], check=True)
    if kind == "spec":
        (root / ".grip").mkdir()
        (root / ".grip" / "workspace_spec.toml").write_text(SPEC_TOML, encoding="utf-8")
    else:
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        (root / "grip.toml").write_text(GRIP_TOML, encoding="utf-8")
    return root


def _member_refusal(tmp_path: Path, kind: str) -> tuple[Path, str]:
    root = _workspace(tmp_path, kind)
    member = root / "m1"
    result = _gr2(member, "review", "check", ".", "m1", "1")
    assert "Traceback" not in result.stderr, result.stderr
    assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    lines = result.stderr.strip().splitlines()
    assert len(lines) == 1, result.stderr
    return root, lines[0]


def test_review_check_from_a_member_points_at_the_workspace_root(tmp_path):
    root, line = _member_refusal(tmp_path, "spec")
    assert "workspace init" not in line, line
    assert f"inside the workspace at {root}" in line, line
    assert f"pass the workspace root {root}" in line, line


def test_review_check_from_a_member_of_a_native_root_points_at_the_root(tmp_path):
    root, line = _member_refusal(tmp_path, "native")
    assert "workspace init" not in line, line
    assert f"inside the workspace at {root}" in line, line


def test_control_review_check_at_the_root_reads_the_spec(tmp_path):
    root = _workspace(tmp_path, "spec")
    result = _gr2(root, "review", "check", ".", "m1", "1")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout)["repo"] == "m1"


def test_control_a_lone_directory_is_still_told_to_init(tmp_path):
    lone = tmp_path / "lone"
    lone.mkdir()
    result = _gr2(lone, "review", "check", ".", "m1", "1")
    assert f"`gr2 workspace init {lone}`" in result.stderr and "inside the workspace" not in result.stderr
