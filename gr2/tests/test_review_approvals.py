"""Unsigned approval-chain proof through the default CLI, using real bare Git remotes."""
import json
import subprocess
import sys
from pathlib import Path

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


def _sample_link():
    root = {"review_id": "gr:" + "a" * 40, "author": "Author A", "members": [
        {"key": "m", "remote": "remote.git", "head_commit": "b" * 40, "head_tree": "c" * 40}]}
    record = {**root, "schema": approvals.SCHEMA, "approver": {"name": "Approver B", "key_id": ""},
              "verdict": "approve", "prev": "d" * 64, "created_at": "2026-01-01T00:00:00Z"}
    return root, record


@pytest.mark.parametrize("case,reason", [
    ("missing", "missing or unknown fields"), ("unknown", "missing or unknown fields"),
    ("schema", "schema or verdict"), ("verdict", "schema or verdict"),
    ("bind", "bind or head pins"), ("author", "bind or head pins"),
    ("head", "bind or head pins"), ("tree", "bind or head pins"), ("remote", "bind or head pins"),
    ("approver_shape", "approver fields"), ("name_empty", "invalid approver name"),
    ("name_control", "invalid approver name"), ("self", "self_approval"),
    ("range_author", "self_approval"), ("signature", "approval_signed_unsupported"),
    ("empty_signature", "approval_signed_unsupported"), ("key", "approval_signed_unsupported"),
    ("keyring", "approval_signed_unsupported"), ("digest", "invalid prev digest"),
    ("timestamp", "invalid created_at"), ("date", "invalid created_at"),
])
def test_malformed_link_fields_refuse(case, reason):
    root, record = _sample_link()
    authors = {"Author A", "Range Author"}
    if case == "missing": del record["created_at"]
    elif case == "unknown": record["unexpected"] = True
    elif case == "schema": record["schema"] = "future"
    elif case == "verdict": record["verdict"] = "block"
    elif case == "bind": record["review_id"] = "gr:" + "e" * 40
    elif case == "author": record["author"] = "Someone Else"
    elif case in ("head", "tree", "remote"):
        record["members"] = [dict(root["members"][0])]
        field = {"head": "head_commit", "tree": "head_tree", "remote": "remote"}[case]
        record["members"][0][field] = "wrong"
    elif case == "approver_shape": record["approver"] = []
    elif case == "name_empty": record["approver"]["name"] = ""
    elif case == "name_control": record["approver"]["name"] = "B\x00"
    elif case == "self": record["approver"]["name"] = "Author A"
    elif case == "range_author": record["approver"]["name"] = "Range Author"
    elif case in ("signature", "empty_signature"): record["sig"] = "abc" if case == "signature" else ""
    elif case == "key": record["approver"]["key_id"] = "e" * 64
    elif case == "keyring": record["keyring_tip"] = "e" * 40
    elif case == "digest": record["prev"] = "not a digest"
    elif case == "timestamp": record["created_at"] = 1
    elif case == "date": record["created_at"] = "2026-02-30T00:00:00Z"
    with pytest.raises(approvals.ApprovalRefused, match=reason):
        approvals._validate_link(record, root, authors)


def test_unsigned_valid_link_control():
    root, record = _sample_link()
    assert approvals._validate_link(record, root, {"Author A"}) == "Approver B"


@pytest.mark.parametrize("text", ['[]', '{', '{"a":1,"a":2}', '{"a":NaN}', '{"a": 1}', '{"a":1}\n'])
def test_malformed_json_refuses(text, monkeypatch):
    def measured(repo, *args, **kwargs):
        return "100644 blob " + "b" * 40 + "\tapproval.json" if args[0] == "ls-tree" else text
    monkeypatch.setattr(approvals, "_git", measured)
    with pytest.raises(approvals.ApprovalRefused, match="approval_chain_broken"):
        approvals._read_link(None, "a" * 40)


@pytest.mark.parametrize("entries", ["", "100755 blob " + "b" * 40 + "\tapproval.json",
                                     "120000 blob " + "b" * 40 + "\tapproval.json",
                                     "100644 blob " + "b" * 40 + "\tapproval.json\n100644 blob " + "b" * 40 + "\textra"])
def test_malformed_tree_refuses(entries, monkeypatch):
    monkeypatch.setattr(approvals, "_git", lambda repo, *args, **kwargs: entries if args[0] == "ls-tree" else '{}')
    with pytest.raises(approvals.ApprovalRefused, match="expected only plain approval.json"):
        approvals._read_link(None, "a" * 40)


