"""Unsigned approval-chain proof through the default CLI, using real bare Git remotes."""
import base64
import hashlib
import json
import subprocess
import sys
import traceback
from pathlib import Path

import pytest

from typer.testing import CliRunner

try:
    from typer._click.utils import strip_ansi
except ImportError:  # older Typer
    from click.utils import strip_ansi

from gr2.python_cli import approvals, check_records, merge_gate, next_steps, set_check
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
    # Reviewers change by config user.name, so the env name (which review_author_name reads first) is dropped after
    # setup; the member keeps Author A as its commit author through config.
    git(member, "config", "user.name", "Author A")
    monkeypatch.delenv("GIT_AUTHOR_NAME")
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
    advice = next(x for x in refused.output.splitlines() if x.startswith("next: "))
    assert "a different approver" in advice and "gr2 review stamp" in advice, advice
    assert not any(f in advice.lower() for f in ("git config", "git_config", "user.name", "delete")), advice
    for name, count in [("Approver B", 1), ("Approver C", 2)]:
        git(workspace, "config", "user.name", name)
        result = runner.invoke(app, ["review", "approve"])
        assert result.exit_code == 0, result.output
        receipt = json.loads(result.stdout)
        assert receipt["count"] == count, receipt
        assert receipt["signed"] is False
    observed = approvals.count_approvals(workspace, rid)
    assert [link["approver"] for link in observed["links"]] == ["Approver B", "Approver C"]
    tip = git(remote, "rev-parse", approvals.PREFIX + rid)
    assert git(remote, "rev-parse", tip + "~2") == rid
    assert git(remote, "rev-parse", "main") == base
    print(json.dumps({"proof": "approve_plus_count", "identities": ["Author A", "Approver B", "Approver C"],
                      "review": rid, "tip": tip, "count": observed["count"], "signed": False}))


def test_author_alone_is_told_the_solo_merge_and_the_printed_command_merges(world):
    """An author alone in a workspace reads the self-stamp refusal; its next line names the explicit zero, and that
    command, run as printed, merges with no approver and says where the zero came from."""
    import shlex
    refused = runner.invoke(app, ["review", "approve"])
    assert refused.exit_code == 2 and "self_approval" in refused.output, refused.output
    advice = next(x for x in refused.output.splitlines() if x.startswith("next: "))
    assert "a different approver" in advice, advice  # the second-approver path is still the first one named
    solo = advice.split("with no other approver: ", 1)[1]
    argv = shlex.split(solo)
    assert argv[:3] == ["gr2", "review", "merge"] and argv[-2:] == ["--approvals", "0"], argv
    check_records.run_check(world["member"], str(world["remote"]), world["head"], "test", [sys.executable, "-c", "pass"])
    merged = runner.invoke(app, argv[1:])
    assert merged.exit_code == 0, merged.output
    receipt = json.loads(merged.stdout)
    assert receipt["approvals"]["required"] == 0 and receipt["approvals"]["source"] == "--approvals", receipt
    assert git(world["remote"], "rev-list", "--parents", "-n", "1", "main").split()[1:] == [world["base"], world["head"]]


def test_author_alone_under_an_approvals_floor_is_not_told_the_solo_merge(world):
    """Under grip.toml [approvals] required = 1, --approvals 0 lowers nothing, so the self-stamp refusal names the
    floor instead of a solo merge that would refuse."""
    set_policy(world, 1)
    refused = runner.invoke(app, ["review", "approve"])
    assert refused.exit_code == 2 and "self_approval" in refused.output, refused.output
    advice = next(x for x in refused.output.splitlines() if x.startswith("next: "))
    assert "a different approver" in advice, advice
    assert "--approvals 0" not in advice and "no other approver" not in advice, advice
    assert "requires 1 approval(s) (grip.toml [approvals] required)" in advice, advice


def test_author_alone_in_a_review_open_lane_reads_the_floor_of_the_store(world, tmp_path):
    """A `review open` lane stamps against the store it was opened from, so the self-stamp refusal reads that store's
    grip.toml: under a floor of 1 the lane is not told the solo merge (the lane itself has no grip.toml)."""
    set_policy(world, 1)
    lane = tmp_path / "lane"
    opened = runner.invoke(app, ["review", "open", str(world["workspace"]), "gr:" + world["rid"], "--lane-dir", str(lane)])
    assert opened.exit_code == 0, opened.output
    refused = runner.invoke(app, ["review", "stamp", str(lane)])
    assert refused.exit_code == 2 and "self_approval" in refused.output, refused.output
    advice = next(x for x in refused.output.splitlines() if x.startswith("next: "))
    assert "--approvals 0" not in advice and "requires 1 approval(s)" in advice, advice


def test_author_alone_in_a_review_open_lane_is_told_a_solo_merge_that_runs_as_printed(world, tmp_path):
    """With no floor, the lane's solo line names the store and the review id, and runs as printed."""
    import shlex
    lane = tmp_path / "lane"
    opened = runner.invoke(app, ["review", "open", str(world["workspace"]), "gr:" + world["rid"], "--lane-dir", str(lane)])
    assert opened.exit_code == 0, opened.output
    refused = runner.invoke(app, ["review", "stamp", str(lane)])
    assert refused.exit_code == 2 and "self_approval" in refused.output, refused.output
    advice = next(x for x in refused.output.splitlines() if x.startswith("next: "))
    argv = shlex.split(advice.split("with no other approver: ", 1)[1])
    assert "<review id>" not in argv and argv[-2:] == ["--approvals", "0"], argv
    check_records.run_check(world["member"], str(world["remote"]), world["head"], "test", [sys.executable, "-c", "pass"])
    merged = runner.invoke(app, argv[1:])
    assert merged.exit_code == 0, merged.output


def test_review_help_names_the_solo_path_in_step_three():
    out = runner.invoke(app, ["review", "--help"])
    assert out.exit_code == 0, out.output
    # CI forces colour (GITHUB_ACTIONS), and a style can split "--approvals"; rich also draws panel borders.
    text = " ".join(strip_ansi(out.output).replace("\u2502", " ").split())
    step3 = text[text.index("3. gr2 review stamp"):text.index("4. gr2 check run")]
    assert "no other approver" in step3 and "--approvals 0" in step3, step3
    assert "no approvals required in grip.toml" in step3, step3


def test_default_merge_reads_workspace_policy_and_counts_two_approvers(world):
    set_policy(world, 2)
    assert approve_as(world, "Approver B").exit_code == 0
    insufficient = runner.invoke(app, ["review", "merge"])
    receipt = json.loads(insufficient.stdout)
    assert insufficient.exit_code == 3, insufficient.output
    assert receipt["members"][0]["refused"].startswith("approvals_insufficient")
    assert receipt["approvals"]["count"] == 1 and receipt["approvals"]["required"] == 2
    assert git(world["remote"], "rev-parse", "main") == world["base"]
    assert approve_as(world, "Approver C").exit_code == 0
    merged = runner.invoke(app, ["review", "merge"])
    receipt = json.loads(merged.stdout)
    assert merged.exit_code == 0, merged.output
    assert receipt["approvals"]["count"] == 2 and receipt["approvals"]["required"] == 2
    assert receipt["approvals"]["source"] == "grip.toml", "a counted receipt still says where its count came from"
    assert {link["approver"] for link in receipt["approvals"]["links"]} == {"Approver B", "Approver C"}
    assert git(world["remote"], "rev-list", "--parents", "-n", "1", "main").split()[1:] == [world["base"], world["head"]]


def test_duplicate_approver_counts_once_and_cannot_satisfy_two(world):
    set_policy(world, 2)
    for _ in range(2):
        result = approve_as(world, "Approver B")
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["count"] == 1
    merged = runner.invoke(app, ["review", "merge"])
    assert merged.exit_code == 3, merged.output
    assert json.loads(merged.stdout)["members"][0]["refused"].startswith("approvals_insufficient")
    assert git(world["remote"], "rev-parse", "main") == world["base"]


def test_broken_prev_refuses_without_moving_target(world):
    set_policy(world, 1)
    result = approve_as(world, "Approver B")
    assert result.exit_code == 0, result.output
    tip = json.loads(result.stdout)["tip"]
    record = json.loads(git(world["remote"], "show", tip + ":approval.json"))
    record["prev"] = "0" * 64
    blob = approvals._git(world["workspace"], "hash-object", "-w", "--stdin", data=approvals.canonical(record))
    tree = approvals._git(world["workspace"], "mktree", data=f"100644 blob {blob}\tapproval.json\n")
    damaged = git(world["workspace"], "commit-tree", tree, "-p", world["rid"], "-m", "damaged prev")
    ref = approvals.PREFIX + world["rid"]
    git(world["workspace"], "push", "--force", world["remote"], damaged + ":" + ref)
    merged = runner.invoke(app, ["review", "merge"])
    assert merged.exit_code == 3, merged.output
    assert json.loads(merged.stdout)["members"][0]["refused"].startswith("approval_chain_broken: prev")
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
    assert default.exit_code == 3 and "review_not_found" in default.output, default.output
    explicit = runner.invoke(app, ["review", "merge", "gr:" + world["rid"]])
    assert explicit.exit_code == 3, explicit.output
    assert json.loads(explicit.stdout)["members"][0]["refused"].startswith("feature_moved")
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


