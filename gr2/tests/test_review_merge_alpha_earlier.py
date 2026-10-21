"""A review merge after alpha merged in an earlier run: the approval count never reads alpha's base as beta's.

Staged on local bare remotes: alpha's head is merged into its remote's main before the run (an earlier run, or a
merge by hand). One approval is required, and beta's refusal must be its own, never alpha's moved base.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from gr2.python_cli.app import app
from tests.test_review_merge_gate import record_set, _git_identity, git, publish, slice2  # noqa: F401  (fixtures)


def _half_land_target_moved(slice2, moves=(("alpha", "land"), ("beta", "move")), approvals="0"):
    """alpha merged on its remote before the run; beta's target moved after the pin. Returns (run, rows)."""
    record_set(slice2)
    for key, how in moves:
        remote, head = slice2["members"][key]["remote"], slice2["members"][key]["head"]
        main = git(remote, "rev-parse", "main")
        tree = git(remote, "merge-tree", "--write-tree", "main", head).split()[0] if how == "land" \
            else git(remote, "rev-parse", "main^{tree}")
        parents = ["-p", main, "-p", head] if how == "land" else ["-p", main]
        git(remote, "update-ref", "refs/heads/main", git(remote, "commit-tree", tree, *parents, "-m", how))
    ran = CliRunner().invoke(app, ["review", "merge", "--approvals", approvals, str(slice2["author"]), slice2["review"],
                                   "--from", "feat", "--into", "main"])
    return ran, {r["key"]: r for r in json.loads(ran.stdout)["members"]}


def test_a_ready_members_target_moving_before_the_count_is_its_own_base_moved(slice2, monkeypatch):
    """alpha merged before the run; beta's target moves after the gate's own read and before the count (a push
    racing the run): the count still checks beta's base, skipping only the merged alpha, so beta is base_moved."""
    from gr2.python_cli import approvals

    real, merged = approvals.count_approvals, []

    def racing(*a, **k):
        remote = slice2["members"]["beta"]["remote"]
        main = git(remote, "rev-parse", "main")
        git(remote, "update-ref", "refs/heads/main",
            git(remote, "commit-tree", git(remote, "rev-parse", "main^{tree}"), "-p", main, "-m", "race"))
        merged.append(k.get("merged"))
        return real(*a, **k)
    monkeypatch.setattr(approvals, "count_approvals", racing)
    ran, rows = _half_land_target_moved(slice2, moves=(("alpha", "land"),), approvals="1")
    assert ran.exit_code == 4, ran.output
    assert merged == [frozenset({"alpha"})], merged
    assert rows["alpha"]["state"] == "already_merged", rows
    assert rows["beta"]["refusal"]["code"] == "base_moved", rows


def test_a_merged_members_moved_base_is_not_the_ready_members_refusal(slice2):
    """alpha merged before the run, beta untouched, one approval required: the count must not refuse alpha's base
    (moved by its own merge) and copy that onto beta; beta's refusal is its own, the missing approval."""
    ran, rows = _half_land_target_moved(slice2, moves=(("alpha", "land"),), approvals="1")
    assert ran.exit_code == 4, ran.output
    assert rows["alpha"]["state"] == "already_merged", rows
    assert rows["beta"]["refused"] == "approvals_insufficient: 0 of 1", rows
    assert rows["beta"]["refusal"]["code"] == "approvals_insufficient", rows
    assert "review update" not in (rows["beta"].get("next") or ""), rows["beta"]
