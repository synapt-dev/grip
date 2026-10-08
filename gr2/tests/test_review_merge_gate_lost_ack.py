"""`review_merge` after a push whose acknowledgement is lost: the remote is re-read, never assumed.

A transport failure after the remote accepted the push must read `merged`; an unreachable remote during
reconciliation reads `unknown`, the exit is 4, and no further member is pushed. The push is wrapped, not
replaced: the real `git push` runs, and the wrapper then reports a failure and, where asked, hides the remote.
"""
from __future__ import annotations

import subprocess

import pytest

from gr2.python_cli import merge_gate
from tests.test_review_merge_gate import _git_identity, check, git, merge, mains, slice2, world  # noqa: F401  (fixtures)

REAL_RUN = subprocess.run


def _is_push(cmd) -> bool:
    return isinstance(cmd, (list, tuple)) and len(cmd) > 3 and cmd[0] == "git" and "push" in cmd[:5]


def lose_acknowledgement(monkeypatch, *, hide=None, only_first=False):
    """Run the real push, then answer as a dropped connection. `hide` is a remote path to move away afterwards."""
    pushes = []

    def fake(cmd, *a, **kw):
        if not _is_push(cmd) or (only_first and pushes):
            return REAL_RUN(cmd, *a, **kw)
        pushes.append(cmd)
        real = REAL_RUN(cmd, *a, **kw)
        assert real.returncode == 0, real.stderr  # the push really landed before the acknowledgement was lost
        if hide is not None:
            hide.rename(hide.with_name(hide.name + ".away"))
        return subprocess.CompletedProcess(cmd, 1, real.stdout, "fatal: the remote end hung up unexpectedly")

    monkeypatch.setattr(merge_gate.subprocess, "run", fake)
    return pushes


def test_a_lost_acknowledgement_reads_merged_never_nothing_moved(world, monkeypatch):
    check(world)
    pushes = lose_acknowledgement(monkeypatch)
    code, receipt = merge(world)
    row = receipt["members"][0]
    tip = git(world["remote"], "rev-parse", "main")
    assert git(world["remote"], "rev-list", "--parents", "-n", "1", tip).split()[1:] == [world["base"], world["head"]]
    assert len(pushes) == 1
    assert row["state"] == "merged" and row["merged"] == tip, row
    assert row["final"] == "merged", row
    assert "push_error" not in row, "a push that landed must not be reported as an error"
    assert code == merge_gate.EXIT_MERGED, receipt


def test_a_lost_acknowledgement_on_the_first_member_does_not_stop_the_set(slice2, monkeypatch):
    lose_acknowledgement(monkeypatch, only_first=True)
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat")
    assert [r["state"] for r in receipt["members"]] == ["merged", "merged"], receipt
    assert code == merge_gate.EXIT_MERGED, receipt
    for k, m in slice2["members"].items():
        assert git(m["remote"], "rev-list", "--parents", "-n", "1", "main").split()[1:] == [m["base"], m["head"]], k


def test_a_push_timeout_after_the_push_landed_is_reconciled_from_the_remote(world, monkeypatch):
    check(world)

    def fake(cmd, *a, **kw):
        if not _is_push(cmd):
            return REAL_RUN(cmd, *a, **kw)
        assert REAL_RUN(cmd, *a, **kw).returncode == 0  # the push lands, then the call runs past its timeout
        raise subprocess.TimeoutExpired(cmd, 120)

    monkeypatch.setattr(merge_gate.subprocess, "run", fake)
    code, receipt = merge(world)
    row = receipt["members"][0]
    tip = git(world["remote"], "rev-parse", "main")
    assert git(world["remote"], "rev-list", "--parents", "-n", "1", tip).split()[1:] == [world["base"], world["head"]]
    assert row["state"] == "merged" and row["merged"] == tip and "push_error" not in row, row
    assert code == merge_gate.EXIT_MERGED, receipt


def test_a_push_oserror_that_never_landed_is_unconfirmed_not_a_crash(world, monkeypatch):
    check(world)

    def fake(cmd, *a, **kw):
        if _is_push(cmd):
            raise OSError("git vanished")
        return REAL_RUN(cmd, *a, **kw)

    monkeypatch.setattr(merge_gate.subprocess, "run", fake)
    code, receipt = merge(world)
    row = receipt["members"][0]
    assert row["state"] == "unmerged" and row["push_error"].startswith("push_unconfirmed"), row
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert git(world["remote"], "rev-parse", "main") == world["base"]


def test_a_push_that_never_landed_reads_unmerged_with_its_error(world, monkeypatch):
    check(world)

    def fake(cmd, *a, **kw):
        if _is_push(cmd):
            return subprocess.CompletedProcess(cmd, 1, "", "fatal: unable to access the remote")
        return REAL_RUN(cmd, *a, **kw)

    monkeypatch.setattr(merge_gate.subprocess, "run", fake)
    code, receipt = merge(world)
    row = receipt["members"][0]
    assert row["state"] == "unmerged" and "unable to access" in row["push_error"], row
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert git(world["remote"], "rev-parse", "main") == world["base"]


