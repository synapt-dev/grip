"""On a partial merge whose refused member's feature branch moved, that member's `next` is the recovery that reaches
exit 0: a check over the moved set passes but never moves the pin, and a pin over every member refuses
base_not_live_head on the merged members' old base, so the next pins the refused member alone."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from gr2.python_cli import next_steps
from gr2.python_cli.app import app
import pytest

from tests.test_review_merge_gate import _git_identity, git, make_slice, move, record_set, slice2  # noqa: F401  (fixtures)


@pytest.fixture
def slice3(tmp_path, monkeypatch):
    return make_slice(tmp_path, monkeypatch, ("alpha", "beta", "gamma"))


def _staged_partial(s):
    """alpha merged, beta's merge undone on its remote and its feature branch moved: the rerun is a partial. Any
    later member's merge is undone too, so the refusal leaves it unpushed at its pinned head."""
    record_set(s)
    argv = ["review", "merge", "--approvals", "0", str(s["author"]), s["review"], "--from", "feat", "--into", "main"]
    assert CliRunner().invoke(app, argv).exit_code == 0
    beta = s["members"]["beta"]
    for k, m in s["members"].items():
        if k != "alpha":
            git(m["remote"], "update-ref", "refs/heads/main", m["base"])
    move(beta)
    ran = CliRunner().invoke(app, argv)
    assert ran.exit_code == 4, ran.output
    return {r["key"]: r for r in json.loads(ran.stdout)["members"]}


def _run_next_as_printed(s, tmp_path):
    """Stage the partial, run beta's printed next (the operator's pull + commit, then the line as printed), and
    return the rows, alpha's main before the run, beta's new commit and the last merge receipt."""
    rows = _staged_partial(s)
    alpha, beta = s["members"]["alpha"], s["members"]["beta"]
    alpha_main = git(alpha["remote"], "rev-parse", "main")
    assert rows["beta"]["refused"].startswith("feature_moved"), rows
    step = rows["beta"]["next"]
    assert step.startswith("next: alpha already merged and beta's feature branch moved"), step
    assert "--at-remote-heads" not in step and "\n" not in step, step
    # The operator's half: the printed pull, then their own commit.
    pull = step[step.index("commit your change: ") + len("commit your change: "):step.index("; then, with")]
    subprocess.run(["sh", "-c", pull], check=True, capture_output=True)
    (beta["repo"] / "fixed.txt").write_text("on top of the late commit\n")
    git(beta["repo"], "add", "fixed.txt")
    git(beta["repo"], "commit", "-m", "new head")
    commit = git(beta["repo"], "rev-parse", "HEAD")
    # The printed run, as printed; only the commit and the test command fill placeholders.
    shim = tmp_path / "bin" / "gr2"
    shim.parent.mkdir()
    shim.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -c "
                    "'import sys; from gr2.python_cli.app import app; sys.argv[0] = \"gr2\"; app()' \"$@\"\n")
    shim.chmod(0o755)
    line = step[step.index(" run: ") + len(" run: "):].replace("<your new commit>", commit)
    line = line.replace("<your test command>", shlex.join([sys.executable, "-c", "pass"]))
    env = {**os.environ, "PATH": f"{shim.parent}{os.pathsep}{os.environ['PATH']}",
           "PYTHONPATH": os.pathsep.join([str(Path(__file__).resolve().parents[1]), os.environ.get("PYTHONPATH", "")])}
    ran = subprocess.run(["sh", "-c", line], capture_output=True, text=True, env=env, timeout=300,
                         stdin=subprocess.DEVNULL)
    assert ran.returncode == 0, (ran.stdout, ran.stderr)
    assert git(alpha["remote"], "rev-parse", "main") == alpha_main
    git(beta["remote"], "merge-base", "--is-ancestor", commit, "main")
    return rows, json.loads(ran.stdout.strip().splitlines()[-1])


def test_the_partial_next_pins_the_moved_member_alone_and_reaches_exit_0_as_printed(slice2, tmp_path):
    _, merged = _run_next_as_printed(slice2, tmp_path)
    assert merged["exit"] == 0 and [m["key"] for m in merged["members"]] == ["beta"], merged


def test_a_member_left_unpushed_by_the_refusal_is_merged_by_the_same_next(slice3, tmp_path):
    # alpha merged earlier, beta refused feature_moved, gamma passed preflight but was never pushed: beta's next
    # also pins gamma alone at its pinned head and merges it.
    rows, merged = _run_next_as_printed(slice3, tmp_path)
    gamma = slice3["members"]["gamma"]
    assert rows["gamma"]["final"] == "unmerged" and not rows["gamma"]["refused"], rows["gamma"]
    assert git(gamma["remote"], "rev-parse", "main") != gamma["base"], "gamma was merged by the printed next"
    git(gamma["remote"], "merge-base", "--is-ancestor", gamma["head"], "main")
    assert merged["exit"] == 0 and [m["key"] for m in merged["members"]] == ["gamma"], merged


def test_two_moved_members_print_the_unpushed_member_once(tmp_path, monkeypatch):
    # beta and delta both moved, gamma was left unpushed: only the first moved member's next pins gamma, so running
    # both printed lines never pins gamma twice.
    s = make_slice(tmp_path, monkeypatch, ("alpha", "beta", "gamma", "delta"))
    record_set(s)
    argv = ["review", "merge", "--approvals", "0", str(s["author"]), s["review"], "--from", "feat", "--into", "main"]
    assert CliRunner().invoke(app, argv).exit_code == 0
    for k, m in s["members"].items():
        if k != "alpha":
            git(m["remote"], "update-ref", "refs/heads/main", m["base"])
    move(s["members"]["beta"])
    move(s["members"]["delta"])
    ran = CliRunner().invoke(app, argv)
    assert ran.exit_code == 4, ran.output
    rows = {r["key"]: r for r in json.loads(ran.stdout)["members"]}
    assert rows["gamma"]["final"] == "unmerged" and not rows["gamma"]["refused"], rows["gamma"]
    assert rows["delta"]["refused"].startswith("feature_moved"), rows["delta"]
    assert rows["beta"]["next"].count("--repo gamma ") == 1, rows["beta"]["next"]
    assert "gamma" not in rows["delta"]["next"], rows["delta"]["next"]


def test_every_head_in_the_partial_next_is_quoted():
    # A waiting member's head comes from the record, not from git, so it is quoted like every other value.
    head = "$(touch pwned)"
    line = next_steps._partial_moved_row(
        workspace="/ws", key="beta", member_path="beta", path="/ws/beta", remote="origin", base="b" * 40, into="main",
        feature="feat", landed=["alpha"], approvals=None,
        waiting=[{"key": "gamma", "member_path": "gamma", "path": "/ws/gamma", "remote": "origin", "base": "c" * 40,
                  "head": head}])
    assert line.count(f"--head {shlex.quote(head)} ") == 2, line
    assert line.count("--head '<your new commit>' ") == 2, line
    assert f"--head {head}" not in line, line


def test_the_readme_names_why_the_check_set_next_does_not_recover_a_partial():
    text = " ".join((Path(__file__).resolve().parents[1] / "README.md").read_text().split())
    assert "never moves the pin" in text and "base_not_live_head" in text and "pin that member alone" in text
