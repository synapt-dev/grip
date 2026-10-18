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