@pytest.mark.parametrize("setting", ["required = -1", "required = true", "required = '2'", "require_signed = 'false'", "require_signed = true"])
def test_invalid_or_signed_policy_refuses(tmp_path, setting):
    (tmp_path / "grip.toml").write_text("[approvals]\n" + setting)
    reason = "approval_signed_unsupported" if setting == "require_signed = true" else "approval_policy_invalid"
    with pytest.raises(approvals.ApprovalRefused, match=reason):
        approvals.required_approvals(tmp_path)


def test_policy_default_zero_and_explicit_override(tmp_path):
    p = tmp_path / "grip.toml"
    p.write_text("[workspace]\nname='test'\n")
    assert approvals.required_approvals(tmp_path) == 0
    p.write_text("[approvals]\nrequired=2\n")
    assert approvals.required_approvals(tmp_path) == 2
    assert approvals.required_approvals(tmp_path, 3) == 3


@pytest.fixture
def multi(world, tmp_path):
    """Extend the disposable single-member fixture to a two-member bind before approving."""
    from gr2.python_cli import grip
    workspace = world["workspace"]
    remote = tmp_path / "beta.git"
    git(tmp_path, "init", "--bare", "-b", "main", remote)
    git(world["member"], "push", remote, world["base"] + ":refs/heads/main")
    beta = workspace / "beta"
    git(workspace, "clone", remote, beta)
    git(beta, "checkout", "-b", "feature")
    (beta / "payload").write_text("beta reviewed\n")
    git(beta, "add", "payload")
    git(beta, "commit", "-m", "beta feature")
    head = git(beta, "rev-parse", "HEAD")
    members = {"member": dict(repo=world["member"], remote=world["remote"], head=world["head"], base=world["base"]),
               "beta": dict(repo=beta, remote=remote, head=head, base=world["base"])}
    rows = [{"key": key, "path": key, "remote": str(m["remote"]), "head": m["head"], "base": m["base"],
             "ref": "refs/heads/main", "source": str(m["repo"])} for key, m in members.items()]
    # The original member was already pushed by world; this names that local-fixture permission.
    rid = grip.create_review_bind_commit(workspace, rows, ratified="local fixture")
    git(workspace, "update-ref", "-d", "refs/dev.synapt.grip/__reviews__/v1/" + world["rid"])
    for m in members.values():
        git(m["repo"], "push", m["remote"], "feature")
        grip.publish_review_commit(workspace, rid, str(m["remote"]))
        check_records.run_check(m["repo"], str(m["remote"]), m["head"], "test", [sys.executable, "-c", "pass"])
    return {**world, "rid": rid, "members": members}


def test_divergent_remote_tips_refuse_and_approve_repairs_a_linear_partial(multi):
    result = approve_as(multi, "Approver B")
    assert result.exit_code == 0, result.output
    tip = json.loads(result.output)["tip"]
    ref = approvals.PREFIX + multi["rid"]
    beta = multi["members"]["beta"]["remote"]
    git(beta, "update-ref", "-d", ref)
    before = {str(m["remote"]): git(m["remote"], "rev-parse", "main") for m in multi["members"].values()}
    merged = runner.invoke(app, ["review", "merge"])
    assert merged.exit_code == 3, merged.output
    receipt = json.loads(merged.output)
    assert all(row["refused"].startswith("approval_chain_divergent") for row in receipt["members"])
    assert str(beta) in merged.output and str(multi["remote"]) in merged.output
    assert before == {str(m["remote"]): git(m["remote"], "rev-parse", "main") for m in multi["members"].values()}
    repaired = approve_as(multi, "Approver C")
    assert repaired.exit_code == 0, repaired.output
    receipt = json.loads(repaired.output)
    assert receipt["count"] == 2
    for m in multi["members"].values():
        assert git(m["remote"], "rev-parse", ref) == receipt["tip"]
        assert git(m["remote"], "rev-parse", receipt["tip"] + "~1") == tip


def _write_link(world, record, parent):
    repo = world["workspace"]
    blob = approvals._git(repo, "hash-object", "-w", "--stdin", data=approvals.canonical(record))
    tree = approvals._git(repo, "mktree", data=f"100644 blob {blob}\tapproval.json\n")
    return git(repo, "commit-tree", tree, "-p", parent, "-m", "fixture approval")


def test_forked_remote_tips_refuse_without_overwriting_either(multi):
    made = approve_as(multi, "Approver B")
    assert made.exit_code == 0, made.output
    tip = json.loads(made.output)["tip"]
    previous = json.loads(git(multi["workspace"], "show", tip + ":approval.json"))
    ref = approvals.PREFIX + multi["rid"]
    forks = {}
    for name, member in zip(("Approver C", "Approver D"), multi["members"].values()):
        record = {**previous, "approver": {"name": name, "key_id": ""}, "prev": approvals.digest(previous)}
        fork = _write_link(multi, record, tip)
        git(multi["workspace"], "push", member["remote"], fork + ":" + ref)
        forks[str(member["remote"])] = fork
    result = approve_as(multi, "Approver E")
    assert result.exit_code == 2 and "approval_chain_divergent" in result.output, result.output
    assert forks == {str(m["remote"]): git(m["remote"], "rev-parse", ref) for m in multi["members"].values()}