def test_an_unreachable_remote_during_reconciliation_is_unknown_and_exit_four(world, monkeypatch):
    check(world)
    lose_acknowledgement(monkeypatch, hide=world["remote"])
    try:
        code, receipt = merge(world)
    finally:
        away = world["remote"].with_name(world["remote"].name + ".away")
        if away.exists():
            away.rename(world["remote"])
    row = receipt["members"][0]
    assert row["state"] == "unknown" and row["final"] == "unknown", row
    assert code == merge_gate.EXIT_PARTIAL, receipt
    # the merge did land, which is why 3 ("nothing moved") would have been a false statement
    tip = git(world["remote"], "rev-parse", "main")
    assert git(world["remote"], "rev-list", "--parents", "-n", "1", tip).split()[1:] == [world["base"], world["head"]]


def test_no_further_member_is_pushed_after_an_unknown_one(slice2, monkeypatch):
    alpha, beta = slice2["members"]["alpha"], slice2["members"]["beta"]
    pushes = lose_acknowledgement(monkeypatch, hide=alpha["remote"])
    try:
        code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat")
    finally:
        away = alpha["remote"].with_name(alpha["remote"].name + ".away")
        if away.exists():
            away.rename(alpha["remote"])
    assert len(pushes) == 1, "only the first member may be pushed"
    assert receipt["members"][0]["state"] == "unknown", receipt
    assert code == merge_gate.EXIT_PARTIAL, receipt
    assert git(beta["remote"], "rev-parse", "main") == beta["base"], "beta must not have been pushed"
    assert git(alpha["remote"], "rev-list", "--parents", "-n", "1", "main").split()[1:] == [alpha["base"], alpha["head"]]


def test_an_advertisement_read_fault_is_a_named_refusal_not_a_crash(world, monkeypatch):
    check(world)
    real = merge_gate._git
    def failing(repo, *args, **kw):
        if args[:1] == ("ls-remote",) and any("__reviews__" in a for a in args):
            raise RuntimeError("ls-remote: simulated advertisement fault")
        return real(repo, *args, **kw)
    monkeypatch.setattr(merge_gate, "_git", failing)
    code, receipt = merge(world)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert receipt["members"][0]["refused"].startswith("remote_unmeasurable"), receipt
    assert git(world["remote"], "rev-parse", "main") == world["base"]


def test_a_history_read_fault_in_preflight_is_a_named_refusal_not_a_crash(world, monkeypatch):
    check(world)
    def broken(*a, **k):
        raise RuntimeError("rev-list: simulated history fault")
    monkeypatch.setattr(merge_gate, "_merged_at", broken)
    code, receipt = merge(world)
    assert receipt["members"][0]["refused"].startswith("remote_unmeasurable"), receipt
    assert git(world["remote"], "rev-parse", "main") == world["base"]


def test_a_merge_build_fault_refuses_before_any_push(world, monkeypatch):
    check(world)
    real = merge_gate._git
    def failing(repo, *args, **kw):
        if args[:1] == ("commit-tree",):
            raise RuntimeError("commit-tree: simulated build fault")
        return real(repo, *args, **kw)
    monkeypatch.setattr(merge_gate, "_git", failing)
    code, receipt = merge(world)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert receipt["members"][0]["refused"].startswith("merge_build_failed"), receipt
    assert git(world["remote"], "rev-parse", "main") == world["base"]


def test_a_same_head_merge_by_another_writer_is_attributed_to_them(world, tmp_path, monkeypatch):
    check(world)
    real_run = subprocess.run
    fired = {"n": 0}
    def run(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and "push" in cmd[:5] and not fired["n"]:
            fired["n"] = 1
            other = tmp_path / "competitor"
            git(tmp_path, "clone", "--no-local", "--branch", "main", world["remote"], other)
            git(other, "fetch", "origin", "feat")
            git(other, "merge", "--no-ff", "-m", "another writer", "FETCH_HEAD")
            git(other, "push", "origin", "main")
        return real_run(cmd, *a, **kw)
    monkeypatch.setattr(merge_gate.subprocess, "run", run)
    code, receipt = merge(world)
    row = receipt["members"][0]
    assert fired["n"] == 1
    assert row["state"] == "merged" and row["merged_by"] == "another_writer", row
    assert row.get("push_error"), row


def test_an_option_shaped_remote_never_reaches_git(world, tmp_path):
    marker = tmp_path / "RAN"
    for call in (lambda r: merge_gate._advertised(world["member"], r, "refs/heads/main"),
                 lambda r: merge_gate._remote_tip(world["member"], r, "main")):
        with pytest.raises(RuntimeError, match="remote_option_shaped"):
            call(f"--upload-pack=touch {marker}")
    assert not marker.exists()


def test_a_member_path_outside_the_workspace_is_refused(world, tmp_path, monkeypatch):
    check(world)
    outside = tmp_path / "outside"
    git(tmp_path, "clone", "-q", "--no-local", world["remote"], outside)
    real_show = merge_gate.grip.show_review_commit
    def show(*a, **k):
        view = real_show(*a, **k)
        view["members"][0]["path"] = str(outside)  # an absolute path escapes the workspace
        return view
    monkeypatch.setattr(merge_gate.grip, "show_review_commit", show)
    before = git(outside, "count-objects", "-v")
    code, receipt = merge(world)
    assert git(outside, "count-objects", "-v") == before, "the gate wrote into a repo outside the workspace"
    assert code != merge_gate.EXIT_MERGED, receipt
    assert receipt["members"][0]["refused"].startswith("member_path_outside_workspace"), receipt
    assert git(world["remote"], "rev-parse", "main") == world["base"]
