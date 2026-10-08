"""Unsigned approval-chain proof through the default CLI, using real bare Git remotes."""
import json
import subprocess

from typer.testing import CliRunner

from gr2.python_cli import approvals
from gr2.python_cli.app import app

runner = CliRunner()


def git(root, *args):
    p = subprocess.run(["git", "-C", str(root), *map(str, args)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def test_default_approve_counts_two_names_and_refuses_the_author(tmp_path, monkeypatch):
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
