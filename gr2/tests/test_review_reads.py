"""The read-only review verbs. `review blockers` is the merge gate's preflight and stops before any build or
push; `review stamps` reads the approval chain. Neither moves a remote ref."""
import json

from gr2.python_cli import approvals
from gr2.python_cli.app import app
from tests.test_review_approvals import approve_as, git, runner, set_policy, world  # noqa: F401  (world is a fixture)


def refs(remote):
    return git(remote, "for-each-ref", "--format=%(objectname) %(refname)")


def test_blockers_names_what_merge_refuses_and_pushes_nothing(world):
    set_policy(world, 2)
    assert approve_as(world, "Approver B").exit_code == 0
    before = refs(world["remote"])
    blocked = runner.invoke(app, ["review", "blockers"])
    assert blocked.exit_code == 3, blocked.output
    receipt = json.loads(blocked.stdout)
    assert receipt["dry_run"] is True and receipt["ready"] is False
    assert [b["code"] for b in receipt["blockers"]] == ["approvals_insufficient"]
    assert refs(world["remote"]) == before  # byte-equal: nothing built, pushed or recorded on the remote
    merged = runner.invoke(app, ["review", "merge"])
    assert merged.exit_code == 3
    assert [r["refusal"]["code"] for r in json.loads(merged.stdout)["members"]] == ["approvals_insufficient"]
    assert approve_as(world, "Approver C").exit_code == 0
    before = refs(world["remote"])
    ready = runner.invoke(app, ["review", "blockers"])
    assert ready.exit_code == 0, ready.output
    assert json.loads(ready.stdout)["blockers"] == [] and json.loads(ready.stdout)["ready"] is True
    assert refs(world["remote"]) == before
    assert runner.invoke(app, ["review", "merge"]).exit_code == 0  # ready meant ready


def test_blockers_matches_merge_on_a_moved_feature(world):
    set_policy(world, 0)
    member, remote = world["member"], world["remote"]
    (member / "payload").write_text("moved\n")
    git(member, "commit", "-am", "moved")
    git(member, "push", remote, "feature")
    before = refs(remote)
    gid = "gr:" + world["rid"]
    blocked = runner.invoke(app, ["review", "blockers", gid, "--into", "main", "--from", "feature"])
    assert blocked.exit_code == 3, blocked.output
    codes = [b["code"] for b in json.loads(blocked.stdout)["blockers"]]
    assert refs(remote) == before
    merged = runner.invoke(app, ["review", "merge", gid, "--into", "main", "--from", "feature"])
    assert codes == [r["refusal"]["code"] for r in json.loads(merged.stdout)["members"] if r["refusal"]] == ["feature_moved"]


def test_stamps_reads_who_and_when_and_writes_nothing(world):
    for name in ("Approver B", "Approver C", "Approver B"):
        assert approve_as(world, name).exit_code == 0
    before = refs(world["remote"])
    read = runner.invoke(app, ["review", "stamps"])
    assert read.exit_code == 0, read.output
    out = json.loads(read.stdout)
    assert out["count"] == approvals.count_approvals(world["workspace"], world["rid"])["count"] == 2
    assert {s["approver"] for s in out["stamps"]} == {"Approver B", "Approver C"}
    assert all(s["created_at"].endswith("Z") and len(s["link"]) == 40 for s in out["stamps"])
    assert out["members"] == [{"key": out["members"][0]["key"], "head": world["head"]}]
    assert (out["required"], out["required_state"]) == (None, "unset")  # dev refuses approvals_unset; the read reports it
    assert refs(world["remote"]) == before


def test_stamps_reports_the_set_policy(world):
    set_policy(world, 2)
    assert approve_as(world, "Approver B").exit_code == 0
    read = runner.invoke(app, ["review", "stamps"])
    assert read.exit_code == 0, read.output
    out = json.loads(read.stdout)
    assert (out["required"], out["required_state"], out["count"]) == (2, "set", 1)


def test_stamps_still_refuses_an_invalid_policy(world):
    set_policy(world, -1)
    read = runner.invoke(app, ["review", "stamps"])
    assert read.exit_code == 5 and "approval_policy_invalid" in read.stdout


def test_merged_reads_the_remote_not_the_local_receipt(world):
    set_policy(world, 0)
    gid = "gr:" + world["rid"]
    before = runner.invoke(app, ["review", "merged", gid, "--into", "main"])
    assert before.exit_code == 3 and json.loads(before.stdout)["members"][0]["merged"] is None
    assert runner.invoke(app, ["review", "merge"]).exit_code == 0
    merge = git(world["remote"], "rev-parse", "main")
    # A clone that never ran the merge has no receipt: drop this one's and the remote must still answer.
    for ref in git(world["workspace"], "for-each-ref", "--format=%(refname)", "refs/dev.synapt.grip/__merges__").split():
        git(world["workspace"], "update-ref", "-d", ref)
    read = runner.invoke(app, ["review", "merged", gid, "--into", "main"])
    assert read.exit_code == 0, read.output
    out = json.loads(read.stdout)
    assert out["receipt_here"] is False
    row = out["members"][0]
    assert row["merged"] == merge and row["merged_at"] == git(world["remote"], "show", "-s", "--format=%cI", merge)


def test_merged_bare_after_merge_resolves_into(world):
    set_policy(world, 0)
    assert runner.invoke(app, ["review", "merge"]).exit_code == 0
    read = runner.invoke(app, ["review", "merged"])  # no id, no --into: the merge's own precondition is false now
    assert read.exit_code == 0, read.output
    out = json.loads(read.stdout)
    assert out["into"] == "main" and out["receipt_here"] is True
    assert out["members"][0]["merged"] and out["members"][0]["merged_at"]
    assert "(receipt)" in read.stderr


def test_merged_bare_without_a_receipt_reads_the_remote_default(world, monkeypatch):
    set_policy(world, 0)
    assert runner.invoke(app, ["review", "merge"]).exit_code == 0
    monkeypatch.setattr("gr2.python_cli.merge_receipts.runs", lambda ws, rid: [])  # a clone that never merged
    read = runner.invoke(app, ["review", "merged"])
    assert read.exit_code == 0, read.output
    out = json.loads(read.stdout)
    assert out["into"] == "main" and out["members"][0]["merged_at"]
    assert "(remote default)" in read.stderr


def test_merged_bare_before_merge_reads_none_merged(world):
    read = runner.invoke(app, ["review", "merged"])  # the default branch does not hold the head yet
    assert read.exit_code == 3, read.output
    assert json.loads(read.stdout)["into"] == "main"
    assert "(remote default)" in read.stderr


def test_merged_bare_after_main_moves_reads_none_merged(world):
    seed = world["workspace"].parent / "seed"  # main moves under the review, the ordinary state between pin and merge
    (seed / "other").write_text("unrelated\n")
    git(seed, "add", "other")
    git(seed, "commit", "-m", "unrelated")
    git(seed, "push", world["remote"], "main")
    read = runner.invoke(app, ["review", "merged"])
    assert read.exit_code == 3, read.output
    out = json.loads(read.stdout)
    assert out["into"] == "main" and out["members"][0]["merged"] is None
    assert "(remote default)" in read.stderr
