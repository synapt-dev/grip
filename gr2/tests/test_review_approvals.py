"""Unsigned approval-chain proof through the default CLI, using real bare Git remotes."""
import json
import subprocess
import sys

import pytest

from typer.testing import CliRunner

from gr2.python_cli import approvals, check_records
from gr2.python_cli.app import app

runner = CliRunner()


def git(root, *args):
    p = subprocess.run(["git", "-C", str(root), *map(str, args)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    for key, value in {"GIT_AUTHOR_NAME": "Author A", "GIT_AUTHOR_EMAIL": "a@example.invalid",
                       "GIT_COMMITTER_NAME": "Author A", "GIT_COMMITTER_EMAIL": "a@example.invalid",
                       "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}.items():
        monkeypatch.setenv(key, value)
    remote, seed, workspace = tmp_path / "remote.git", tmp_path / "seed", tmp_path / "workspace"
    git(tmp_path, "init", "--bare", "-b", "main", remote)
    seed.mkdir()
    git(seed, "init", "-b", "main")
    (seed / "payload").write_text("base\n")
    git(seed, "add", "payload")
    git(seed, "commit", "-m", "base")
    base = git(seed, "rev-parse", "HEAD")
    git(seed, "push", remote, "main")
    workspace.mkdir()
    member = workspace / "member"
    git(workspace, "clone", remote, member)
    init = runner.invoke(app, ["store", "init", str(workspace)])
    assert init.exit_code == 0, init.output
    git(member, "checkout", "-b", "feature")
    (member / "payload").write_text("reviewed\n")
    git(member, "add", "payload")
    git(member, "commit", "-m", "feature")
    head = git(member, "rev-parse", "HEAD")
    monkeypatch.chdir(workspace)
    git(workspace, "config", "user.name", "Author A")
    git(workspace, "config", "user.email", "a@example.invalid")
    bound = runner.invoke(app, ["review", "bind", "--repo", "member", "--remote", str(remote),
                               "--base", base, "--head", head, "--ref", "refs/heads/main", "--source", str(member)])
    assert bound.exit_code == 0, bound.output
    rid = approvals.current_review(workspace)
    published = runner.invoke(app, ["review", "publish", "gr:" + rid, "--remote", str(remote)])
    assert published.exit_code == 0, published.output
    git(member, "push", remote, "feature")
    return dict(workspace=workspace, member=member, remote=remote, base=base, head=head, rid=rid)


def approve_as(world, name):
    git(world["workspace"], "config", "user.name", name)
    return runner.invoke(app, ["review", "approve"])


def set_policy(world, required):
    policy = world["workspace"] / "grip.toml"
    policy.write_text(policy.read_text() + f"\n[approvals]\nrequired = {required}\n")
    check_records.run_check(world["member"], str(world["remote"]), world["head"], "test", [sys.executable, "-c", "pass"])


def test_default_approve_counts_two_names_and_refuses_the_author(world):
    workspace, remote, rid, base = (world[k] for k in ("workspace", "remote", "rid", "base"))
    refused = runner.invoke(app, ["review", "approve"])
    assert refused.exit_code == 2 and "self_approval" in refused.output, refused.output
    for name, count in [("Approver B", 1), ("Approver C", 2)]:
        git(workspace, "config", "user.name", name)
        result = runner.invoke(app, ["review", "approve"])
        assert result.exit_code == 0, result.output
        receipt = json.loads(result.output)
        assert receipt["count"] == count, receipt
        assert receipt["signed"] is False
    observed = approvals.count_approvals(workspace, rid)
    assert [link["approver"] for link in observed["links"]] == ["Approver B", "Approver C"]
    tip = git(remote, "rev-parse", approvals.PREFIX + rid)
    assert git(remote, "rev-parse", tip + "~2") == rid
    assert git(remote, "rev-parse", "main") == base
    print(json.dumps({"proof": "approve_plus_count", "identities": ["Author A", "Approver B", "Approver C"],
                      "review": rid, "tip": tip, "count": observed["count"], "signed": False}))


def test_default_merge_reads_workspace_policy_and_counts_two_approvers(world):
    set_policy(world, 2)
    assert approve_as(world, "Approver B").exit_code == 0
    insufficient = runner.invoke(app, ["review", "merge"])
    receipt = json.loads(insufficient.output)
    assert insufficient.exit_code == 3, insufficient.output
    assert receipt["members"][0]["refused"].startswith("approvals_insufficient")
    assert receipt["approvals"]["count"] == 1 and receipt["approvals"]["required"] == 2
    assert git(world["remote"], "rev-parse", "main") == world["base"]
    assert approve_as(world, "Approver C").exit_code == 0
    merged = runner.invoke(app, ["review", "merge"])
    receipt = json.loads(merged.output)
    assert merged.exit_code == 0, merged.output
    assert receipt["approvals"]["count"] == 2 and receipt["approvals"]["required"] == 2
    assert {link["approver"] for link in receipt["approvals"]["links"]} == {"Approver B", "Approver C"}
    assert git(world["remote"], "rev-list", "--parents", "-n", "1", "main").split()[1:] == [world["base"], world["head"]]


def test_duplicate_approver_counts_once_and_cannot_satisfy_two(world):
    set_policy(world, 2)
    for _ in range(2):
        result = approve_as(world, "Approver B")
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["count"] == 1
    merged = runner.invoke(app, ["review", "merge"])
    assert merged.exit_code == 3, merged.output
    assert json.loads(merged.output)["members"][0]["refused"].startswith("approvals_insufficient")
    assert git(world["remote"], "rev-parse", "main") == world["base"]


def test_broken_prev_refuses_without_moving_target(world):
    set_policy(world, 1)
    result = approve_as(world, "Approver B")
    assert result.exit_code == 0, result.output
    tip = json.loads(result.output)["tip"]
    record = json.loads(git(world["remote"], "show", tip + ":approval.json"))
    record["prev"] = "0" * 64
    blob = approvals._git(world["workspace"], "hash-object", "-w", "--stdin", data=approvals.canonical(record))
    tree = approvals._git(world["workspace"], "mktree", data=f"100644 blob {blob}\tapproval.json\n")
    damaged = git(world["workspace"], "commit-tree", tree, "-p", world["rid"], "-m", "damaged prev")
    ref = approvals.PREFIX + world["rid"]
    git(world["workspace"], "push", "--force", world["remote"], damaged + ":" + ref)
    merged = runner.invoke(app, ["review", "merge"])
    assert merged.exit_code == 3, merged.output
    assert json.loads(merged.output)["members"][0]["refused"].startswith("approval_chain_broken: prev")
    assert git(world["remote"], "rev-parse", "main") == world["base"]


def test_moved_head_cannot_reuse_the_old_chain(world):
    set_policy(world, 2)
    assert approve_as(world, "Approver B").exit_code == 0
    assert approve_as(world, "Approver C").exit_code == 0
    old = git(world["remote"], "rev-parse", approvals.PREFIX + world["rid"])
    (world["member"] / "payload").write_text("moved\n")
    git(world["member"], "add", "payload")
    git(world["member"], "commit", "-m", "moved")
    git(world["member"], "push", world["remote"], "feature")
    default = runner.invoke(app, ["review", "merge"])
    assert default.exit_code == 4 and "approval_bind_not_unique" in default.output
    explicit = runner.invoke(app, ["review", "merge", "gr:" + world["rid"]])
    assert explicit.exit_code == 3, explicit.output
    assert json.loads(explicit.output)["members"][0]["refused"].startswith("feature_moved")
    assert git(world["remote"], "rev-parse", "main") == world["base"]
    assert git(world["remote"], "rev-parse", approvals.PREFIX + world["rid"]) == old
