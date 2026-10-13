"""`gr2 exec run` names the lane repos it did not run, and shows why the failed one failed.

A lane's `fail_fast` stops the run at the first failing repo; that is the lane's policy and
stays. But the table used to list only the repos that ran, with no line for the rest, and
the failing command's output was only in the payload: a run whose repo-a lacked pytest
showed "repo-a failed 1" and nothing about why, or that repo-b and repo-c never ran.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gr2.python_cli.app import app
from tests.test_exec_single_string_and_materialize_plan import _lane_toml

runner = CliRunner()
REPOS = ["repo-a", "repo-b", "repo-c"]


@pytest.fixture
def lane(request):
    parallelism = getattr(request, "param", "sequential")
    tmp = Path(tempfile.mkdtemp())
    workspace = tmp / "ws"
    base = workspace / ".grip" / "state" / "lanes" / "unit" / "lane"
    for repo in REPOS:
        (base / "repos" / repo).mkdir(parents=True)
    doc = {"lane_name": "lane", "owner_unit": "unit", "lane_type": "feature", "repos": REPOS,
           "branch_map": {r: "feat/x" for r in REPOS}}
    (base / "lane.toml").write_text(
        _lane_toml(doc).replace('parallelism = "sequential"', f'parallelism = "{parallelism}"'))
    current = workspace / ".grip" / "state" / "current_lane"
    current.mkdir(parents=True)
    (current / "unit.json").write_text(json.dumps({"current": {"lane_name": "lane"}}))
    (workspace / ".grip" / "events").mkdir(parents=True)
    yield workspace
    shutil.rmtree(tmp, ignore_errors=True)


def _run(workspace, *command, json_output=False):
    args = ["exec", "run", str(workspace), "unit", "--actor", "agent:test"]
    args += ["--json"] if json_output else []
    return runner.invoke(app, args + ["--", *command])


def test_a_stopped_run_names_the_repos_it_did_not_run(lane):
    payload = json.loads(_run(lane, "false", json_output=True).output)
    assert [r["repo"] for r in payload["results"]] == ["repo-a"]
    assert payload["not_run"] == ["repo-b", "repo-c"]

    text = _run(lane, "false").output
    assert "not run: repo-b, repo-c (fail_fast stopped after repo-a)" in text, text


def test_the_failed_repo_shows_its_stderr_tail(lane):
    # The message is assembled in the child, so it is NOT in the argv the table echoes
    # ("command = ..."); otherwise this row would pass on the echo alone.
    script = ("import sys; sys.stderr.write('Module' + 'NotFound' + 'Error: pytest\\n');"
              " sys.exit(1)")
    result = _run(lane, sys.executable, "-c", script)
    echoed = result.output.split("command = ", 1)[1].splitlines()[0]
    assert "ModuleNotFoundError" not in echoed
    assert "repo-a stderr: ModuleNotFoundError: pytest" in result.output, result.output


def test_a_clean_run_has_no_not_run_line(lane):
    payload = json.loads(_run(lane, "true", json_output=True).output)
    assert [r["repo"] for r in payload["results"]] == REPOS
    assert payload["not_run"] == []
    assert "not run:" not in _run(lane, "true").output


@pytest.mark.parametrize("lane", ["parallel"], indirect=True)
def test_a_parallel_run_reaches_every_repo(lane):
    payload = json.loads(_run(lane, "false", json_output=True).output)
    assert sorted(r["repo"] for r in payload["results"]) == REPOS
    assert payload["not_run"] == []


def test_the_failed_repo_shows_its_stdout_tail_beside_stderr(lane):
    # pytest writes its FAILED line to stdout and may write only a warning to stderr; the
    # stderr tail alone would show the warning as the reason.
    script = ("import sys; sys.stdout.write('FAIL' + 'ED test_x.py::test_x - assert 1 == 2\\n');"
              " sys.stderr.write('Deprecation' + 'Warning: unrelated\\n'); sys.exit(1)")
    result = _run(lane, sys.executable, "-c", script)
    echoed = result.output.split("command = ", 1)[1].splitlines()[0]
    assert "FAILED test_x" not in echoed
    assert "repo-a stdout: FAILED test_x.py::test_x - assert 1 == 2" in result.output, result.output
    assert "repo-a stderr: DeprecationWarning: unrelated" in result.output, result.output
