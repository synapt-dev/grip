"""`check set --at-remote-heads <branch>`: after a member moves, one command checks the set at the heads the remotes
name NOW, records the result as `remote_set`, and changes nothing a merge reads.

Smallest proof first: the two-member review, one member moved from ANOTHER clone (so the workspace does not hold the
new commit until the check fetches it), one command, a non-zero exit that names both current heads.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gr2.python_cli import check_records, merge_gate, set_check
from gr2.python_cli.app import app
from tests.test_review_merge_gate import _git_identity, git, mains, slice2  # noqa: F401  (fixtures)

runner = CliRunner()
# Passes while beta's payload is the reviewed text; a breaking move rewrites it.
BETA_AS_REVIEWED = [sys.executable, "-c",
                    "import os, sys; p = os.path.join(os.environ['GR2_SET_ROOT'], 'beta', 'payload.txt'); "
                    "sys.exit(0 if open(p).read() == 'beta reviewed\\n' else 1)"]


def move_elsewhere(tmp_path, m, name, payload=None):
    """Push a new commit to the member's `feat` from a separate clone, as a teammate would; returns the new head."""
    other = tmp_path / f"other-{name}"
    git(tmp_path, "clone", "--quiet", "--branch", "feat", m["remote"], other)
    if payload is None:
        (other / "moved.txt").write_text("after review\n")
        git(other, "add", "moved.txt")
    else:
        (other / "payload.txt").write_text(payload)
        git(other, "add", "payload.txt")
    git(other, "commit", "-m", "moved after review")
    git(other, "push", "origin", "feat")
    return git(other, "rev-parse", "HEAD")


def remote_set_rows(m, head):
    return [r for r in check_records.read_remote_check(
        str(m["remote"]), {"path": m["repo"], "key": "x", "remote": str(m["remote"])}, head, ("remote_set",))["records"]
        if r.get("name") == "remote_set"]


def at_remote(s, argv=BETA_AS_REVIEWED, branch="feat"):
    return set_check.run_set_check_at_remote_heads(s["author"], s["review"], branch, argv)


def test_a_breaking_move_is_checked_at_the_current_heads_and_recorded_without_moving_anything(slice2, tmp_path):
    beta = slice2["members"]["beta"]
    moved = move_elsewhere(tmp_path, beta, "beta", payload="beta broken\n")
    local_feat = {k: git(m["repo"], "rev-parse", "feat") for k, m in slice2["members"].items()}

    out = at_remote(slice2)

    assert out["result"] == "fail" and out["exit_code"] == 1, out
    by = {m["key"]: m for m in out["members"]}
    assert by["beta"]["head"] == moved and by["beta"]["moved"] is True and by["beta"]["pinned_head"] == beta["head"]
    assert by["alpha"]["head"] == slice2["members"]["alpha"]["head"] and by["alpha"]["moved"] is False
    for key, m in slice2["members"].items():
        rows = remote_set_rows(m, by[key]["head"])
        assert len(rows) == 1 and rows[0]["result"] == "fail" and rows[0]["review"] == slice2["review"], rows
        assert rows[0]["heads"] == sorted(f"{k}:{v['head']}" for k, v in by.items())
        assert git(m["repo"], "rev-parse", "feat") == local_feat[key], "no member branch moves"
        assert git(m["repo"], "for-each-ref", "refs/dev.synapt.grip/__remote_head_transfers__") == ""
    assert mains(slice2) == {k: m["base"] for k, m in slice2["members"].items()}


def test_a_benign_move_passes_at_the_current_heads(slice2, tmp_path):
    moved = move_elsewhere(tmp_path, slice2["members"]["beta"], "beta")
    out = at_remote(slice2)
    assert out["result"] == "pass", out
    assert {m["key"]: m["head"] for m in out["members"]}["beta"] == moved


def test_the_record_neither_satisfies_nor_blocks_the_merge(slice2):
    # No move: a PASS at exactly the reviewed heads, under remote_set, must not stand in for the review's set record.
    assert at_remote(slice2)["result"] == "pass"
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat")
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert all(m["refused"].startswith("check_set_absent") for m in receipt["members"]), receipt
    # And a FAIL under remote_set does not block a merge the review's own set check allows.
    assert at_remote(slice2, [sys.executable, "-c", "import sys; sys.exit(2)"])["result"] == "fail"
    set_check.run_set_check(slice2["author"], slice2["review"], "set", [sys.executable, "-c", "pass"])
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat")
    assert code == merge_gate.EXIT_MERGED, receipt