def test_policy_unset_refuses_and_zero_is_explicit(tmp_path):
    p = tmp_path / "grip.toml"
    p.write_text("[workspace]\nname='test'\n")
    with pytest.raises(approvals.ApprovalRefused, match="approvals_unset"):
        approvals.required_approvals(tmp_path)
    (tmp_path / "grip.toml").unlink()
    with pytest.raises(approvals.ApprovalRefused, match="approvals_unset"):
        approvals.required_approvals(tmp_path)
    assert approvals.approval_policy(tmp_path, 0) == {"required": 0, "source": "--approvals"}
    p.write_text("[approvals]\nrequired=0\n")
    assert approvals.approval_policy(tmp_path) == {"required": 0, "source": "grip.toml"}
    p.write_text("[approvals]\nrequired=2\n")
    assert approvals.required_approvals(tmp_path) == 2
    assert approvals.required_approvals(tmp_path, 3) == 3
    assert approvals.required_approvals(tmp_path, 0) == 2
    assert approvals.required_approvals(tmp_path, 1) == 2
    assert approvals.approval_policy(tmp_path, 3)["source"] == "grip.toml and --approvals"


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
             "ref": "refs/heads/main", "source": str(m["repo"]), "visibility": "public"} for key, m in members.items()]
    # The original member was already pushed by world as its feature branch tip, which pins and records
    # pushed_before_pin (1.0521306); no receipt is needed.
    rid = grip.create_review_bind_commit(workspace, rows)
    git(workspace, "update-ref", "-d", "refs/dev.synapt.grip/__reviews__/v1/" + world["rid"])
    for m in members.values():
        git(m["repo"], "push", m["remote"], "feature")
        grip.publish_review_commit(workspace, rid, str(m["remote"]))
        check_records.run_check(m["repo"], str(m["remote"]), m["head"], "test", [sys.executable, "-c", "pass"])
    return {**world, "rid": rid, "members": members}


def test_divergent_remote_tips_refuse_and_approve_repairs_a_linear_partial(multi):
    set_policy(multi, 1)
    result = approve_as(multi, "Approver B")
    assert result.exit_code == 0, result.output
    tip = json.loads(result.stdout)["tip"]
    ref = approvals.PREFIX + multi["rid"]
    beta = multi["members"]["beta"]["remote"]
    git(beta, "update-ref", "-d", ref)
    before = {str(m["remote"]): git(m["remote"], "rev-parse", "main") for m in multi["members"].values()}
    # a review over several members merges only on a passing set; recorded so the approval chain is what refuses
    rec = set_check.run_set_check(multi["workspace"], multi["rid"], "set", [sys.executable, "-c", "pass"])
    assert rec.get("result") == "pass", rec
    merged = runner.invoke(app, ["review", "merge"])
    assert merged.exit_code == 3, merged.output
    receipt = json.loads(merged.stdout)
    assert all(row["refused"].startswith("approvals_incomplete") for row in receipt["members"])
    assert all(row["refusal"]["expected"] == tip for row in receipt["members"])
    assert "not yet on beta" in merged.output and "retry" in merged.output
    assert before == {str(m["remote"]): git(m["remote"], "rev-parse", "main") for m in multi["members"].values()}
    repaired = approve_as(multi, "Approver C")
    assert repaired.exit_code == 0, repaired.output
    receipt = json.loads(repaired.stdout)
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
    tip = json.loads(made.stdout)["tip"]
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
    tip = json.loads(made.stdout)["tip"]
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
    assert retried.exit_code == 0 and json.loads(retried.stdout)["count"] == 3, retried.output
    assert git(world["remote"], "rev-parse", ref + "~1") == competitor


def test_count_refuses_a_tip_that_moves_during_the_walk(monkeypatch):
    root, _ = _sample_link()
    monkeypatch.setattr(approvals, "_context", lambda *args, **kw: (root, {"Author A"}))
    measurements = iter(({"remote.git": "b" * 40}, {"remote.git": "c" * 40}))
    monkeypatch.setattr(approvals, "_remote_tips", lambda *args: next(measurements))
    monkeypatch.setattr(approvals, "_walk", lambda *args: [])
    with pytest.raises(approvals.ApprovalRefused, match="approval_chain_moved"):
        approvals.count_approvals(None, root["review_id"])


def test_count_refuses_divergence_without_writes(monkeypatch):
    root, _ = _sample_link()
    tips = {"alpha.git": "b" * 40, "beta.git": "c" * 40}
    monkeypatch.setattr(approvals, "_context", lambda *args, **kw: (root, {"Author A"}))
    monkeypatch.setattr(approvals, "_remote_tips", lambda *args: tips)
    monkeypatch.setattr(approvals, "_walk", lambda *args: [])
    monkeypatch.setattr(approvals, "_ancestors", lambda workspace, tip: {tip})  # unrelated fake tips: a fork
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
    monkeypatch.setattr(approvals, "_bind_record", lambda *args: root)
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
    monkeypatch.setattr(approvals.grip, "review_author_name", lambda *args: "Author A")
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
    monkeypatch.setattr(approvals, "_bind_record", lambda *args: root)
    rid = root["review_id"].removeprefix("gr:")
    def finite_chain(repo, *args, **kwargs):
        value = int(args[-1], 16)
        parent = rid if value == 1 else f"{value-1:040x}"
        return args[-1] + " " + parent
    monkeypatch.setattr(approvals, "_git", finite_chain)
    with pytest.raises(approvals.ApprovalRefused, match="excessive length"):
        approvals._walk(None, root, {"Author A"}, f"{20000:040x}")


def test_git_failure_is_not_an_empty_success(monkeypatch, tmp_path):
    monkeypatch.setattr(approvals.gitops, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 1, "", "refused"))
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


def _missing_remote(tmp_path):
    """A remote that does not exist, named so git echoes a word the refusal must not carry."""
    workspace = tmp_path / "ws"
    git(tmp_path, "init", "-q", workspace)
    return workspace, str(tmp_path / "sekretword-missing.git")


def _refuses_by_key(caught, reason):
    text = str(caught.value)
    assert text == reason + ": m", text
    full = "".join(traceback.format_exception(caught.value))
    assert "sekretword" not in full, full


def test_remote_tips_on_a_missing_remote_names_the_member_not_git(tmp_path):
    workspace, remote = _missing_remote(tmp_path)
    root, _ = _sample_link()
    root["members"][0]["remote"] = remote
    with pytest.raises(approvals.ApprovalRefused) as caught:
        approvals._remote_tips(workspace, root)
    _refuses_by_key(caught, "approval_remote_unreadable")


def test_remote_tips_fetch_from_a_missing_remote_names_the_member_not_git(tmp_path, monkeypatch):
    workspace, remote = _missing_remote(tmp_path)
    root, _ = _sample_link()
    root["members"][0]["remote"] = remote
    ref = approvals.PREFIX + root["review_id"].removeprefix("gr:")
    original = approvals._git
    def advertised(repo, *args, **kwargs):
        if args[0] == "ls-remote": return "b" * 40 + "\t" + ref
        return original(repo, *args, **kwargs)
    monkeypatch.setattr(approvals, "_git", advertised)
    with pytest.raises(approvals.ApprovalRefused) as caught:
        approvals._remote_tips(workspace, root)
    _refuses_by_key(caught, "approval_remote_unreadable")


def test_reconcile_push_to_a_missing_remote_names_the_member_not_git(tmp_path, monkeypatch):
    workspace, remote = _missing_remote(tmp_path)
    git(workspace, "commit", "-q", "--allow-empty", "-m", "tip")
    tip = git(workspace, "rev-parse", "HEAD")
    root, _ = _sample_link()
    root["members"][0]["remote"] = remote
    root["members"].append({**root["members"][0], "key": "other", "remote": str(workspace)})
    monkeypatch.setattr(approvals, "_walk", lambda *args: [])
    monkeypatch.setattr(approvals, "_longest", lambda *args: [tip])
    with pytest.raises(approvals.ApprovalRefused) as caught:
        approvals._reconcile(workspace, root, {"Author A"}, {remote: None, str(workspace): tip})
    _refuses_by_key(caught, "approval_remote_push_refused")


def test_approve_push_to_a_missing_remote_names_the_member_not_git(world, tmp_path, monkeypatch):
    missing = str(tmp_path / "sekretword-missing.git")
    original = approvals._git
    def elsewhere(repo, *args, **kwargs):
        if args[0] == "push":
            args = tuple(missing if a == str(world["remote"]) else a for a in args)
        return original(repo, *args, **kwargs)
    monkeypatch.setattr(approvals, "_git", elsewhere)
    refused = approve_as(world, "Approver B")
    assert refused.exit_code == 2, refused.output
    assert "approval_remote_push_refused: member" in refused.output, refused.output
    assert "sekretword" not in refused.output, refused.output


def test_reader_rejects_nested_member_paths(monkeypatch, tmp_path):
    from gr2.python_cli import defaults
    (tmp_path / "nested").mkdir()
    monkeypatch.setattr(approvals, "_git", lambda repo, *args, **kwargs: "a" * 40 if args[0] == "for-each-ref" else "b" * 40)
    monkeypatch.setattr(approvals.grip, "show_review_commit", lambda *args: {"members": [{"path": "nested", "head": "b" * 40}]})
    monkeypatch.setattr(defaults, "_git", lambda path, *args: str(tmp_path) if args[-1] == "--show-toplevel" else "b" * 40)
    with pytest.raises(approvals.ApprovalRefused, match="approval_bind_not_unique"):
        approvals.current_review(tmp_path)


