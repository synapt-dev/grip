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

from gr2.python_cli import gitops, next_steps
from gr2.python_cli.app import app
import pytest

from tests.test_review_merge_gate import _git_identity, git, make_slice, move, record_set, slice2  # noqa: F401  (fixtures)


@pytest.fixture
def slice3(tmp_path, monkeypatch):
    return make_slice(tmp_path, monkeypatch, ("alpha", "beta", "gamma"))


def _move_target(member):
    """Someone else moves the member's target: a commit on its base, so the merge's rerun refuses base_moved."""
    moved = git(member["remote"], "commit-tree", member["base"] + "^{tree}", "-p", member["base"], "-m", "someone else")
    git(member["remote"], "update-ref", "refs/heads/main", moved)
    return moved


def _staged_partial(s, mover=move):
    """alpha merged, beta's merge undone on its remote and its feature branch (or, with _move_target, its target)
    moved: the rerun is a partial. Any later member's merge is undone too, so the refusal leaves it unpushed at its
    pinned head."""
    record_set(s)
    argv = ["review", "merge", "--approvals", "0", str(s["author"]), s["review"], "--from", "feat", "--into", "main"]
    assert CliRunner().invoke(app, argv).exit_code == 0
    beta = s["members"]["beta"]
    for k, m in s["members"].items():
        if k != "alpha":
            git(m["remote"], "update-ref", "refs/heads/main", m["base"])
    mover(beta)
    ran = CliRunner().invoke(app, argv)
    assert ran.exit_code == 4, ran.output
    return {r["key"]: r for r in json.loads(ran.stdout)["members"]}


def _run_next_as_printed(s, tmp_path, mover=move, rows=None):
    """Stage the partial (or take rows already staged at push), run beta's printed next (the operator's pull or target
    merge + commit, then the line as printed), and return the rows and the last merge receipt."""
    code = "target_moved_at_push" if rows else "base_moved"
    rows = rows or _staged_partial(s, mover)
    alpha, beta = s["members"]["alpha"], s["members"]["beta"]
    alpha_main = git(alpha["remote"], "rev-parse", "main")
    step = rows["beta"]["next"]
    assert "--at-remote-heads" not in step and "gr2 review update" not in step and "\n" not in step, step
    if mover is move:
        assert rows["beta"]["refused"].startswith("feature_moved"), rows
        assert step.startswith("next: alpha already merged and beta's feature branch moved"), step
        # The operator's half: the printed pull, then their own commit.
        pull = step[step.index("commit your change: ") + len("commit your change: "):step.index("; then, with")]
        subprocess.run(["sh", "-c", pull], check=True, capture_output=True)
        (beta["repo"] / "fixed.txt").write_text("on top of the late commit\n")
        git(beta["repo"], "add", "fixed.txt")
        git(beta["repo"], "commit", "-m", "new head")
    else:
        assert rows["beta"]["refused"].startswith(code), rows
        assert step.startswith("next: alpha already merged and main moved on beta's remote"), step
        # The operator's half is the printed fetch and merge of the target, committed and not pushed.
        first = step[step.index("without pushing: ") + len("without pushing: "):step.index("; then, with")]
        subprocess.run(["sh", "-c", first], check=True, capture_output=True, stdin=subprocess.DEVNULL)
        assert git(beta["remote"], "rev-parse", "feat") == beta["head"]
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
    assert git(gamma["remote"], "rev-parse", "main") != gamma["base"], "gamma was not merged by the printed next"
    git(gamma["remote"], "merge-base", "--is-ancestor", gamma["head"], "main")
    assert merged["exit"] == 0 and [m["key"] for m in merged["members"]] == ["gamma"], merged


