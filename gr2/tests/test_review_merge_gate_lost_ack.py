"""`review_merge` after a push whose acknowledgement is lost: the remote is re-read, never assumed.

A transport failure after the remote accepted the push must read `merged`; an unreachable remote during
reconciliation reads `unknown`, the exit is 4, and no further member is pushed. The push is wrapped, not
replaced: the real `git push` runs, and the wrapper then reports a failure and, where asked, hides the remote.
"""
from __future__ import annotations

import json
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
    outside.mkdir()
    git(outside, "init", "-q", "-b", "main")  # an UNRELATED repo: any fetch into it would add objects
    (outside / "v.txt").write_text("victim\n")
    git(outside, "add", "v.txt")
    git(outside, "commit", "-q", "-m", "victim")
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


def test_a_bind_whose_verification_returns_false_refuses_before_any_transfer(world, monkeypatch):
    check(world)
    monkeypatch.setattr(merge_gate.grip, "verify_review_commit", lambda *a, **k: {"tree_matches": False})
    calls = []
    real_run = subprocess.run
    def run(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and any(v in cmd[:5] for v in ("fetch", "push", "ls-remote")):
            calls.append(cmd)
        return real_run(cmd, *a, **kw)
    monkeypatch.setattr(merge_gate.subprocess, "run", run)
    code, receipt = merge(world)
    assert code == merge_gate.EXIT_PARTIAL and receipt["refused"].startswith("bind_verification_failed"), receipt
    assert calls == [], "no remote transfer for an unverified bind"
    assert git(world["remote"], "rev-parse", "main") == world["base"]


def test_a_nested_directory_never_selects_its_enclosing_repo(world, monkeypatch):
    check(world)
    nested = world["member"] / "empty"
    nested.mkdir()
    real_show = merge_gate.grip.show_review_commit
    def show(*a, **k):
        view = real_show(*a, **k)
        view["members"][0]["path"] = "member/empty"  # inside the workspace, but git would select member/
        return view
    monkeypatch.setattr(merge_gate.grip, "show_review_commit", show)
    refs = git(world["member"], "for-each-ref")
    objects = git(world["member"], "count-objects", "-v")
    code, receipt = merge(world)
    assert receipt["members"][0]["refused"].startswith("member_repo_mismatch"), receipt
    assert receipt["members"][0]["final"] == "unknown"
    assert git(world["member"], "for-each-ref") == refs
    assert git(world["member"], "count-objects", "-v") == objects
    assert git(world["remote"], "rev-parse", "main") == world["base"]


def test_a_missing_bind_through_the_cli_is_a_json_receipt_not_a_traceback(world):
    from typer.testing import CliRunner
    from gr2.python_cli.app import app
    out = CliRunner().invoke(app, ["review", "merge", str(world["author"]), "gr:" + "0" * 40, "--from", "feat"])
    assert out.exit_code == merge_gate.EXIT_PARTIAL, out.output
    receipt = json.loads(out.stdout)
    assert receipt["refused"].startswith("bind_unreadable") and receipt["members"] == []


def test_a_later_member_moving_after_an_earlier_push_is_partial_not_none(slice2, monkeypatch):
    beta = slice2["members"]["beta"]
    real_run = subprocess.run
    fired = {"n": 0}
    def run(cmd, *a, **kw):
        result = real_run(cmd, *a, **kw)
        if isinstance(cmd, (list, tuple)) and "push" in cmd[:5] and not fired["n"]:
            fired["n"] = 1  # alpha has just been pushed; beta's feature moves before its re-read
            (beta["repo"] / "late.txt").write_text("late\n")
            git(beta["repo"], "add", "late.txt")
            git(beta["repo"], "commit", "-m", "late")
            git(beta["repo"], "push", beta["remote"], "feat")
        return result
    monkeypatch.setattr(merge_gate.subprocess, "run", run)
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat")
    rows = {r["key"]: r for r in receipt["members"]}
    assert rows["alpha"]["final"] == "merged", receipt
    assert rows["beta"]["refused"].startswith("feature_moved_before_push"), receipt
    assert code == merge_gate.EXIT_PARTIAL, receipt


def test_a_dot_git_file_pointing_outside_never_selects_that_store(world, tmp_path, monkeypatch):
    check(world)
    outside = tmp_path / "outside-store"
    outside.mkdir()
    git(outside, "init", "-q", "-b", "main")
    (outside / "v.txt").write_text("victim\n")
    git(outside, "add", "v.txt")
    git(outside, "commit", "-q", "-m", "victim")
    decoy = world["author"] / "decoy"
    decoy.mkdir()
    (decoy / ".git").write_text(f"gitdir: {outside / '.git'}\n")  # a worktree path whose objects live outside
    real_show = merge_gate.grip.show_review_commit
    def show(*a, **k):
        view = real_show(*a, **k)
        view["members"][0]["path"] = "decoy"
        return view
    monkeypatch.setattr(merge_gate.grip, "show_review_commit", show)
    objects = git(outside, "count-objects", "-v")
    code, receipt = merge(world)
    assert receipt["members"][0]["refused"].startswith("member_store_outside_workspace"), receipt
    assert git(outside, "count-objects", "-v") == objects
    assert git(world["remote"], "rev-parse", "main") == world["base"]


def test_an_objects_symlink_inside_an_inside_git_dir_is_refused(world, tmp_path, monkeypatch):
    check(world)
    outside = tmp_path / "outside-objects"
    outside.mkdir()
    git(outside, "init", "-q", "-b", "main")
    (outside / "v.txt").write_text("victim\n")
    git(outside, "add", "v.txt")
    git(outside, "commit", "-q", "-m", "victim")
    objects = world["member"] / ".git" / "objects"
    git(world["member"], "repack", "-a", "-d", "-q")
    for item in objects.iterdir():  # move this member's objects into the outside store, then link to it
        target = outside / ".git" / "objects" / item.name
        if item.is_dir():
            target.mkdir(exist_ok=True)
            for f in item.iterdir():
                f.rename(target / f.name)
        elif not target.exists():
            item.rename(target)
    import shutil
    shutil.rmtree(objects)
    objects.symlink_to(outside / ".git" / "objects")
    assert git(world["member"], "rev-parse", "HEAD")  # the member still works through the link
    before = git(outside, "count-objects", "-v")
    code, receipt = merge(world)
    assert receipt["members"][0]["refused"].startswith("member_store_outside_workspace"), receipt
    assert git(outside, "count-objects", "-v") == before
    assert git(world["remote"], "rev-parse", "main") == world["base"]


def test_a_symlinked_fanout_dir_under_a_real_objects_dir_is_refused(world, tmp_path):
    check(world)
    outside = tmp_path / "outside-fanout"
    outside.mkdir()
    git(outside, "init", "-q", "-b", "main")
    (outside / "v.txt").write_text("victim\n")
    git(outside, "add", "v.txt")
    git(outside, "commit", "-q", "-m", "victim")
    objects = world["member"] / ".git" / "objects"
    git(world["member"], "repack", "-a", "-d", "-q")  # loose fan-out dirs are now empty; replace each with a link
    for i in range(256):
        name = f"{i:02x}"
        here, there = objects / name, outside / ".git" / "objects" / name
        there.mkdir(exist_ok=True)
        if here.exists():
            for f in here.iterdir():
                f.rename(there / f.name)
            here.rmdir()
        here.symlink_to(there)
    assert git(world["member"], "rev-parse", "HEAD")
    before = git(outside, "count-objects", "-v")
    code, receipt = merge(world)
    assert receipt["members"][0]["refused"].startswith("member_store_outside_workspace"), receipt
    assert git(outside, "count-objects", "-v") == before
    assert git(world["remote"], "rev-parse", "main") == world["base"]


def test_a_symlinked_reflog_file_is_refused_and_never_appended_to(world, tmp_path, monkeypatch):
    check(world)
    git(world["member"], "config", "core.logAllRefUpdates", "always")
    fixed = merge_gate.uuid.UUID(int=0x1138)
    monkeypatch.setattr(merge_gate.uuid, "uuid4", lambda: fixed)  # deterministic staging ref, so its log can be planted
    victim = tmp_path / "outside-reflog"
    victim.write_bytes(b"victim\n")
    logdir = world["member"] / ".git" / "logs" / "refs" / "dev.synapt.grip" / "__merge_transfers__"
    logdir.mkdir(parents=True)
    (logdir / fixed.hex).symlink_to(victim)
    code, receipt = merge(world)
    assert receipt["members"][0]["refused"].startswith("member_store_outside_workspace"), receipt
    assert victim.read_bytes() == b"victim\n", "the gate appended a reflog outside the workspace"
    assert git(world["remote"], "rev-parse", "main") == world["base"]


def test_an_ordinary_reflog_is_not_refused(world):
    check(world)
    git(world["member"], "config", "core.logAllRefUpdates", "always")
    code, receipt = merge(world)
    assert code == merge_gate.EXIT_MERGED, receipt


def test_a_symlinked_hook_does_not_refuse_a_member(world, tmp_path):
    check(world)
    hook = tmp_path / "commit-msg-guard"
    hook.write_text("#!/bin/sh\nexit 0\n")
    (world["member"] / ".git" / "hooks" / "commit-msg").symlink_to(hook)  # how our own clones install guards
    code, receipt = merge(world)
    assert code == merge_gate.EXIT_MERGED, receipt