def test_reader_refuses_several_matching_binds(monkeypatch, tmp_path):
    from gr2.python_cli import defaults
    def read(repo, *args, **kwargs):
        return "a" * 40 + "\n" + "c" * 40 if args[0] == "for-each-ref" else "b" * 40
    monkeypatch.setattr(approvals, "_git", read)
    monkeypatch.setattr(approvals.grip, "show_review_commit", lambda *args: {"members": [{"path": "member", "head": "b" * 40}]})
    (tmp_path / "member").mkdir()
    monkeypatch.setattr(defaults, "_git", lambda path, *args: str(path) if args[-1] == "--show-toplevel" else "b" * 40)
    with pytest.raises(approvals.ApprovalRefused, match="approval_bind_not_unique"):
        approvals.current_review(tmp_path)


def test_policy_table_must_be_a_table(tmp_path):
    (tmp_path / "grip.toml").write_text("approvals=2")
    with pytest.raises(approvals.ApprovalRefused, match="approval_policy_invalid"):
        approvals.required_approvals(tmp_path)


def test_first_prev_hashes_full_bind_with_bytes_and_nested_unknowns(world):
    from gr2.python_cli import review_field_tree as fd
    workspace = world["workspace"]
    original_tree = git(workspace, "rev-parse", world["rid"] + "^{tree}")
    original_wire = fd.to_protobuf(workspace, original_tree)
    changed = b""
    for number, wt, payload in fd.fields(original_wire):
        if number == 4:
            payload += fd._emit(999, 2, b"\xff\x00nested-unknown")
        changed += fd._emit(number, wt, payload)
    changed += fd._emit(998, 2, b"\x00\xffroot-unknown")
    tree = fd.write_tree(workspace, changed)
    # The ordinary decoded view cannot see either unknown, but the stored record can.
    assert fd.read_record(workspace, tree) == fd.read_record(workspace, original_tree)
    assert approvals.digest(approvals._bind_record(workspace, world["rid"])) != approvals.digest({
        "format": "protobuf-fields-v1", "schema": fd.PACKAGE, "fields": [
            {"number": n, "wire_type": wt, "payload": base64.b64encode(p).decode("ascii")}
            for n, wt, p in fd.fields(fd.to_protobuf(workspace, tree))]})
    rid = git(workspace, "commit-tree", tree, "-m", "bind with unknown fields")
    for line in git(workspace, "for-each-ref", "--format=%(refname) %(objectname)", "refs/dev.synapt.grip/__reviews__/").splitlines():
        ref, oid = line.split()
        if oid == world["rid"]:
            git(workspace, "update-ref", "-d", ref)
    git(workspace, "update-ref", "refs/dev.synapt.grip/__reviews__/v1/" + rid, rid)
    world["rid"] = rid
    published = runner.invoke(app, ["review", "publish", "gr:" + rid, "--remote", str(world["remote"])])
    assert published.exit_code == 0, published.output
    expected = {"format": "protobuf-fields-v1", "schema": fd.PACKAGE, "fields": [
        {"number": n, "wire_type": wt, "payload": base64.b64encode(p).decode("ascii")}
        for n, wt, p in fd.fields(fd.to_protobuf(workspace, tree))]}
    assert expected["fields"][-1]["payload"] == base64.b64encode(b"\x00\xffroot-unknown").decode("ascii")
    member = next(f for f in expected["fields"] if f["number"] == 4)
    assert fd.fields(base64.b64decode(member["payload"]))[-1] == (999, 2, b"\xff\x00nested-unknown")
    expected_hash = hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    approved = approve_as(world, "Approver B")
    assert approved.exit_code == 0, approved.output
    tip = json.loads(approved.stdout)["tip"]
    first = json.loads(git(world["remote"], "show", tip + ":approval.json"))
    assert first["prev"] == expected_hash
    assert first["prev"] != approvals.digest(approvals._context(workspace, rid)[0])
    second = approve_as(world, "Approver C")
    assert second.exit_code == 0, second.output
    last = json.loads(git(world["remote"], "show", json.loads(second.stdout)["tip"] + ":approval.json"))
    assert last["prev"] == approvals.digest(first)


def _mock_context(monkeypatch, tmp_path, actual="a" * 40, measured="a" * 40):
    from gr2.python_cli import merge_gate
    member = {"key": "m", "path": "member", "remote": "remote.git", "base": "b" * 40, "head": "c" * 40}
    monkeypatch.setattr(merge_gate, "_toplevel", lambda path: path)
    monkeypatch.setattr(merge_gate, "_store_inside", lambda *args: True)
    monkeypatch.setattr(approvals.grip, "verify_review_commit", lambda *args: {"tree_matches": True, "rows": [{"key": "m", "head_tree": measured}]})
    monkeypatch.setattr(approvals.grip, "show_review_commit", lambda *args: {"author": "Author A", "members": [member]})
    monkeypatch.setattr(approvals, "_git", lambda repo, *args, **kwargs: actual if args[0] == "rev-parse" else "")
    return merge_gate


def test_context_refuses_member_that_is_not_its_own_repo(monkeypatch, tmp_path):
    merge_gate = _mock_context(monkeypatch, tmp_path)
    monkeypatch.setattr(merge_gate, "_toplevel", lambda path: tmp_path)
    with pytest.raises(approvals.ApprovalRefused, match="approval_member_repo_mismatch"):
        approvals._context(tmp_path, "a" * 40)


def test_context_refuses_a_head_tree_mismatch(monkeypatch, tmp_path):
    _mock_context(monkeypatch, tmp_path, actual="d" * 40)
    with pytest.raises(approvals.ApprovalRefused, match="approval_head_tree_unmeasurable"):
        approvals._context(tmp_path, "a" * 40)


def test_branch_resolver_refuses_nested_member(monkeypatch, tmp_path):
    from gr2.python_cli import defaults
    nested = tmp_path / "member"
    nested.mkdir()
    monkeypatch.setattr(defaults, "_git", lambda path, *args: str(tmp_path) if args[-1] == "--show-toplevel" else "feature")
    with pytest.raises(defaults.Unresolved, match="feature_unresolved"):
        defaults.branch([nested], None)


def test_branch_resolver_refuses_different_member_branches(monkeypatch, tmp_path):
    from gr2.python_cli import defaults
    repos = [tmp_path / "alpha", tmp_path / "beta"]
    for repo in repos:
        repo.mkdir()
    monkeypatch.setattr(defaults, "_git", lambda path, *args: str(path) if args[-1] == "--show-toplevel" else path.name)
    with pytest.raises(defaults.Unresolved, match="feature_unresolved"):
        defaults.branch(repos, None)


def test_reconcile_refuses_a_changed_post_publish_measurement(monkeypatch):
    root, _ = _sample_link()
    tip = "b" * 40
    monkeypatch.setattr(approvals, "_walk", lambda *args: [])
    monkeypatch.setattr(approvals, "_git", lambda *args, **kwargs: tip)
    monkeypatch.setattr(approvals, "_remote_tips", lambda *args: {"remote.git": "c" * 40})
    with pytest.raises(approvals.ApprovalRefused, match="approval_chain_divergent"):
        approvals._reconcile(None, root, {"Author A"}, {"remote.git": tip})


def test_missing_approver_refuses_before_remote_measurement(monkeypatch):
    root, _ = _sample_link()
    monkeypatch.setattr(approvals, "_context", lambda *args: (root, {"Author A"}))
    monkeypatch.setattr(approvals.grip, "review_author_name", lambda *args: "")
    monkeypatch.setattr(approvals, "_remote_tips", lambda *args: pytest.fail("missing identity reached remote measurement"))
    with pytest.raises(approvals.ApprovalRefused, match="approval_identity_missing"):
        approvals.approve(Path.cwd(), root["review_id"])


def test_invalid_approver_name_refuses_before_remote_measurement(monkeypatch):
    root, _ = _sample_link()
    monkeypatch.setattr(approvals, "_context", lambda *args: (root, {"Author A"}))
    # approve reads the identity through grip.review_author_name, GIT_AUTHOR_NAME first, so the bad name is set there.
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Approver\x1bB")
    monkeypatch.setattr(approvals, "_git", lambda *args, **kwargs: "Approver\x1bB")
    monkeypatch.setattr(approvals, "_remote_tips", lambda *args: pytest.fail("invalid identity reached remote measurement"))
    monkeypatch.setattr(approvals, "_reconcile", lambda *args: pytest.fail("invalid identity reached reconcile"))
    with pytest.raises(approvals.ApprovalRefused, match="^approval_identity_invalid"):
        approvals.approve(Path.cwd(), root["review_id"])


def test_reconcile_lease_keeps_a_racing_remote_link(multi, monkeypatch):
    made = approve_as(multi, "Approver B")
    assert made.exit_code == 0, made.output
    tip = json.loads(made.stdout)["tip"]
    previous = json.loads(git(multi["workspace"], "show", tip + ":approval.json"))
    record = {**previous, "approver": {"name": "Racing C", "key_id": ""}, "prev": approvals.digest(previous)}
    competitor = _write_link(multi, record, tip)
    beta = multi["members"]["beta"]["remote"]
    ref = approvals.PREFIX + multi["rid"]
    git(beta, "update-ref", "-d", ref)
    original = approvals._git
    def race(repo, *args, **kwargs):
        if args[0] == "push":
            git(multi["workspace"], "push", beta, competitor + ":" + ref)
            monkeypatch.setattr(approvals, "_git", original)
        return original(repo, *args, **kwargs)
    monkeypatch.setattr(approvals, "_git", race)
    refused = approve_as(multi, "Approver D")
    assert refused.exit_code == 2, refused.output
    assert git(beta, "rev-parse", ref) == competitor
    assert git(multi["remote"], "rev-parse", ref) == tip