@pytest.mark.parametrize("names", [("alpha", "beta"), ("alpha", "beta", "gamma")])
def test_a_partial_whose_target_moved_reaches_exit_0_as_printed(tmp_path, monkeypatch, names):
    # alpha merged earlier, then someone moved beta's main: `review update` would pin alpha too and refuse, so beta's
    # next merges the observed main into beta and pins beta alone on it (gamma, if any, alone at its pinned head).
    s = make_slice(tmp_path, monkeypatch, names)
    rows, merged = _run_next_as_printed(s, tmp_path, _move_target)
    assert merged["exit"] == 0 and [m["key"] for m in merged["members"]] == [names[-1]], merged
    if "gamma" in names:
        gamma = s["members"]["gamma"]
        assert rows["gamma"]["final"] == "unmerged" and not rows["gamma"]["refused"], rows["gamma"]
        git(gamma["remote"], "merge-base", "--is-ancestor", gamma["head"], "main")


def _staged_at_push(s, monkeypatch):
    """alpha merged, then beta's main moved after the gate read it and before its push: a partial refused at push."""
    record_set(s)
    argv = ["review", "merge", "--approvals", "0", str(s["author"]), s["review"], "--from", "feat", "--into", "main"]
    beta = s["members"]["beta"]
    real = gitops.run
    moved = {}

    def push_after_move(repo, *args, **kw):
        # The gate has read beta's base; its push is the first moment after that read.
        if args[:1] == ("push",) and not moved and Path(repo).resolve() == beta["repo"].resolve():
            moved["main"] = _move_target(beta)
        return real(repo, *args, **kw)

    monkeypatch.setattr(gitops, "run", push_after_move)
    ran = CliRunner().invoke(app, argv)
    assert ran.exit_code == 4, ran.output
    rows = {r["key"]: r for r in json.loads(ran.stdout)["members"]}
    assert rows["beta"]["refused"].startswith("target_moved_at_push"), rows
    return rows, moved["main"]


def test_a_partial_whose_target_moved_at_push_prints_the_target_merge(tmp_path, monkeypatch):
    # The gate read beta's base, then main moved before the push: the same next as base_moved, on the main it saw.
    s = make_slice(tmp_path, monkeypatch, ("alpha", "beta", "gamma"))
    rows, main = _staged_at_push(s, monkeypatch)
    step = rows["beta"]["next"]
    assert step.startswith("next: alpha already merged and main moved on beta's remote"), step
    assert f"merge --no-edit {main}" in step and f"--base {main}" in step, step
    assert step.count("--repo gamma ") == 1 and "gr2 review update" not in step, step


def test_a_partial_whose_target_moved_at_push_reaches_exit_0_as_printed(tmp_path, monkeypatch):
    # The same at-push partial, its printed next run as printed: beta merged on the main it saw, gamma alone after.
    s = make_slice(tmp_path, monkeypatch, ("alpha", "beta", "gamma"))
    staged, main = _staged_at_push(s, monkeypatch)
    rows, merged = _run_next_as_printed(s, tmp_path, _move_target, rows=staged)
    beta, gamma = s["members"]["beta"], s["members"]["gamma"]
    git(beta["remote"], "merge-base", "--is-ancestor", main, "main")
    assert git(gamma["remote"], "rev-parse", "main") != gamma["base"], "gamma was not merged by the printed next"
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


def test_a_member_refused_for_a_conflict_after_a_land_gets_no_moved_next(slice2, monkeypatch):
    # alpha landed, beta's merge undone on its remote and its merge build now conflicts: beta is refused
    # merge_conflict, its feature and target where the pin left them, so its next never says the feature moved and
    # never pins it again.
    s = slice2
    record_set(s)
    argv = ["review", "merge", "--approvals", "0", str(s["author"]), s["review"], "--from", "feat", "--into", "main"]
    assert CliRunner().invoke(app, argv).exit_code == 0
    beta = s["members"]["beta"]
    git(beta["remote"], "update-ref", "refs/heads/main", beta["base"])
    real = gitops.real_history_run

    def conflict_on_beta(cwd, *args, **kw):
        if "merge-tree" in args and Path(cwd).resolve().name == "beta":
            return subprocess.CompletedProcess(args, 1, "", "")
        return real(cwd, *args, **kw)

    monkeypatch.setattr(gitops, "real_history_run", conflict_on_beta)
    ran = CliRunner().invoke(app, argv)
    rows = {r["key"]: r for r in json.loads(ran.stdout)["members"]}
    assert rows["alpha"]["final"] == "merged" and rows["beta"]["refused"] == "merge_conflict", rows
    step = rows["beta"].get("next") or ""
    assert "feature branch moved" not in step and "gr2 review pin" not in step, step


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


