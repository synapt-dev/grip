"""`review merge` when the target moves after the gate read it and before the push lands.

The lease on the reviewed base loses, nothing is pushed, and the receipt must name why: `target_moved_at_push`, the
base the gate read and the oid now on the remote, plus the next command. A bare `unmerged` with `refused: null` left
the user nothing to act on. With an approval count the stamp read refuses `base_moved` first; that path is unchanged.
"""
from __future__ import annotations

import json
import subprocess

from gr2.python_cli import gitops, merge_gate
from tests.test_review_approvals import app, approve_as, git, runner, set_policy, world  # noqa: F401  (fixtures)

REAL_RUN = subprocess.run


def _move_main(w) -> str:
    seed = w["workspace"].parent / "mover"
    git(w["workspace"].parent, "clone", "-q", w["remote"], seed)
    (seed / "other").write_text("moved\n")
    git(seed, "add", "other")
    git(seed, "commit", "-q", "-m", "moved")
    git(seed, "push", "-q", w["remote"], "main")
    return git(seed, "rev-parse", "HEAD")


def move_main_before_the_push(monkeypatch, w) -> dict:
    """Run every git command for real; at the first push, move the remote's main first, so the gate's reads saw base."""
    moved = {}

    def run(cmd, *a, **kw):
        if (isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git" and gitops.command_of(cmd) == "push"
                and str(cmd[-1]).endswith(":refs/heads/main") and not moved):
            moved["oid"] = None  # the mover's own push runs through this wrapper too
            moved["oid"] = _move_main(w)
        return REAL_RUN(cmd, *a, **kw)

    monkeypatch.setattr(merge_gate.subprocess, "run", run)
    return moved


def test_target_moved_at_push_names_the_refusal_both_oids_and_a_next_step(world, monkeypatch):
    set_policy(world, 0)
    moved = move_main_before_the_push(monkeypatch, world)
    out = runner.invoke(app, ["review", "merge", "--approvals", "0"])
    assert out.exit_code == merge_gate.EXIT_REFUSED, out.output
    row = json.loads(out.stdout)["members"][0]
    assert moved, "the race never fired: no push ran"
    assert git(world["remote"], "rev-parse", "main") == moved["oid"], "the merge must not land on the moved target"
    assert row["state"] == "unmerged" and row["final"] == "unmerged", row
    assert row["refused"] == f"target_moved_at_push: remote main is {moved['oid']}, the gate read {world['base']}", row
    assert row["refusal"] == {"code": "target_moved_at_push", "member": row["key"],
                              "expected": world["base"], "observed": moved["oid"]}, row
    assert "gr2 review update" in row["next"] and "--into main" in row["next"], row


def test_approvals_one_with_the_target_moved_still_reads_base_moved(world, monkeypatch):
    set_policy(world, 1)
    assert approve_as(world, "Approver B").exit_code == 0
    moved = _move_main(world)
    out = runner.invoke(app, ["review", "merge", "--approvals", "1"])
    assert out.exit_code == merge_gate.EXIT_REFUSED, out.output
    row = json.loads(out.stdout)["members"][0]
    assert row["refused"].startswith(f"base_moved: remote main is {moved}"), row
    assert "target_moved_at_push" not in out.stdout, row
    assert git(world["remote"], "rev-parse", "main") == moved


# --- v2: the loop stops at the first member whose push failed, and every such member says why ---------------------

from tests.test_review_merge_gate import git as mgit, record_set, slice2  # noqa: E402,F401  (fixtures)

ODD = " sp‮\x1b[31m\x85end"  # a hook's message that must not reach the terminal raw


def _wrap(monkeypatch, at_push):
    """Run every git command for real; `at_push(remote)` runs once per member push, before it."""
    pushed, busy = [], []

    def run(cmd, *a, **kw):
        if (not busy and isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git"
                and gitops.command_of(cmd) == "push" and str(cmd[-1]).endswith(":refs/heads/main")):
            pushed.append(str(cmd[-2]))
            busy.append(1)
            try:
                at_push(str(cmd[-2]))
            finally:
                busy.clear()
        return REAL_RUN(cmd, *a, **kw)

    monkeypatch.setattr(merge_gate.subprocess, "run", run)
    return pushed


def _move_bare(remote) -> str:
    """Move a bare remote's main by one commit, as another writer would."""
    tree = mgit(remote, "rev-parse", "main^{tree}")
    moved = mgit(remote, "commit-tree", tree, "-p", "main", "-m", "another writer")
    mgit(remote, "update-ref", "refs/heads/main", moved)
    return moved