def test_cli_count_cannot_lower_workspace_policy(world):
    set_policy(world, 2)
    result = runner.invoke(app, ["review", "merge", "--approvals", "0"])
    assert result.exit_code == 3, result.output
    receipt = json.loads(result.stdout)
    assert receipt["approvals"]["required"] == 2 and receipt["approvals"]["count"] == 0
    assert receipt["members"][0]["refused"].startswith("approvals_insufficient")
    assert git(world["remote"], "rev-parse", "main") == world["base"]


def test_zero_required_skips_an_unmeasurable_approval_chain(world, monkeypatch):
    set_policy(world, 0)
    calls = []
    def unmeasurable(*args):
        calls.append(args)
        raise approvals.ApprovalRefused("approval_unmeasurable: fixture")
    monkeypatch.setattr(approvals, "count_approvals", unmeasurable)
    result = runner.invoke(app, ["review", "merge"])
    assert result.exit_code == 0, result.output
    assert calls == []
    assert json.loads(result.stdout)["approvals"] == {"required": 0, "skipped": True, "source": "grip.toml"}
    assert "approvals: skipped, 0 required by grip.toml" in result.stderr, "a skip is said where a person reads it"
    assert git(world["remote"], "rev-list", "--parents", "-n", "1", "main").split()[1:] == [world["base"], world["head"]]


def test_positive_required_read_failure_is_not_skipped(world, monkeypatch):
    set_policy(world, 2)
    calls = []
    def unmeasurable(*args):
        calls.append(args)
        raise approvals.ApprovalRefused("approval_unmeasurable: receipt fixture")
    monkeypatch.setattr(approvals, "count_approvals", unmeasurable)
    result = runner.invoke(app, ["review", "merge"])
    assert result.exit_code == 3, result.output
    receipt = json.loads(result.stdout)
    assert len(calls) == 1
    assert receipt["approvals"] == {"required": 2, "source": "grip.toml"}
    assert receipt["members"][0]["refused"].startswith("approval_unmeasurable")
    assert git(world["remote"], "rev-parse", "main") == world["base"]


def test_unset_policy_refuses_and_an_explicit_zero_merges(world, tmp_path):
    # Neither a grip.toml count nor --approvals: zero is never assumed. A missing file is the same as no table.
    with pytest.raises(approvals.ApprovalRefused, match="approvals_unset"):
        approvals.required_approvals(tmp_path)
    check_records.run_check(world["member"], str(world["remote"]), world["head"], "test", [sys.executable, "-c", "pass"])
    code, receipt = merge_gate.review_merge(world["workspace"], world["rid"], into="main", feature="feature")
    assert code == merge_gate.EXIT_REFUSED, receipt
    row = receipt["members"][0]
    assert row["refused"].startswith("approvals_unset"), row
    assert "--approvals N" in row["next"] and "[approvals] required = N" in row["next"], row
    assert git(world["remote"], "rev-parse", "main") == world["base"], "nothing merges on an unset count"
    # A count above what is recorded refuses as insufficient, not unset.
    result = runner.invoke(app, ["review", "merge", "--approvals", "2"])
    assert result.exit_code == 3, result.output
    refused = json.loads(result.stdout)
    assert refused["members"][0]["refused"] == "approvals_insufficient: 0 of 2"
    assert refused["approvals"]["source"] == "--approvals", "a counted count given only by flag names the flag"
    # Saying zero is the way to merge without approvals, and the skip is said on both surfaces.
    result = runner.invoke(app, ["review", "merge", "--approvals", "0"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["approvals"] == {"required": 0, "skipped": True, "source": "--approvals"}
    assert "approvals: skipped, 0 required by --approvals" in result.stderr


def test_approve_uses_workspace_identity_from_a_member_cwd(world, monkeypatch):
    git(world["workspace"], "config", "user.name", "Workspace B")
    git(world["member"], "config", "user.name", "Member C")
    monkeypatch.chdir(world["member"])
    result = runner.invoke(app, ["review", "approve"])
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.stdout)
    assert receipt["links"][0]["approver"] == "Workspace B"
    record = json.loads(git(world["remote"], "show", receipt["tip"] + ":approval.json"))
    assert record["approver"]["name"] == "Workspace B"


def test_one_person_with_env_and_config_names_apart_cannot_pin_then_approve(world, monkeypatch):
    workspace, member, remote, base = (world[k] for k in ("workspace", "member", "remote", "base"))
    (member / "payload").write_text("second\n")
    git(member, "commit", "-am", "second")
    head = git(member, "rev-parse", "HEAD")
    git(member, "push", remote, "feature")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Env X")
    git(workspace, "config", "user.name", "Config Y")
    bound = runner.invoke(app, ["review", "bind", "--repo", "member", "--remote", str(remote),
                               "--base", base, "--head", head, "--ref", "refs/heads/main", "--source", str(member)])
    assert bound.exit_code == 0, bound.output
    rid = approvals.current_review(workspace)
    assert rid != world["rid"]
    assert approvals._context(workspace, rid, None)[0]["author"] == "Env X"
    published = runner.invoke(app, ["review", "publish", "gr:" + rid, "--remote", str(remote)])
    assert published.exit_code == 0, published.output
    with pytest.raises(approvals.ApprovalRefused, match="self_approval"):
        approvals.approve(workspace, rid)
    assert git(remote, "for-each-ref", approvals.PREFIX + rid) == ""


@pytest.mark.parametrize("given", [-1, True, "2"])
def test_invalid_requested_count_refuses(tmp_path, given):
    (tmp_path / "grip.toml").write_text("[approvals]\nrequired=2\n")
    with pytest.raises(approvals.ApprovalRefused, match="requested count"):
        approvals.required_approvals(tmp_path, given)


def test_default_approve_skips_an_unreadable_bind(world):
    blob = approvals._git(world["workspace"], "hash-object", "-w", "--stdin", data="unreadable bind")
    git(world["workspace"], "update-ref", "refs/dev.synapt.grip/__reviews__/v1/unreadable", blob)
    result = approve_as(world, "Approver B")
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.stdout)
    assert receipt["review_id"] == "gr:" + world["rid"]
    assert receipt["count"] == 1


def test_merge_defaults_keep_explicit_flags_with_approval_floor(world):
    set_policy(world, 1)
    assert approve_as(world, "Approver B").exit_code == 0
    git(world["member"], "checkout", "--detach", world["head"])
    result = runner.invoke(app, ["review", "merge", "gr:" + world["rid"],
                                 "--from", "feature", "--into", "main", "--approvals", "0"])
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.stdout)
    assert receipt["feature"] == "feature" and receipt["into"] == "main"
    assert receipt["approvals"]["required"] == 1 and receipt["approvals"]["count"] == 1
    assert receipt["approvals"]["source"] == "grip.toml and --approvals", "a flag that cannot lower the floor is still named"
    assert "(given)" in result.stderr
    assert git(world["remote"], "rev-parse", "main^2") == world["head"]


def test_git_adapter_keeps_input_timeout_and_raw_text(monkeypatch, tmp_path):
    seen = []
    def launch(repo, *args, **kwargs):
        seen.append((repo, args, kwargs))
        return subprocess.CompletedProcess([], 0, "  result\n", "")
    monkeypatch.setattr(approvals.gitops, "run", launch)
    assert approvals._git(tmp_path, "hash-object", "--stdin", data="payload", raw=True) == "  result\n"
    assert approvals._git(tmp_path, "status") == "result"
    for _, _, kwargs in seen:
        env = kwargs.pop("env")
        assert (env["GIT_NO_REPLACE_OBJECTS"], env["GIT_GRAFT_FILE"]) == ("1", "/dev/null")
        assert env["PATH"] == approvals.os.environ["PATH"]
    true_history = ("-c", "core.commitGraph=false")
    assert seen == [
        (tmp_path, (*true_history, "hash-object", "--stdin"), {"input": "payload", "timeout": 60, "raise_timeout": True}),
        (tmp_path, (*true_history, "status"), {"input": None, "timeout": 60, "raise_timeout": True}),
    ]


@pytest.mark.parametrize("fault", [OSError("missing"), subprocess.SubprocessError("fault"),
                                  UnicodeError("bad text"), subprocess.TimeoutExpired("git", 60)])
def test_git_adapter_keeps_failure_translation(monkeypatch, tmp_path, fault):
    def launch(*args, **kwargs):
        raise fault
    monkeypatch.setattr(approvals.gitops, "run", launch)
    with pytest.raises(approvals.ApprovalRefused, match="approval_unmeasurable: " + type(fault).__name__):
        approvals._git(tmp_path, "ls-remote")


def test_default_approve_skips_canonical_blob_ref(world):
    blob = approvals._git(world["workspace"], "hash-object", "-w", "--stdin", data="not a bind commit")
    git(world["workspace"], "update-ref", "refs/dev.synapt.grip/__reviews__/v1/" + blob, blob)
    result = approve_as(world, "Approver B")
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.stdout)
    assert receipt["review_id"] == "gr:" + world["rid"]
    assert receipt["count"] == 1