def test_lease_race_refuses_then_a_fresh_attempt_appends(world, monkeypatch):
    made = approve_as(world, "Approver B")
    assert made.exit_code == 0, made.output
    tip = json.loads(made.output)["tip"]
    previous = json.loads(git(world["workspace"], "show", tip + ":approval.json"))
    record = {**previous, "approver": {"name": "Racing C", "key_id": ""}, "prev": approvals.digest(previous)}
    competitor = _write_link(world, record, tip)
    ref = approvals.PREFIX + world["rid"]
    original = approvals._git
    def race(repo, *args, **kwargs):
        if args[0] == "push":
            git(world["workspace"], "push", world["remote"], competitor + ":" + ref)
            monkeypatch.setattr(approvals, "_git", original)
        return original(repo, *args, **kwargs)
    monkeypatch.setattr(approvals, "_git", race)
    refused = approve_as(world, "Approver D")
    assert refused.exit_code == 2, refused.output
    assert git(world["remote"], "rev-parse", ref) == competitor
    retried = approve_as(world, "Approver D")
    assert retried.exit_code == 0 and json.loads(retried.output)["count"] == 3, retried.output
    assert git(world["remote"], "rev-parse", ref + "~1") == competitor


def test_count_refuses_a_tip_that_moves_during_the_walk(monkeypatch):
    root, _ = _sample_link()
    monkeypatch.setattr(approvals, "_context", lambda *args: (root, {"Author A"}))
    measurements = iter(({"remote.git": "b" * 40}, {"remote.git": "c" * 40}))
    monkeypatch.setattr(approvals, "_remote_tips", lambda *args: next(measurements))
    monkeypatch.setattr(approvals, "_walk", lambda *args: [])
    with pytest.raises(approvals.ApprovalRefused, match="approval_chain_moved"):
        approvals.count_approvals(None, root["review_id"])


def test_count_refuses_divergence_without_writes(monkeypatch):
    root, _ = _sample_link()
    tips = {"alpha.git": "b" * 40, "beta.git": "c" * 40}
    monkeypatch.setattr(approvals, "_context", lambda *args: (root, {"Author A"}))
    monkeypatch.setattr(approvals, "_remote_tips", lambda *args: tips)
    monkeypatch.setattr(approvals, "_walk", lambda *args: [])
    with pytest.raises(approvals.ApprovalRefused, match="approval_chain_divergent"):
        approvals.count_approvals(None, root["review_id"])


def test_duplicate_json_keys_are_refused_directly():
    with pytest.raises(approvals.ApprovalRefused, match="duplicate JSON key"):
        approvals._unique_pairs([("a", 1), ("a", 2)])


def test_approval_ref_cannot_point_to_the_bind():
    root, _ = _sample_link()
    with pytest.raises(approvals.ApprovalRefused, match="approval ref points at bind"):
        approvals._walk(None, root, {"Author A"}, root["review_id"].removeprefix("gr:"))


def test_a_link_must_have_one_parent(monkeypatch):
    root, record = _sample_link()
    rid = root["review_id"].removeprefix("gr:")
    record["prev"] = approvals.digest(root)
    monkeypatch.setattr(approvals, "_git", lambda *args, **kwargs: "b" * 40 + " " + rid + " " + "c" * 40)
    monkeypatch.setattr(approvals, "_read_link", lambda *args: record)
    with pytest.raises(approvals.ApprovalRefused, match="expected one parent"):
        approvals._walk(None, root, {"Author A"}, "b" * 40)


def test_bind_verification_failure_stops_before_any_remote(monkeypatch, tmp_path):
    from gr2.python_cli import merge_gate
    monkeypatch.setattr(merge_gate, "_toplevel", lambda path: path)
    monkeypatch.setattr(merge_gate, "_store_inside", lambda *args: True)
    monkeypatch.setattr(approvals.grip, "verify_review_commit", lambda *args: {"tree_matches": False})
    with pytest.raises(approvals.ApprovalRefused, match="approval_bind_unverified"):
        approvals._context(tmp_path, "a" * 40)


def test_author_is_refused_before_remote_measurement(monkeypatch):
    root, _ = _sample_link()
    monkeypatch.setattr(approvals, "_context", lambda *args: (root, {"Author A"}))
    monkeypatch.setattr(approvals, "_git", lambda *args, **kwargs: "Author A")
    monkeypatch.setattr(approvals, "_remote_tips", lambda *args: pytest.fail("author reached remote measurement"))
    with pytest.raises(approvals.ApprovalRefused, match="self_approval"):
        approvals.approve(Path.cwd(), root["review_id"])


