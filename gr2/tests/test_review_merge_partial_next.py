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

from gr2.python_cli.app import app
from tests.test_review_merge_gate import _git_identity, git, move, record_set, slice2  # noqa: F401  (fixtures)


def _staged_partial(s):
    """alpha merged, beta's merge undone on its remote and its feature branch moved: the rerun is a partial."""
    record_set(s)
    argv = ["review", "merge", "--approvals", "0", str(s["author"]), s["review"], "--from", "feat", "--into", "main"]
    assert CliRunner().invoke(app, argv).exit_code == 0
    beta = s["members"]["beta"]
    git(beta["remote"], "update-ref", "refs/heads/main", beta["base"])
    move(beta)
    ran = CliRunner().invoke(app, argv)
    assert ran.exit_code == 4, ran.output
    return {r["key"]: r for r in json.loads(ran.stdout)["members"]}


def test_the_partial_next_pins_the_moved_member_alone_and_reaches_exit_0_as_printed(slice2, tmp_path):
    rows = _staged_partial(slice2)
    alpha, beta = slice2["members"]["alpha"], slice2["members"]["beta"]
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
    merged = json.loads(ran.stdout.strip().splitlines()[-1])
    assert merged["exit"] == 0 and [m["key"] for m in merged["members"]] == ["beta"], merged
    assert git(alpha["remote"], "rev-parse", "main") == alpha_main
    git(beta["remote"], "merge-base", "--is-ancestor", commit, "main")


def test_the_readme_names_why_the_check_set_next_does_not_recover_a_partial():
    text = " ".join((Path(__file__).resolve().parents[1] / "README.md").read_text().split())
    assert "never moves the pin" in text and "base_not_live_head" in text and "pin that member alone" in text