def test_a_reviewer_who_stamps_before_receiving_gets_a_named_refusal_and_the_order_that_works(world, tmp_path, monkeypatch):
    import shlex
    refused = runner.invoke(app, ["review", "approve"])
    advice = next(x for x in refused.output.splitlines() if x.startswith("next: "))
    assert "gr2 review receive <their-workspace>" in advice and "gr2 review stamp <their-workspace>" in advice, advice
    reviewer = tmp_path / "reviewer-ws"
    reviewer.mkdir()
    git(reviewer, "clone", world["remote"], "member")
    assert runner.invoke(app, ["store", "init", str(reviewer)]).exit_code == 0
    monkeypatch.chdir(reviewer)
    git(reviewer, "config", "user.name", "Reviewer R")
    git(reviewer, "config", "user.email", "r@example.invalid")
    rid = "gr:" + world["rid"]
    early = runner.invoke(app, ["review", "stamp", str(reviewer), rid])
    assert early.exit_code == 2 and "not_received" in early.output, early.output
    assert "Traceback" not in early.output and early.exception is None or isinstance(early.exception, SystemExit), early.output
    line = next(x for x in early.output.splitlines() if x.startswith("next: "))
    commands = line[line.index("gr2 review receive"):].split(", then ")
    receive, stamp = (shlex.split(c.removeprefix("gr2 ")) for c in commands)
    receive[receive.index("<member-remote>")] = str(world["remote"])
    got = runner.invoke(app, receive)
    assert got.exit_code == 0, got.output
    stamped = runner.invoke(app, stamp)
    assert stamped.exit_code == 0, stamped.output


def _reviewer_without_the_head(world, tmp_path, monkeypatch):
    """A reviewer's own workspace whose member clone was made before the head was pushed, then the review received.

    receive now fetches a head the receiving remote serves; a member whose head lives on another remote is not
    fetched, and stamp fetches it. That is the path these rows keep: the head receive fetched is pruned again, so
    the clone lacks it the way such a member's clone does."""
    reviewer = tmp_path / "reviewer-ws"
    reviewer.mkdir()
    # --no-local: a local clone copies every object, which would hide the case a real early clone meets
    git(reviewer, "clone", "--no-local", "--single-branch", "--branch", "main", world["remote"], "member")
    assert runner.invoke(app, ["store", "init", str(reviewer)]).exit_code == 0
    monkeypatch.chdir(reviewer)
    git(reviewer, "config", "user.name", "Reviewer R")
    git(reviewer, "config", "user.email", "r@example.invalid")
    rid = "gr:" + world["rid"]
    assert runner.invoke(app, ["review", "receive", str(reviewer), rid, "--remote", str(world["remote"])]).exit_code == 0
    # receive fetched the head by id onto no ref; drop it so stamp meets a clone without it
    git(reviewer / "member", "reflog", "expire", "--expire=now", "--all")
    git(reviewer / "member", "gc", "-q", "--prune=now")
    assert subprocess.run(["git", "-C", str(reviewer / "member"), "cat-file", "-e", world["head"] + "^{commit}"],
                          capture_output=True).returncode != 0, "precondition: the reviewer's clone lacks the head"
    return reviewer, rid


def test_a_reviewer_clone_without_the_head_stamps_after_receive_with_no_hand_fetch(world, tmp_path, monkeypatch):
    """The reader's probe from the three-reviewer walk: receive, then stamp. stamp fetches the exact reviewed head from
    the member remote the review names (the remote it already reads the approval chain from), so a reviewer runs no
    git fetch per member."""
    reviewer, rid = _reviewer_without_the_head(world, tmp_path, monkeypatch)

    stamped = runner.invoke(app, ["review", "stamp", str(reviewer), rid])

    assert stamped.exit_code == 0, stamped.output
    receipt = json.loads(stamped.stdout)
    assert receipt["count"] == 1 and receipt["links"][0]["approver"] == "Reviewer R", receipt


def test_stamp_fetches_from_the_remote_the_review_names_not_the_clones_origin(world, tmp_path, monkeypatch):
    reviewer, rid = _reviewer_without_the_head(world, tmp_path, monkeypatch)
    git(reviewer / "member", "remote", "set-url", "origin", str(tmp_path / "nowhere.git"))

    stamped = runner.invoke(app, ["review", "stamp", str(reviewer), rid])

    assert stamped.exit_code == 0, stamped.output


def test_a_head_the_member_remote_no_longer_has_still_refuses_head_not_fetched(world, tmp_path, monkeypatch):
    """The fetch is asked of the remote, not assumed: when the remote no longer has the head, stamp refuses by name
    with the next step, and never with a raw `unknown revision`."""
    reviewer, rid = _reviewer_without_the_head(world, tmp_path, monkeypatch)
    git(world["remote"], "update-ref", "-d", "refs/heads/feature")
    git(world["remote"], "reflog", "expire", "--expire=now", "--all")
    git(world["remote"], "gc", "-q", "--prune=now")
    assert subprocess.run(["git", "-C", str(world["remote"]), "cat-file", "-e", world["head"] + "^{commit}"],
                          capture_output=True).returncode != 0, "precondition: the remote lacks the head"

    early = runner.invoke(app, ["review", "stamp", str(reviewer), rid])

    assert early.exit_code == 2 and "head_not_fetched: member" in early.output, early.output
    assert "unknown revision" not in early.output, early.output
    assert any(x.startswith("next: ") for x in early.output.splitlines()), early.output


def test_a_bind_approval_push_carries_no_pr_lane_approval(world):
    """PR-lane approvals (PREFIX + "pr/<head>") are local. The bind writer pushes only its own tip:ref, so a ref
    under pr/ in the repo it pushes from stays home while the bind approval reaches the remote."""
    workspace, remote, rid = world["workspace"], world["remote"], world["rid"]
    pr_ref = approvals.PR_PREFIX + world["head"]
    git(workspace, "update-ref", pr_ref, rid)  # any object the store holds; the review commit is one
    stamped = approve_as(world, "Reviewer B")
    assert stamped.exit_code == 0, stamped.output
    on_remote = git(remote, "for-each-ref", "--format=%(refname)", approvals.PREFIX).splitlines()
    assert approvals.PREFIX + rid in on_remote, on_remote  # control: the bind approval was pushed
    assert not [r for r in on_remote if r.startswith(approvals.PR_PREFIX)], on_remote


def _reviewer_in_another_gripspace(world, tmp_path, monkeypatch, *open_args):
    """A reviewer whose own workspace does not hold the reviewed repo at all (another team's gripspace): it receives
    the review and opens a lane, which is the only place the member is checked out."""
    reviewer = tmp_path / "other-gripspace"
    reviewer.mkdir()
    # their own repo, with a remote of its own: nothing in this workspace is the reviewed member
    theirs = tmp_path / "theirs.git"
    git(tmp_path, "init", "--bare", "-b", "main", theirs)
    git(reviewer, "clone", theirs, "their-own-repo")
    git(reviewer / "their-own-repo", "commit", "--allow-empty", "-m", "theirs")
    git(reviewer / "their-own-repo", "push", "origin", "main")
    initialised = runner.invoke(app, ["store", "init", str(reviewer)])
    assert initialised.exit_code == 0, initialised.output
    monkeypatch.chdir(reviewer)
    git(reviewer, "config", "user.name", "Reviewer R")
    git(reviewer, "config", "user.email", "r@example.invalid")
    rid = "gr:" + world["rid"]
    got = runner.invoke(app, ["review", "receive", str(reviewer), rid, "--remote", str(world["remote"])])
    assert got.exit_code == 0, got.output
    lane = tmp_path / "review-lane"
    opened = runner.invoke(app, ["review", "open", str(reviewer), rid, "--lane-dir", str(lane), *open_args])
    assert opened.exit_code == 0, opened.output
    return reviewer, rid, lane


def test_a_reviewer_in_another_gripspace_stamps_from_the_review_lane(world, tmp_path, monkeypatch):
    reviewer, rid, lane = _reviewer_in_another_gripspace(world, tmp_path, monkeypatch)
    git(lane, "config", "user.name", "Lane Name")  # the approver is the store workspace's name, not the lane's
    stamped = runner.invoke(app, ["review", "stamp", str(lane)])
    assert stamped.exit_code == 0, stamped.output
    receipt = json.loads(stamped.stdout)
    assert receipt["review_id"] == rid and receipt["count"] == 1, receipt
    assert [link["approver"] for link in receipt["links"]] == ["Reviewer R"], receipt
    # the author's own workspace reads the same approval from the member remote
    monkeypatch.chdir(world["workspace"])
    seen = approvals.count_approvals(world["workspace"], world["rid"])
    assert seen["count"] == 1 and [link["approver"] for link in seen["links"]] == ["Reviewer R"], seen