def test_option_shaped_remote_stops_before_git(monkeypatch):
    root, _ = _sample_link()
    root["members"][0]["remote"] = "--upload-pack=bad"
    monkeypatch.setattr(approvals, "_git", lambda *args, **kwargs: pytest.fail("invalid remote reached git"))
    with pytest.raises(approvals.ApprovalRefused, match="approval_remote_invalid"):
        approvals._remote_tips(None, root)


def test_duplicate_remote_advertisement_refuses(monkeypatch):
    root, _ = _sample_link()
    ref = approvals.PREFIX + root["review_id"].removeprefix("gr:")
    monkeypatch.setattr(approvals, "_git", lambda *args, **kwargs: (("b" * 40) + "\t" + ref + "\n") * 2)
    with pytest.raises(approvals.ApprovalRefused, match="approval_chain_unmeasurable"):
        approvals._remote_tips(None, root)


def test_workspace_store_is_checked_before_bind_reads(monkeypatch, tmp_path):
    from gr2.python_cli import merge_gate
    monkeypatch.setattr(merge_gate, "_toplevel", lambda path: path)
    monkeypatch.setattr(merge_gate, "_store_inside", lambda *args: False)
    monkeypatch.setattr(approvals.grip, "verify_review_commit", lambda *args: pytest.fail("unsafe store read"))
    with pytest.raises(approvals.ApprovalRefused, match="approval_workspace_store_outside_workspace"):
        approvals._context(tmp_path, "a" * 40)


def test_chain_length_limit_refuses(monkeypatch):
    root, record = _sample_link()
    record["prev"] = "d" * 64
    monkeypatch.setattr(approvals, "digest", lambda *args: "d" * 64)
    monkeypatch.setattr(approvals, "_read_link", lambda *args: record)
    monkeypatch.setattr(approvals, "_git", lambda repo, *args, **kwargs: args[-1] + " " + f"{int(args[-1], 16)-1:040x}")
    with pytest.raises(approvals.ApprovalRefused, match="excessive length"):
        approvals._walk(None, root, {"Author A"}, f"{20000:040x}")


def test_git_failure_is_not_an_empty_success(monkeypatch, tmp_path):
    monkeypatch.setattr(approvals.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 1, "", "refused"))
    with pytest.raises(approvals.ApprovalRefused, match="approval_unmeasurable"):
        approvals._git(tmp_path, "ls-remote")


def test_tip_fetch_must_match_its_advertisement(monkeypatch):
    root, _ = _sample_link()
    ref = approvals.PREFIX + root["review_id"].removeprefix("gr:")
    def changed(repo, *args, **kwargs):
        if args[0] == "ls-remote": return "b" * 40 + "\t" + ref
        if args[0] == "rev-parse": return "c" * 40
        return ""
    monkeypatch.setattr(approvals, "_git", changed)
    with pytest.raises(approvals.ApprovalRefused, match="approval_chain_moved"):
        approvals._remote_tips(None, root)


def test_reader_rejects_nested_member_paths(monkeypatch, tmp_path):
    from gr2.python_cli import merge_gate
    monkeypatch.setattr(approvals, "_git", lambda repo, *args, **kwargs: "a" * 40 if args[0] == "for-each-ref" else "b" * 40)
    monkeypatch.setattr(approvals.grip, "show_review_commit", lambda *args: {"members": [{"path": "nested", "head": "b" * 40}]})
    monkeypatch.setattr(merge_gate, "_toplevel", lambda path: tmp_path)
    with pytest.raises(approvals.ApprovalRefused, match="approval_bind_not_unique"):
        approvals.current_review(tmp_path)


def test_reader_refuses_several_matching_binds(monkeypatch, tmp_path):
    from gr2.python_cli import merge_gate
    def read(repo, *args, **kwargs):
        return "a" * 40 + "\n" + "c" * 40 if args[0] == "for-each-ref" else "b" * 40
    monkeypatch.setattr(approvals, "_git", read)
    monkeypatch.setattr(approvals.grip, "show_review_commit", lambda *args: {"members": [{"path": "member", "head": "b" * 40}]})
    monkeypatch.setattr(merge_gate, "_toplevel", lambda path: path)
    with pytest.raises(approvals.ApprovalRefused, match="approval_bind_not_unique"):
        approvals.current_review(tmp_path)


def test_policy_table_must_be_a_table(tmp_path):
    (tmp_path / "grip.toml").write_text("approvals=2")
    with pytest.raises(approvals.ApprovalRefused, match="approval_policy_invalid"):
        approvals.required_approvals(tmp_path)