def test_a_partial_whose_count_refuses_base_moved_with_no_measured_heads_refuses_and_does_not_crash(slice2, monkeypatch):
    # alpha landed earlier; beta is ready but the approval count refuses base_moved and measures no heads, so there is
    # no observed target to pin: the run refuses beta and exits 4 instead of raising KeyError on the missing heads.
    from gr2.python_cli import approvals
    s = slice2
    record_set(s)
    argv = ["review", "merge", "--approvals", "0", str(s["author"]), s["review"], "--from", "feat", "--into", "main"]
    assert CliRunner().invoke(app, argv).exit_code == 0
    beta = s["members"]["beta"]
    git(beta["remote"], "update-ref", "refs/heads/main", beta["base"])

    def refuse(*a, **kw):
        raise approvals.ApprovalRefused("base_moved: the count read a moved base")
    monkeypatch.setattr(approvals, "count_approvals", refuse)
    ran = CliRunner().invoke(app, ["review", "merge", "--approvals", "1", *argv[4:]])
    assert ran.exception is None or isinstance(ran.exception, SystemExit), repr(ran.exception)
    assert ran.exit_code == 4, ran.output
    rows = {r["key"]: r for r in json.loads(ran.stdout)["members"]}
    assert rows["alpha"]["final"] == "merged" and rows["beta"]["final"] == "unmerged", rows
    assert rows["beta"]["refused"].startswith("base_moved"), rows["beta"]


def test_a_base_moved_refusal_that_names_no_branch_names_the_merge_target(slice2, monkeypatch):
    # The approval count's base_moved names no remote branch, so the next names the target this merge measured.
    from gr2.python_cli import approvals
    s = slice2
    record_set(s)
    argv = ["review", "merge", "--approvals", "0", str(s["author"]), s["review"], "--from", "feat", "--into", "main"]
    assert CliRunner().invoke(app, argv).exit_code == 0
    git(s["members"]["beta"]["remote"], "update-ref", "refs/heads/main", s["members"]["beta"]["base"])

    def refuse(*a, **kw):
        raise approvals.ApprovalRefused("base_moved: beta: no branch on its remote is at the reviewed base 0123456789ab")
    monkeypatch.setattr(approvals, "count_approvals", refuse)
    ran = CliRunner().invoke(app, ["review", "merge", "--approvals", "1", *argv[4:]])
    assert ran.exit_code == 4, ran.output
    step = {r["key"]: r for r in json.loads(ran.stdout)["members"]}["beta"]["next"]
    assert "--into main" in step and "<branch>" not in step, step


def test_a_dry_run_base_moved_refusal_that_names_no_branch_names_the_merge_target(slice2, monkeypatch):
    # The dry run (`review blockers`) writes its own next; it names the merge target too.
    from gr2.python_cli import approvals, merge_gate
    s = slice2
    record_set(s)
    argv = ["review", "merge", "--approvals", "0", str(s["author"]), s["review"], "--from", "feat", "--into", "main"]
    assert CliRunner().invoke(app, argv).exit_code == 0
    git(s["members"]["beta"]["remote"], "update-ref", "refs/heads/main", s["members"]["beta"]["base"])

    def refuse(*a, **kw):
        raise approvals.ApprovalRefused("base_moved: beta: no branch on its remote is at the reviewed base 0123456789ab")
    monkeypatch.setattr(approvals, "count_approvals", refuse)
    _, receipt = merge_gate.review_merge(s["author"], s["review"], into="main", feature="feat",
                                         required_approvals=1, dry_run=True)
    step = {r["key"]: r for r in receipt["members"]}["beta"]["next"]
    assert "--into main" in step and "<branch>" not in step, step


def test_a_base_moved_refusal_that_names_its_branch_keeps_that_branch():
    line = next_steps.merge_row("base_moved: remote refs/heads/release is abc, reviewed base def", workspace="/ws",
                                review="gr:abc", remote="/r.git", path="/ws/m", head="h", checks=(), into="main")
    assert line.endswith("--into release"), line