def test_a_reviewer_in_another_gripspace_stamps_a_two_member_review_from_its_lane(multi, tmp_path, monkeypatch):
    """Two members: the lane holds each at <lane>/<key>, and the stamp reaches both member remotes."""
    reviewer, rid, lane = _reviewer_in_another_gripspace(multi, tmp_path, monkeypatch)
    assert sorted(p.name for p in lane.iterdir() if (p / ".git").exists()) == ["beta", "member"]
    stamped = runner.invoke(app, ["review", "stamp", str(lane)])
    assert stamped.exit_code == 0, stamped.output
    receipt = json.loads(stamped.stdout)
    assert receipt["review_id"] == rid and receipt["count"] == 1, receipt
    ref = approvals.PREFIX + multi["rid"]
    tips = {key: git(m["remote"], "rev-parse", ref) for key, m in multi["members"].items()}
    assert len(set(tips.values())) == 1 and receipt["tip"] in tips.values(), tips


def test_a_member_directory_of_a_two_member_lane_stamps_its_lane(multi, tmp_path, monkeypatch):
    reviewer, rid, lane = _reviewer_in_another_gripspace(multi, tmp_path, monkeypatch)
    stamped = runner.invoke(app, ["review", "stamp", str(lane / "beta")])
    assert stamped.exit_code == 0, stamped.output
    assert json.loads(stamped.stdout)["count"] == 1, stamped.output


def test_a_lane_holding_part_of_the_review_refuses_by_name_with_the_next_step(multi, tmp_path, monkeypatch):
    reviewer, rid, lane = _reviewer_in_another_gripspace(multi, tmp_path, monkeypatch, "--repo", "beta")
    refused = runner.invoke(app, ["review", "stamp", str(lane)])
    assert refused.exit_code == 2, refused.output
    assert "approval_lane_partial: this lane holds beta; the review has" in refused.output, refused.output
    assert any(x.startswith("next: ") and "gr2 review open" in x for x in refused.output.splitlines()), refused.output
    ref = approvals.PREFIX + multi["rid"]
    assert all(subprocess.run(["git", "-C", str(m["remote"]), "rev-parse", "--verify", "-q", ref],
                              capture_output=True).returncode != 0 for m in multi["members"].values())


def test_a_lane_whose_marker_names_no_workspace_refuses_as_an_older_lane(world, tmp_path, monkeypatch):
    reviewer, rid, lane = _reviewer_in_another_gripspace(world, tmp_path, monkeypatch)
    marker = lane / ".grip-review-open.json"
    record = json.loads(marker.read_text())
    record.pop("workspace_root")
    marker.write_text(json.dumps(record))
    refused = runner.invoke(app, ["review", "stamp", str(lane)])
    assert refused.exit_code == 2 and "approval_lane_names_no_workspace" in refused.output, refused.output
    assert "unreadable" not in refused.output, refused.output


def test_a_lane_whose_marker_names_a_relative_workspace_refuses_by_name(world, tmp_path, monkeypatch):
    # review open writes workspace_root absolute; a hand-edited or foreign marker may not, and a relative root would
    # resolve against the caller's cwd: here cwd makes "other-gripspace" the reviewer's real workspace, so it would stamp
    reviewer, rid, lane = _reviewer_in_another_gripspace(world, tmp_path, monkeypatch)
    marker = lane / ".grip-review-open.json"
    record = json.loads(marker.read_text())
    assert Path(record["workspace_root"]) == reviewer.resolve(), record
    record["workspace_root"] = reviewer.name
    marker.write_text(json.dumps(record))
    monkeypatch.chdir(tmp_path)
    refused = runner.invoke(app, ["review", "stamp", str(lane)])
    assert refused.exit_code == 2 and "approval_lane_workspace_not_absolute" in refused.output, refused.output
    assert subprocess.run(["git", "-C", str(world["remote"]), "rev-parse", "--verify", "-q",
                           approvals.PREFIX + world["rid"]], capture_output=True).returncode != 0


# A stamp on a review whose target has moved past the reviewed base can never be merged (merge refuses base_moved,
# and the review cannot move: a new base is a new pin), so the stamp refuses before anything is pushed.

def _advance(repo, remote, base, branch="main"):
    """Move `branch` on `remote` one commit past `base`, as a teammate's merge does."""
    tree = git(repo, "rev-parse", base + "^{tree}")
    moved = git(repo, "commit-tree", tree, "-p", base, "-m", "main moved")
    git(repo, "push", remote, f"{moved}:refs/heads/{branch}")
    return moved


def _approval_refs(remote):
    return git(remote, "for-each-ref", "--format=%(refname)", approvals.PREFIX)


def test_stamp_after_the_target_moved_refuses_base_moved_and_writes_nothing(world):
    moved = _advance(world["member"], world["remote"], world["base"])
    result = approve_as(world, "Approver B")
    assert result.exit_code == 2, result.output
    assert "base_moved" in result.output and "member" in result.output, result.output
    assert world["base"][:12] in result.output and moved[:12] in result.output, result.output
    advice = next(x for x in result.output.splitlines() if x.startswith("next: "))
    assert "pin" in advice and "main" in advice, advice
    assert _approval_refs(world["remote"]) == "", "no approval may be pushed for a review that cannot merge"


@pytest.mark.parametrize("past", [False, True], ids=["tip_is_head", "one_past_head"])
def test_stamp_after_the_target_fast_forwarded_to_the_head_refuses_head_already_on_target(world, past):
    """A writer moved main to the reviewed head (or one past it) with no merge commit. base_moved's line sent the
    stamper to update, which pins the same head and refuses head_already_on_remote: merge's answer instead."""
    tip = world["head"]
    if past:
        tip = git(world["remote"], "commit-tree", git(world["remote"], "rev-parse", f"{tip}^{{tree}}"), "-p", tip,
                  "-m", "past")
    git(world["remote"], "update-ref", "refs/heads/main", tip)
    if past:
        git(world["member"], "fetch", "-q", world["remote"], "main")  # the tip object is held here
    result = approve_as(world, "Approver B")
    assert result.exit_code == 2, result.output
    assert (f"head_already_on_target: member: the reviewed head {world['head']} is already in main at {tip}"
            in result.output.splitlines()), result.output
    advice = next(x for x in result.output.splitlines() if x.startswith("next: "))
    assert advice == next_steps._nothing_to_merge() and "review update" not in advice, advice
    assert "base_moved" not in result.output, result.output
    assert _approval_refs(world["remote"]) == "", "no approval may be pushed for a review already on its target"


def test_stamp_with_a_past_tip_not_held_here_keeps_base_moved(world):
    """The ancestry read needs the tip object; one not fetched cannot be proved to hold the head."""
    tip = git(world["remote"], "commit-tree", git(world["remote"], "rev-parse", f"{world['head']}^{{tree}}"), "-p",
              world["head"], "-m", "past")
    git(world["remote"], "update-ref", "refs/heads/main", tip)
    result = approve_as(world, "Approver B")
    assert result.exit_code == 2 and "base_moved: member:" in result.output, result.output
    assert "head_already_on_target" not in result.output, result.output


def test_stamp_after_a_real_base_move_keeps_base_moved_when_ancestry_says_no(world, monkeypatch):
    """A tip held here that does not hold the head: the ancestry read answers False, and the stamp stays base_moved."""
    moved = _advance(world["member"], world["remote"], world["base"])
    answers = []
    real = approvals.gitops.is_ancestor
    monkeypatch.setattr(approvals.gitops, "is_ancestor", lambda *a, **k: answers.append(real(*a, **k)) or answers[-1])
    result = approve_as(world, "Approver B")
    assert answers == [False], answers
    assert result.exit_code == 2 and "base_moved: member:" in result.output, result.output
    assert moved[:12] in result.output and "head_already_on_target" not in result.output, result.output
    assert _approval_refs(world["remote"]) == "", "no approval may be pushed for a review that cannot merge"


def test_stamp_when_the_ancestry_read_fails_keeps_base_moved(world, monkeypatch):
    """A tip that does hold the head, but git could not tell (None): only a proved yes reads head_already_on_target."""
    tip = git(world["remote"], "commit-tree", git(world["remote"], "rev-parse", f"{world['head']}^{{tree}}"), "-p",
              world["head"], "-m", "past")
    git(world["remote"], "update-ref", "refs/heads/main", tip)
    git(world["member"], "fetch", "-q", world["remote"], "main")
    calls = []
    monkeypatch.setattr(approvals.gitops, "is_ancestor", lambda *a, **k: calls.append(a) or None)
    result = approve_as(world, "Approver B")
    assert len(calls) == 1, calls
    assert result.exit_code == 2 and "base_moved: member:" in result.output, result.output
    assert "head_already_on_target" not in result.output, result.output
    assert _approval_refs(world["remote"]) == "", "no approval may be pushed for a review that cannot merge"


def test_control_stamp_with_the_target_at_the_base_succeeds(world):
    result = approve_as(world, "Approver B")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["count"] == 1
    assert _approval_refs(world["remote"]) != ""


def test_stamp_succeeds_while_another_branch_still_sits_at_the_base(world):
    """A review targeting a non-default branch (dev) stamps while that branch is at the base, even after main
    moved. Known limit: an unrelated branch left at the base passes the same way; merge still refuses it."""
    git(world["member"], "push", world["remote"], world["base"] + ":refs/heads/dev")
    _advance(world["member"], world["remote"], world["base"])
    result = approve_as(world, "Approver B")
    assert result.exit_code == 0, result.output