def _refuse_pushes(remote, message=ODD):
    hook = remote / "hooks" / "pre-receive"
    assert "'" not in message
    hook.write_text("#!/bin/sh\nwhile read old new ref; do\n  if [ \"$ref\" = refs/heads/main ]; then\n"
                    f"    printf '%s\\n' '{message}' >&2; exit 1\n  fi\ndone\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)


def _merge2(s, before=lambda: None):
    record_set(s)
    before()
    return merge_gate.review_merge(s["author"], s["review"], feature="feat", required_approvals=0)


def _rows(receipt):
    return {r["key"]: r for r in receipt["members"]}


def test_first_member_lease_lost_pushes_no_further_member(slice2, monkeypatch):
    a, b = slice2["members"]["alpha"], slice2["members"]["beta"]
    moved = {}
    pushed = _wrap(monkeypatch, lambda remote: moved.setdefault(remote, _move_bare(a["remote"]))
                   if remote == str(a["remote"]) else None)
    code, receipt = _merge2(slice2)
    rows = _rows(receipt)
    assert pushed == [str(a["remote"])], f"beta was pushed after alpha's lease was lost: {pushed}"
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert rows["alpha"]["refusal"]["code"] == "target_moved_at_push", rows["alpha"]
    assert rows["alpha"]["refusal"]["observed"] == moved[str(a["remote"])], rows["alpha"]
    assert rows["beta"]["final"] == "unmerged" and mgit(b["remote"], "rev-parse", "main") == b["base"], rows["beta"]


def test_second_member_lease_lost_leaves_the_first_merged_and_names_both(slice2, monkeypatch):
    a, b = slice2["members"]["alpha"], slice2["members"]["beta"]
    moved = {}
    pushed = _wrap(monkeypatch, lambda remote: moved.setdefault(remote, _move_bare(b["remote"]))
                   if remote == str(b["remote"]) else None)
    code, receipt = _merge2(slice2)
    rows = _rows(receipt)
    assert pushed == [str(a["remote"]), str(b["remote"])], pushed
    assert code == merge_gate.EXIT_PARTIAL, receipt
    assert rows["alpha"]["final"] == "merged" and rows["alpha"]["refused"] is None, rows["alpha"]
    assert rows["beta"]["final"] == "unmerged", rows["beta"]
    assert rows["beta"]["refusal"] == {"code": "target_moved_at_push", "member": "beta", "expected": b["base"],
                                       "observed": moved[str(b["remote"])]}, rows["beta"]
    assert "gr2 review update" in rows["beta"]["next"], rows["beta"]


def test_first_member_push_rejected_names_git_escaped_and_pushes_no_further_member(slice2, monkeypatch):
    a, b = slice2["members"]["alpha"], slice2["members"]["beta"]
    pushed = _wrap(monkeypatch, lambda remote: None)
    code, receipt = _merge2(slice2, lambda: _refuse_pushes(a["remote"]))
    rows = _rows(receipt)
    assert pushed == [str(a["remote"])], f"beta was pushed after alpha's push was rejected: {pushed}"
    assert code == merge_gate.EXIT_REFUSED, receipt
    refused = rows["alpha"]["refused"]
    assert refused.startswith("push_rejected: ") and "pre-receive hook declined" in refused, refused
    assert " sp\\u202e\\x1b[31m\\x85end" in refused, refused
    assert not any(c in refused for c in "‮\x1b\x85\n"), refused
    assert rows["alpha"]["refusal"]["code"] == "push_rejected", rows["alpha"]
    assert "the remote refused the push" in rows["alpha"]["next"], rows["alpha"]
    assert mains_at_base(slice2)


def mains_at_base(s):
    return all(mgit(m["remote"], "rev-parse", "main") == m["base"] for m in s["members"].values())


def test_single_member_push_rejected_is_refused_exit_3(world, monkeypatch):
    set_policy(world, 0)
    _refuse_pushes(world["remote"], "protected branch")
    out = runner.invoke(app, ["review", "merge", "--approvals", "0"])
    assert out.exit_code == merge_gate.EXIT_REFUSED, out.output
    row = json.loads(out.stdout)["members"][0]
    assert row["final"] == "unmerged" and row["refused"].startswith("push_rejected: "), row
    assert "protected branch" in row["refused"] and row["next"], row


def test_single_member_target_unread_after_a_failed_push(world, monkeypatch):
    set_policy(world, 0)
    _refuse_pushes(world["remote"])
    seen = []

    def run(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git":
            verb = gitops.command_of(cmd)
            if verb == "push":
                seen.append(1)
            elif seen and verb == "ls-remote":
                raise OSError("remote unreachable")
        return REAL_RUN(cmd, *a, **kw)

    monkeypatch.setattr(merge_gate.subprocess, "run", run)
    out = runner.invoke(app, ["review", "merge", "--approvals", "0"])
    assert out.exit_code == merge_gate.EXIT_PARTIAL, out.output
    row = json.loads(out.stdout)["members"][0]
    assert row["refused"] == "target_unread_at_push: OSError: remote unreachable", row
    assert "gr2 review merged" in row["next"], row


def test_single_member_target_deleted_at_the_push(world, monkeypatch):
    set_policy(world, 0)
    _wrap(monkeypatch, lambda remote: git(world["remote"], "update-ref", "-d", "refs/heads/main"))
    out = runner.invoke(app, ["review", "merge", "--approvals", "0"])
    assert out.exit_code == merge_gate.EXIT_PARTIAL, out.output
    row = json.loads(out.stdout)["members"][0]
    assert row["refused"] == f"target_deleted_at_push: remote main is gone, the gate read {world['base']}", row
    assert row["refusal"]["expected"] == world["base"] and row["refusal"]["observed"] is None, row
    assert "restore it" in row["next"], row


# --- v3: a writer moved the target to the reviewed head (or past it) with no merge commit -------------------------

def _ff_main(w, past: bool) -> str:
    """Move the bare remote's main to the reviewed head, or one commit past it, as a fast-forward writer would."""
    to = w["head"]
    if past:
        to = git(w["remote"], "commit-tree", git(w["remote"], "rev-parse", f"{to}^{{tree}}"), "-p", to, "-m", "past")
    git(w["remote"], "update-ref", "refs/heads/main", to)
    return to


def _assert_already_on_target(out, w, now):
    assert out.exit_code == merge_gate.EXIT_REFUSED, out.output
    row = json.loads(out.stdout)["members"][0]
    assert row["refused"] == f"head_already_on_target: the reviewed head {w['head']} is already in main at {now}", row
    assert row["refusal"] == {"code": "head_already_on_target", "member": row["key"],
                              "expected": w["base"], "observed": now}, row
    assert row["final"] == "unmerged" and git(w["remote"], "rev-parse", "main") == now, row
    # Never the update line: it pins the same head again and dead-ends at head_already_on_remote.
    assert row["next"].startswith("next: nothing to merge") and "review update" not in row["next"], row


def _ff_at_push(monkeypatch, w, past):
    moved = {}

    def run(cmd, *a, **kw):
        if (not moved and isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git"
                and gitops.command_of(cmd) == "push" and str(cmd[-1]).endswith(":refs/heads/main")):
            moved["oid"] = None
            moved["oid"] = _ff_main(w, past)
        return REAL_RUN(cmd, *a, **kw)

    monkeypatch.setattr(merge_gate.subprocess, "run", run)
    return moved


def test_ff_to_head_at_the_push_reads_head_already_on_target(world, monkeypatch):
    set_policy(world, 0)
    moved = _ff_at_push(monkeypatch, world, past=False)
    out = runner.invoke(app, ["review", "merge", "--approvals", "0"])
    assert moved["oid"] == world["head"], moved
    _assert_already_on_target(out, world, world["head"])


def test_one_past_head_at_the_push_reads_head_already_on_target(world, monkeypatch):
    set_policy(world, 0)
    moved = _ff_at_push(monkeypatch, world, past=True)
    out = runner.invoke(app, ["review", "merge", "--approvals", "0"])
    assert moved["oid"] not in (None, world["head"]), moved
    _assert_already_on_target(out, world, moved["oid"])


def test_ff_to_head_before_the_gate_under_approvals_one_reads_head_already_on_target(world):
    set_policy(world, 1)
    assert approve_as(world, "Approver B").exit_code == 0
    now = _ff_main(world, past=False)
    _assert_already_on_target(runner.invoke(app, ["review", "merge", "--approvals", "1"]), world, now)


def test_one_past_head_before_the_gate_under_approvals_one_reads_head_already_on_target(world):
    set_policy(world, 1)
    assert approve_as(world, "Approver B").exit_code == 0
    now = _ff_main(world, past=True)
    _assert_already_on_target(runner.invoke(app, ["review", "merge", "--approvals", "1"]), world, now)


def test_a_target_moved_off_the_head_line_still_reads_target_moved_at_push(world, monkeypatch):
    # The ancestry check must not swallow an ordinary move: main moved by a commit that does not hold the head.
    set_policy(world, 0)
    moved = move_main_before_the_push(monkeypatch, world)
    out = runner.invoke(app, ["review", "merge", "--approvals", "0"])
    assert json.loads(out.stdout)["members"][0]["refused"].startswith("target_moved_at_push"), out.stdout
    assert moved["oid"]