def test_the_merge_still_refuses_a_moved_member_after_the_check(slice2, tmp_path):
    move_elsewhere(tmp_path, slice2["members"]["beta"], "beta")
    assert at_remote(slice2)["result"] == "pass"
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat", no_set=True)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert {m["key"]: m.get("refused", "") for m in receipt["members"]}["beta"].startswith("feature_moved"), receipt
    assert mains(slice2) == {k: m["base"] for k, m in slice2["members"].items()}


def test_an_absent_branch_is_refused_by_name_and_records_nothing(slice2):
    with pytest.raises(check_records.CheckRefused, match=r"remote_branch_absent: 'nope'"):
        at_remote(slice2, branch="nope")
    for m in slice2["members"].values():
        assert remote_set_rows(m, m["head"]) == []


@pytest.mark.parametrize("branch", ["--upload-pack=x", "a..b", "feat~1", "@{-1}"])
def test_a_branch_that_is_not_a_branch_name_is_refused_before_any_fetch(slice2, branch, monkeypatch):
    # `@{-1}` names the root's PREVIOUS branch, which `check-ref-format --branch` expands and passes. The fixture root
    # is unborn (no HEAD reflog, so `@{-1}` cannot resolve there and the old check refused it by accident); give it a
    # commit and a previous branch, so the row meets the shape. Any fetch fails the row: the refusal must come first.
    root = slice2["author"]
    git(root, "commit", "-q", "--allow-empty", "-m", "a root with history")
    git(root, "checkout", "-q", "-b", "previous-branch")
    git(root, "checkout", "-q", "-")  # back where it was (detached or not); @{-1} now names previous-branch
    assert git(root, "rev-parse", "--symbolic-full-name", "@{-1}") == "refs/heads/previous-branch"
    monkeypatch.setattr(set_check, "_fetch_remote_head", lambda *a: pytest.fail(f"fetched for {branch!r}"))
    with pytest.raises(check_records.CheckRefused, match="branch_name_invalid"):
        at_remote(slice2, branch=branch)


def test_the_cli_exits_by_result_and_prints_every_current_head(slice2, tmp_path):
    moved = move_elsewhere(tmp_path, slice2["members"]["beta"], "beta", payload="beta broken\n")
    result = runner.invoke(app, ["check", "set", str(slice2["author"]), slice2["review"], "--at-remote-heads", "feat",
                                 "--", *BETA_AS_REVIEWED])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout.splitlines()[0])
    assert payload["name"] == "remote_set" and payload["result"] == "fail"
    assert f"alpha: {slice2['members']['alpha']['head']}\n" in result.stderr
    assert f"beta: {moved} (moved from {slice2['members']['beta']['head']})" in result.stderr


def test_the_cli_refuses_at_remote_heads_beside_a_declared_set(slice2):
    result = runner.invoke(app, ["check", "set", str(slice2["author"]), slice2["review"], "--at-remote-heads", "feat",
                                 "--set", "core", "--", "true"])
    assert result.exit_code == 2 and "at_remote_heads_takes_a_review" in result.stdout, result.output


def test_the_feature_moved_next_step_names_the_check_and_does_not_point_only_at_a_new_pin(slice2, tmp_path):
    # Before: "pin the new head with gr2 review pin", which head_already_on_remote then refuses for a pushed head.
    move_elsewhere(tmp_path, slice2["members"]["beta"], "beta")
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat", no_set=True)
    assert code == merge_gate.EXIT_REFUSED, receipt
    beta = next(m for m in receipt["members"] if m["key"] == "beta")
    line = beta["next"]
    root = Path(slice2["author"]).resolve()  # the gate names the resolved root (macOS: /private/var/...)
    assert f"gr2 check set {root} {slice2['review']} --at-remote-heads feat --" in line, line
    assert "pin it before you push" in line and "--ratified" in line, line
    # The printed check is the real verb: it runs as printed and sees the moved head.
    out = at_remote(slice2, [sys.executable, "-c", "pass"], branch="feat")
    assert out["result"] == "pass" and {m["key"]: m["moved"] for m in out["members"]}["beta"] is True


def test_a_command_that_moves_a_member_head_is_refused_and_records_nothing(slice2):
    step_back = [sys.executable, "-c",
                 "import os, subprocess; subprocess.run(['git', '-C', os.path.join(os.environ['GR2_SET_ROOT'], 'beta'), "
                 "'checkout', '-q', '--detach', 'HEAD~1'], check=True)"]
    with pytest.raises(check_records.CheckRefused, match="check_execution_head_changed"):
        at_remote(slice2, step_back)
    for m in slice2["members"].values():
        assert remote_set_rows(m, m["head"]) == []