def test_stamp_with_an_unreadable_member_remote_refuses_by_name_and_writes_nothing(world):
    """The base check's ls-remote fails (the remote is gone): a refusal naming the member and a next line, not
    git's raw message, and no approval pushed."""
    gone = world["remote"].with_name("gone.git")
    world["remote"].rename(gone)
    result = approve_as(world, "Approver B")
    gone.rename(world["remote"])
    assert result.exit_code == 2, result.output
    lines = result.output.splitlines()
    assert lines[0] == "approval_remote_unreadable: member", result.output
    assert lines[1].startswith("next: no stamp was written;") and str(world["remote"]) in lines[1], result.output
    assert "does not appear" not in result.output, result.output
    assert _approval_refs(world["remote"]) == ""


def test_unreadable_remote_with_a_credential_is_never_printed(tmp_path):
    remote = "https://user:sekret@127.0.0.1:1/x.git"
    with pytest.raises(approvals.ApprovalRefused) as caught:
        approvals._refuse_moved_base(tmp_path, "member", remote, "0" * 40, "gr:r", tmp_path, "1" * 40)
    shown = f"{caught.value}\n{caught.value.next_step}"
    assert str(caught.value) == "approval_remote_unreadable: member" and "sekret" not in shown, shown
    assert shown.splitlines()[1].startswith("next: no stamp was written;"), shown


@pytest.mark.parametrize("moved, still", [("beta", "member"), ("member", "beta")])
def test_stamp_names_only_the_member_whose_target_moved(multi, moved, still):
    """Both orders: beta sorts first and member last, so a check of the first member alone fails one of them."""
    m = multi["members"][moved]
    _advance(m["repo"], m["remote"], m["base"])
    result = approve_as(multi, "Approver B")
    assert result.exit_code == 2, result.output
    refusal = next(x for x in result.output.splitlines() if "base_moved" in x)
    assert f"base_moved: {moved}:" in refusal and f"{still}:" not in refusal, refusal
    for m in multi["members"].values():
        assert _approval_refs(m["remote"]) == "", m["remote"]


FIVE = ("api", "app", "core", "store", "svc")


def _five(monkeypatch, tips):
    root, _ = _sample_link()
    root = {**root, "members": [{**root["members"][0], "key": k, "remote": k + ".git"} for k in FIVE]}
    reads = iter(tips if isinstance(tips, list) else [tips, tips])
    monkeypatch.setattr(approvals, "_context", lambda *args, **kw: (root, {"Author A"}))
    monkeypatch.setattr(approvals, "_remote_tips", lambda *args: next(reads))
    monkeypatch.setattr(approvals, "_walk", lambda *args: [{"approver": "Approver B"}])
    # fake tips are unrelated commits: each is its own only ancestor, so two of them are a fork
    monkeypatch.setattr(approvals, "_ancestors", lambda workspace, tip: {tip})
    return root


def test_a_chain_missing_on_one_member_while_the_rest_agree_is_incomplete(monkeypatch):
    tips = {k + ".git": "b" * 40 for k in FIVE} | {"svc.git": None}
    root = _five(monkeypatch, tips)
    with pytest.raises(approvals.ApprovalRefused, match="^approvals_incomplete: ") as refused:
        approvals.count_approvals(None, root["review_id"])
    assert "4 of 5" in str(refused.value) and "not yet on svc" in str(refused.value)
    assert "api" not in str(refused.value).split("not yet on", 1)[1]
    assert (refused.value.expected, refused.value.observed) == ("b" * 40, None)


@pytest.mark.parametrize("tips", [
    {"api.git": "b" * 40, "app.git": "b" * 40, "core.git": "b" * 40, "store.git": "b" * 40, "svc.git": "c" * 40},
    {"api.git": "b" * 40, "app.git": None, "core.git": "b" * 40, "store.git": None, "svc.git": "c" * 40},
], ids=["last-differs", "with-missing"])
def test_two_different_tips_are_divergent_with_both_tips_named(monkeypatch, tips):
    root = _five(monkeypatch, tips)
    with pytest.raises(approvals.ApprovalRefused, match="^approval_chain_divergent: ") as refused:
        approvals.count_approvals(None, root["review_id"])
    assert (refused.value.expected, refused.value.observed) == ("b" * 40, "c" * 40)


def test_one_chain_on_every_member_counts(monkeypatch):
    root = _five(monkeypatch, {k + ".git": "b" * 40 for k in FIVE})
    assert approvals.count_approvals(None, root["review_id"])["count"] == 1


def test_a_stamp_finishing_between_two_reads_is_incomplete_then_counts(monkeypatch):
    late = {k + ".git": "b" * 40 for k in FIVE} | {"svc.git": None}
    done = {k + ".git": "b" * 40 for k in FIVE}
    root = _five(monkeypatch, [late, done, done])  # the refusal stops after its first read
    with pytest.raises(approvals.ApprovalRefused, match="^approvals_incomplete: "):
        approvals.count_approvals(None, root["review_id"])
    assert approvals.count_approvals(None, root["review_id"])["count"] == 1


@pytest.mark.parametrize("second, code, heads", [
    ({k + ".git": "b" * 40 for k in FIVE} | {"svc.git": None}, "approvals_incomplete", ("b" * 40, None)),
    ({k + ".git": "b" * 40 for k in FIVE} | {"svc.git": "c" * 40}, "approval_chain_divergent", ("b" * 40, "c" * 40)),
], ids=["second-read-missing", "second-read-forked"])
def test_a_chain_that_splits_during_the_walk_is_named_by_the_second_read(monkeypatch, second, code, heads):
    agree = {k + ".git": "b" * 40 for k in FIVE}
    root = _five(monkeypatch, [agree, second])
    with pytest.raises(approvals.ApprovalRefused, match=f"^{code}: ") as refused:
        approvals.count_approvals(None, root["review_id"])
    assert (refused.value.expected, refused.value.observed) == heads


def test_a_link_on_some_members_extending_the_rest_is_incomplete_at_the_longest_tip(monkeypatch):
    tips = {"api.git": "b" * 40, "app.git": "c" * 40, "core.git": "b" * 40, "store.git": None, "svc.git": "b" * 40}
    root = _five(monkeypatch, tips)
    monkeypatch.setattr(approvals, "_ancestors", lambda workspace, tip: {"b" * 40, "c" * 40} if tip == "c" * 40 else {tip})
    with pytest.raises(approvals.ApprovalRefused, match="^approvals_incomplete: ") as refused:
        approvals.count_approvals(None, root["review_id"])
    assert "at " + "c" * 12 + " is on 1 of 5 members, not yet on api, core, store, svc" in str(refused.value)
    assert (refused.value.expected, refused.value.observed) == ("c" * 40, None)


@pytest.mark.parametrize("order, heads", [
    (("a", "b", "c"), ("b", "c")),
    (("b", "a", "c"), ("b", "c")),
    (("c", "a", "b"), ("c", "b")),
    (("b", "a", "b", "c"), ("b", "c")),
    (("a", "b", "c", "d"), ("b", "c")),
], ids=["behind-first", "behind-middle", "behind-middle-reversed", "fork-tip-on-two-members", "three-way-fork"])
def test_a_fork_with_a_member_behind_it_is_named_by_the_two_fork_tips(monkeypatch, order, heads):
    tips = {k + ".git": tip * 40 for k, tip in zip(FIVE, order)}
    root = _five(monkeypatch, tips)
    root["members"] = root["members"][:len(order)]
    # b, c and d each build on a: a fork, with the member on a behind every side
    monkeypatch.setattr(approvals, "_ancestors", lambda workspace, tip: {"a" * 40, tip})
    with pytest.raises(approvals.ApprovalRefused, match="^approval_chain_divergent: ") as refused:
        approvals.count_approvals(None, root["review_id"])
    assert (refused.value.expected, refused.value.observed) == tuple(h * 40 for h in heads)


@pytest.mark.parametrize("order, heads", [
    (("b", "c", "d"), ("c", "d")),
    (("d", "b", "c"), ("d", "c")),
], ids=["behind-one-side-first", "behind-one-side-middle"])
def test_a_member_behind_one_side_of_a_fork_is_not_named(monkeypatch, order, heads):
    tips = {k + ".git": tip * 40 for k, tip in zip(FIVE, order)}
    root = _five(monkeypatch, tips)
    root["members"] = root["members"][:len(order)]
    # b and c build on a; d builds on b: the member on b is behind one side of the fork only
    chain = {"b": "ab", "c": "ac", "d": "abd"}
    monkeypatch.setattr(approvals, "_ancestors", lambda workspace, tip: {t * 40 for t in chain[tip[0]]})
    with pytest.raises(approvals.ApprovalRefused, match="^approval_chain_divergent: ") as refused:
        approvals.count_approvals(None, root["review_id"])
    assert (refused.value.expected, refused.value.observed) == tuple(h * 40 for h in heads)


_ANCESTORS = approvals._ancestors


@pytest.mark.parametrize("order, chain", [
    (("b", "c"), {"b": "bc", "c": "bc"}),
    (("a", "b", "c"), {"a": "a", "b": "bc", "c": "bc"}),
], ids=["two-tips-each-others-ancestor", "a-third-tip-outside-the-cycle"])
def test_tips_in_a_cycle_are_divergent_and_name_no_fork_tips(monkeypatch, order, chain):
    tips = {k + ".git": tip * 40 for k, tip in zip(FIVE, order)}
    root = _five(monkeypatch, tips)
    root["members"] = root["members"][:len(order)]
    # an ancestry no commit graph can hold: b and c are each other's ancestor
    monkeypatch.setattr(approvals, "_ancestors", lambda workspace, tip: {t * 40 for t in chain[tip[0]]})
    with pytest.raises(approvals.ApprovalRefused, match="^approval_chain_divergent: ") as refused:
        approvals.count_approvals(None, root["review_id"])
    assert str(refused.value) == "approval_chain_divergent: " + approvals.canonical(tips)
    assert (refused.value.expected, refused.value.observed) == (None, None)


