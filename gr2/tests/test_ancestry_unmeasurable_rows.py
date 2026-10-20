"""The callers of gitops.is_ancestor that act on the answer, through the real verb on a shallow clone: None is no answer.

Each row marks one commit shallow in the clone the verb reads (the line a depth-1 fetch writes to .git/shallow), so a
walk from it stops there and `merge-base --is-ancestor` exits 1 for a true ancestor. The verb must refuse with a
named, typed refusal and the fetch --unshallow line, never act on that "no"."""
from __future__ import annotations

import json

from gr2.python_cli import merge_gate
from tests.test_declared_sets import PASS, merge, record, world as set_world  # noqa: F401  (fixture)
from tests.test_review_approvals import app, approve_as, git, runner, set_policy, world  # noqa: F401  (fixtures)
from tests.test_review_merge_target_moved import REAL_RUN, gitops
from tests.test_review_reads import repin, since


def _shallow_at(repo, oid: str) -> None:
    gitdir = git(repo, "rev-parse", "--absolute-git-dir")
    with open(f"{gitdir}/shallow", "a", encoding="utf-8") as fh:
        fh.write(oid + "\n")
    assert git(repo, "rev-parse", "--is-shallow-repository") == "true"


def test_since_my_stamp_on_a_shallow_clone_refuses_approval_unmeasurable(world):
    # approvals:506: the stamped head is an ancestor of the new one, but the shallow clone cannot say so.
    assert approve_as(world, "Approver B").exit_code == 0
    old = world["head"]
    _, new = repin(world, "moved\n")
    _shallow_at(world["member"], new)
    out = since(world)
    assert out.exit_code == 5, out.output
    assert "approval_unmeasurable" in out.output and f"cannot tell whether {old} is an ancestor of {new}" in out.output
    assert f"next: git -C {world['member'].resolve()} fetch --unshallow" in out.output, out.output


def test_since_my_stamp_unshallow_line_quotes_a_repo_path_with_a_space(tmp_path, monkeypatch):
    # approvals:509: the printed fetch --unshallow line runs as printed when the member path holds a space and a ';'.
    from gr2.python_cli import approvals
    workspace = tmp_path / "work space;x"
    workspace.mkdir()
    pinned = {"members": [{"key": "m", "path": "m", "head": "a" * 40, "files": []}]}
    current = {"members": [{"key": "m", "path": "m", "head": "b" * 40, "files": []}]}
    monkeypatch.setattr(approvals, "_git", lambda *a, **kw: "Approver B")
    monkeypatch.setattr(approvals.grip, "show_review_commit", lambda ws, rid: pinned if rid == "pin" else current)
    monkeypatch.setattr(approvals.grip, "list_review_binds", lambda ws: [("pin", None)])
    monkeypatch.setattr(approvals, "count_approvals", lambda ws, pin: {"links": [{"approver": "Approver B", "commit": "c"}]})
    monkeypatch.setattr(approvals, "_read_link", lambda ws, c: {"created_at": "2026-10-20T00:00:00Z"})
    monkeypatch.setattr(approvals.gitops, "is_ancestor", lambda *a, **kw: None)
    try:
        approvals.since_my_stamp(workspace, "gr:" + "c" * 40)
    except approvals.ApprovalRefused as refused:
        message = str(refused)
    else:
        raise AssertionError("no refusal")
    repo = str((workspace / "m").resolve())
    assert f"next: git -C '{repo}' fetch --unshallow" in message, message


def test_landed_check_on_a_shallow_clone_refuses_ancestry_unmeasurable(world, monkeypatch):
    # merge_gate:816: main moves one commit past the reviewed head at the push; the shallow clone cannot tell landed.
    set_policy(world, 0)
    moved = {}

    def run(cmd, *a, **kw):
        if (not moved and isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git"
                and gitops.command_of(cmd) == "push" and str(cmd[-1]).endswith(":refs/heads/main")):
            moved["oid"] = None
            tree = git(world["remote"], "rev-parse", f"{world['head']}^{{tree}}")
            past = git(world["remote"], "commit-tree", tree, "-p", world["head"], "-m", "past")
            git(world["remote"], "update-ref", "refs/heads/main", past)
            git(world["member"], "fetch", "-q", str(world["remote"]), past)
            _shallow_at(world["member"], past)
            moved["oid"] = past
        return REAL_RUN(cmd, *a, **kw)

    monkeypatch.setattr(merge_gate.subprocess, "run", run)
    out = runner.invoke(app, ["review", "merge", "--approvals", "0"])
    assert moved["oid"], "the race never fired: no push ran"
    assert out.exit_code == merge_gate.EXIT_REFUSED, out.output
    row = json.loads(out.stdout)["members"][0]
    assert row["refused"].startswith(f"ancestry_unmeasurable: remote main moved to {moved['oid']} at the push"), row
    assert "head_already_on_target" not in row["refused"] and "target_moved_at_push" not in row["refused"], row
    assert row["next"].startswith("next: git -C ") and "fetch --unshallow" in row["next"], row


def test_pin_tie_on_a_shallow_clone_refuses_ancestry_unmeasurable(set_world):
    # merge_gate:244: beta pinned at its base, which IS in beta's main; main moved one past it and the clone is shallow.
    author, beta = set_world["author"], set_world["members"]["beta"]
    tree = git(beta["remote"], "rev-parse", f"{beta['base']}^{{tree}}")
    past = git(beta["remote"], "commit-tree", tree, "-p", beta["base"], "-m", "past")
    git(beta["remote"], "update-ref", "refs/heads/main", past)
    git(beta["repo"], "fetch", "-q", str(beta["remote"]), past)
    _shallow_at(beta["repo"], past)
    git(author, "update-index", "--cacheinfo", f"160000,{beta['base']},beta")
    git(author, "commit", "-q", "--no-verify", "-m", "beta pinned at its base")
    assert record(set_world, PASS, commit=git(author, "rev-parse", "HEAD"))["result"] == "pass"

    code, receipt = merge(set_world, "alpha")

    row = receipt["members"][0]
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert (row["refused"] or "").startswith(f"ancestry_unmeasurable: beta's pin {beta['base'][:12]}"), row
    assert "declared_set_pin_unreviewed" not in row["refused"], row
    assert row["next"] and "fetch --unshallow" in row["next"] and str(beta["repo"].resolve()) in row["next"], row