def _fork(repo, monkeypatch):
    for key, value in {"GIT_AUTHOR_NAME": "Author A", "GIT_AUTHOR_EMAIL": "a@example.invalid",
                       "GIT_COMMITTER_NAME": "Author A", "GIT_COMMITTER_EMAIL": "a@example.invalid",
                       "GIT_CONFIG_NOSYSTEM": "1"}.items():
        monkeypatch.setenv(key, value)
    git(repo.parent, "init", "-q", repo.name)
    empty = git(repo, "write-tree")
    a = git(repo, "commit-tree", "-m", "a", empty)
    b = git(repo, "commit-tree", "-m", "b", "-p", a, empty)
    c = git(repo, "commit-tree", "-m", "c", "-p", a, empty)
    return {"a": a, "b": b, "c": c}


@pytest.mark.parametrize("local", ["none", "grafts", "replace"])
@pytest.mark.parametrize("order", [("b", "c"), ("a", "b", "c")], ids=["fork-tips", "fork-tips-and-base"])
def test_a_local_graft_or_replace_cycle_does_not_change_how_a_fork_reads(tmp_path, monkeypatch, local, order):
    repo = tmp_path / "ws"
    oid = _fork(repo, monkeypatch)
    if local == "grafts":
        (repo / ".git" / "info").mkdir(exist_ok=True)
        (repo / ".git" / "info" / "grafts").write_text(f"{oid['b']} {oid['c']}\n{oid['c']} {oid['b']}\n")
    elif local == "replace":
        git(repo, "replace", "--graft", oid["b"], oid["c"])
        git(repo, "replace", "--graft", oid["c"], oid["b"])
    if local != "none":
        # the cycle is live for plain git: each fork tip lists the other
        assert oid["c"] in git(repo, "rev-list", oid["b"]) and oid["b"] in git(repo, "rev-list", oid["c"])
    tips = {k + ".git": oid[t] for k, t in zip(FIVE, order)}
    root = _five(monkeypatch, tips)
    root["members"] = root["members"][:len(order)]
    monkeypatch.setattr(approvals, "_ancestors", _ANCESTORS)
    with pytest.raises(approvals.ApprovalRefused, match="^approval_chain_divergent: ") as refused:
        approvals.count_approvals(repo, root["review_id"])
    assert (refused.value.expected, refused.value.observed) == (oid["b"], oid["c"])


def test_a_linear_chain_with_a_local_replace_cycle_still_reads_incomplete(tmp_path, monkeypatch):
    repo = tmp_path / "ws"
    oid = _fork(repo, monkeypatch)
    # b already builds on a; a replacement that puts b under a closes the cycle
    git(repo, "replace", "--graft", oid["a"], oid["b"])
    assert oid["b"] in git(repo, "rev-list", oid["a"])
    tips = {"api.git": oid["a"], "app.git": oid["b"]}
    root = _five(monkeypatch, tips)
    root["members"] = root["members"][:2]
    monkeypatch.setattr(approvals, "_ancestors", _ANCESTORS)
    with pytest.raises(approvals.ApprovalRefused, match="^approvals_incomplete: ") as refused:
        approvals.count_approvals(repo, root["review_id"])
    assert (refused.value.expected, refused.value.observed) == (oid["b"], None)


def _merge(multi):
    merged = runner.invoke(app, ["review", "merge"])
    return merged, json.loads(merged.stdout)


def test_merge_refuses_a_chain_still_landing_then_merges_when_the_stamp_finishes(multi):
    set_policy(multi, 1)
    made = approve_as(multi, "Approver B")
    assert made.exit_code == 0, made.output
    tip = json.loads(made.stdout)["tip"]
    ref = approvals.PREFIX + multi["rid"]
    beta = multi["members"]["beta"]["remote"]
    git(beta, "update-ref", "-d", ref)
    rec = set_check.run_set_check(multi["workspace"], multi["rid"], "set", [sys.executable, "-c", "pass"])
    assert rec.get("result") == "pass", rec
    merged, receipt = _merge(multi)
    assert merged.exit_code == 3, merged.output
    for row in receipt["members"]:
        assert row["refusal"]["code"] == "approvals_incomplete", row
        assert row["refusal"]["expected"] == tip
    assert "not yet on beta" in merged.output and "next: " in merged.output and "retry" in merged.output
    git(multi["workspace"], "push", beta, tip + ":" + ref)  # the stamp's last push lands
    merged, receipt = _merge(multi)
    assert merged.exit_code == 0, merged.output
    assert receipt["approvals"]["count"] == 1


def test_merge_names_both_tips_of_a_forked_chain(multi):
    set_policy(multi, 1)
    made = approve_as(multi, "Approver B")
    assert made.exit_code == 0, made.output
    tip = json.loads(made.stdout)["tip"]
    previous = json.loads(git(multi["workspace"], "show", tip + ":approval.json"))
    ref = approvals.PREFIX + multi["rid"]
    forks = []
    for name, member in zip(("Approver C", "Approver D"), multi["members"].values()):
        record = {**previous, "approver": {"name": name, "key_id": ""}, "prev": approvals.digest(previous)}
        fork = _write_link(multi, record, tip)
        git(multi["workspace"], "push", member["remote"], fork + ":" + ref)
        forks.append(fork)
    rec = set_check.run_set_check(multi["workspace"], multi["rid"], "set", [sys.executable, "-c", "pass"])
    assert rec.get("result") == "pass", rec
    merged, receipt = _merge(multi)
    assert merged.exit_code == 3, merged.output
    for row in receipt["members"]:
        assert row["refusal"]["code"] == "approval_chain_divergent", row
        assert sorted([row["refusal"]["expected"], row["refusal"]["observed"]]) == sorted(forks)


def test_merge_reads_a_second_approval_still_landing_as_incomplete_then_counts_it(multi):
    set_policy(multi, 1)
    made = approve_as(multi, "Approver B")
    assert made.exit_code == 0, made.output
    tip = json.loads(made.stdout)["tip"]
    previous = json.loads(git(multi["workspace"], "show", tip + ":approval.json"))
    ref = approvals.PREFIX + multi["rid"]
    record = {**previous, "approver": {"name": "Approver C", "key_id": ""}, "prev": approvals.digest(previous)}
    second = _write_link(multi, record, tip)
    beta = multi["members"]["beta"]["remote"]
    for key, member in multi["members"].items():
        if key != "beta":  # the second stamp's first push lands; beta still has only B's link
            git(multi["workspace"], "push", member["remote"], second + ":" + ref)
    rec = set_check.run_set_check(multi["workspace"], multi["rid"], "set", [sys.executable, "-c", "pass"])
    assert rec.get("result") == "pass", rec
    merged, receipt = _merge(multi)
    assert merged.exit_code == 3, merged.output
    for row in receipt["members"]:
        assert row["refusal"]["code"] == "approvals_incomplete", row
        assert row["refusal"]["expected"] == second
    assert "1 of 2" in merged.output and "not yet on beta" in merged.output and "retry" in merged.output
    git(multi["workspace"], "push", beta, second + ":" + ref)  # the second stamp's last push lands
    merged, receipt = _merge(multi)
    assert merged.exit_code == 0, merged.output
    assert receipt["approvals"]["count"] == 2


def test_a_pr_chain_tip_reached_through_a_replaced_tag_reads_the_real_links(tmp_path, monkeypatch):
    for key in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{key}_NAME", "Author A")
        monkeypatch.setenv(f"GIT_{key}_EMAIL", "a@example.invalid")
    store, head = tmp_path / "store", "1" * 40
    git(tmp_path, "init", "-q", store)

    def link(name):
        body = json.dumps({"repo": "api", "head": head, "approver": {"name": name}})
        blob = subprocess.run(["git", "-C", str(store), "hash-object", "-w", "--stdin"], input=body,
                              capture_output=True, text=True, check=True).stdout.strip()
        tree = subprocess.run(["git", "-C", str(store), "mktree"], input=f"100644 blob {blob}\tapproval.json\n",
                              capture_output=True, text=True, check=True).stdout.strip()
        return git(store, "commit-tree", tree, "-m", name)

    real, other = link("Real Approver"), link("Never Approved")
    git(store, "tag", "-a", "-m", "real", "real", real)
    git(store, "tag", "-a", "-m", "other", "other", other)
    git(store, "update-ref", approvals.PR_PREFIX + head, git(store, "rev-parse", "real"))
    git(store, "replace", "-f", git(store, "rev-parse", "real"), git(store, "rev-parse", "other"))
    # the replacement is live for plain git: the PR ref now peels to the other commit
    assert git(store, "rev-parse", approvals.PR_PREFIX + head + "^{commit}") == other
    got = approvals.pr_head_approvals(store, "api", head)
    assert [link["approver"]["name"] for link in got["links"]] == ["Real Approver"]
    assert (got["tip"], got["count"]) == (real, 1)
    # a head with no PR ref still reads as no chain
    assert approvals.pr_head_approvals(store, "api", "2" * 40)["tip"] is None
