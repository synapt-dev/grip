"""`check set` runs one command over every member of a review at its pinned head and records the result against the
review; `review merge` requires that record on a multi-member review, and judges the fields the producer writes
(the review and the set identity), never the name alone.

Smallest proof first: a two-member review, one command over both, one record per member, one merge.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from gr2.python_cli import check_records, merge_gate, set_check
from gr2.python_cli.app import app
from tests.test_review_merge_gate import _git_identity, git, mains, slice2  # noqa: F401  (fixtures)

runner = CliRunner()
PASS = [sys.executable, "-c", "pass"]
FAIL = [sys.executable, "-c", "import sys; sys.exit(3)"]


def run_set(s, argv=PASS, name="set"):
    return set_check.run_set_check(s["author"], s["review"], name, argv)


def merge(s, **kw):
    return merge_gate.review_merge(s["author"], s["review"], feature="feat", **kw)


def untouched(s):
    return mains(s) == {k: m["base"] for k, m in s["members"].items()}


def records(m):
    return check_records.read_remote_check(str(m["remote"]), {"path": m["repo"], "key": "x", "remote": str(m["remote"])},
                                           m["head"], ("set",))["records"]


def test_one_command_sees_every_member_and_one_record_lands_on_each_remote(slice2):
    probe = ("import json, os, sys; m = json.loads(os.environ['GR2_SET_MEMBERS']); "
             "sys.exit(0 if sorted(m) == ['alpha', 'beta'] and all(os.path.isdir(p) for p in m.values()) "
             "and os.environ['GR2_REVIEW_ID'] == " + repr(slice2["review"]) + " else 4)")
    out = run_set(slice2, [sys.executable, "-c", probe])
    assert out["result"] == "pass" and out["exit_code"] == 0, out
    assert [m["key"] for m in out["members"]] == ["alpha", "beta"]
    for m in slice2["members"].values():
        row = [r for r in records(m) if r.get("name") == "set"]
        assert len(row) == 1 and row[0]["review"] == slice2["review"] and row[0]["set_id"] == out["set_id"], row
        assert row[0]["result"] == "pass" and row[0]["observed_head"] == m["head"]


def test_a_green_set_merges_a_multi_member_review_with_the_default_requirement(slice2):
    run_set(slice2)
    code, receipt = merge(slice2)
    assert code == merge_gate.EXIT_MERGED, receipt


def test_a_red_set_is_recorded_as_fail_and_the_merge_refuses_with_no_main_moved(slice2):
    out = run_set(slice2, FAIL)
    assert out["result"] == "fail" and out["exit_code"] == 3
    code, receipt = merge(slice2)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert all(m["refused"].startswith("check_set_fail") for m in receipt["members"]), receipt
    assert untouched(slice2)


def test_D6_a_multi_member_review_requires_a_set_record_by_default(slice2):
    # slice2 already carries a passing per-member `test` record at every head: that alone must not merge a set
    code, receipt = merge(slice2)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert all(m["refused"].startswith("check_set_absent") for m in receipt["members"]), receipt
    assert untouched(slice2)


def test_D6_an_explicit_check_test_still_requires_the_set(slice2):
    code, receipt = merge(slice2, required_checks=("test",))
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert receipt["members"][0]["refused"].startswith("check_set_absent"), receipt


def test_D6_no_set_is_the_stated_way_out(slice2):
    code, receipt = merge(slice2, required_checks=("test",), no_set=True)
    assert code == merge_gate.EXIT_MERGED, receipt


def test_D6_a_single_member_review_does_not_need_a_set(slice2):
    # the default for ONE member is the old default; required_check_names is the whole rule
    assert merge_gate.required_check_names(None, 1, False) == ("test",)
    assert merge_gate.required_check_names(None, 2, False) == ("set",)
    assert merge_gate.required_check_names(("test",), 2, False) == ("test", "set")
    assert merge_gate.required_check_names(("test",), 2, True) == ("test",)


def test_R6_a_forged_record_named_set_does_not_satisfy_the_gate(slice2):
    # `check run --name set -- true` writes the name and none of the fields only the producer writes
    for m in slice2["members"].values():
        check_records.run_check(m["repo"], str(m["remote"]), m["head"], "set", PASS)
    code, receipt = merge(slice2)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert all(m["refused"].startswith("check_set_stale") for m in receipt["members"]), receipt
    assert untouched(slice2)


def test_R7_a_partial_publication_names_who_has_a_record_and_the_merge_refuses(slice2, monkeypatch):
    real = check_records.publish_observation
    beta = str(slice2["members"]["beta"]["remote"])

    def flaky(repo, remote, row):
        if str(remote) == beta:
            raise check_records.CheckRefused("remote unreachable")
        return real(repo, remote, row)

    monkeypatch.setattr(check_records, "publish_observation", flaky)
    with pytest.raises(check_records.CheckRefused, match=r"set_publication_incomplete: published to \['alpha'\], failed at beta"):
        run_set(slice2)
    monkeypatch.setattr(check_records, "publish_observation", real)
    code, receipt = merge(slice2)
    assert code == merge_gate.EXIT_REFUSED, receipt
    by = {m["key"]: m.get("refused") for m in receipt["members"]}
    assert by["beta"].startswith("check_set_absent") and untouched(slice2), by


def test_R8_a_command_that_commits_into_a_member_is_refused_and_leaves_no_record(slice2):
    commit = ("import os, subprocess; d = os.path.join(os.environ['GR2_SET_ROOT'], 'alpha'); "
              "open(os.path.join(d, 'x.txt'), 'w').write('x'); "
              "subprocess.check_call(['git', '-C', d, 'add', 'x.txt']); "
              "subprocess.check_call(['git', '-c', 'user.name=t', '-c', 'user.email=t@x.invalid', '-C', d, 'commit', '-m', 'x'])")
    with pytest.raises(check_records.CheckRefused, match="check_execution_head_changed: alpha"):
        run_set(slice2, [sys.executable, "-c", commit])
    assert all(not [r for r in records(m) if r.get("name") == "set"] for m in slice2["members"].values())


def test_R9_the_loose_package_fixture_needs_no_install_step(slice2):
    # slice2's members are bare payload files with no pyproject: the lane is a directory tree, nothing is installed
    out = run_set(slice2, [sys.executable, "-c",
                           "import os; assert open(os.path.join(os.environ['GR2_SET_ROOT'], 'beta', 'payload.txt')).read() == 'beta reviewed\\n'"])
    assert out["result"] == "pass", out


def test_a_set_that_failed_for_this_review_blocks_even_beside_a_later_pass_of_another_set():
    me, other = "gr:aaa", "gr:bbb"
    rows = [dict(name="set", result="fail", review=me, set_id="s1"), dict(name="set", result="pass", review=other, set_id="s2")]
    assert set_check.judge_set_records(rows, me, "s1")[0] == "fail"
    assert set_check.judge_set_records(rows[1:], me, "s1")[0] == "stale", "another review's pass does not satisfy"
    assert set_check.judge_set_records([dict(name="set", result="fail", review=other, set_id="s2")] + rows[:0], me, "s1")[0] == "stale", \
        "another review's failure does not block"
    assert set_check.judge_set_records([], me, "s1")[0] == "absent"
    assert set_check.judge_set_records([dict(name="test", result="pass")], me, "s1")[0] == "absent"


def test_the_set_identity_depends_on_every_key_and_head_and_not_on_order():
    a = [{"key": "alpha", "head": "1" * 40}, {"key": "beta", "head": "2" * 40}]
    assert set_check.set_id(a) == set_check.set_id(list(reversed(a)))
    assert set_check.set_id(a) != set_check.set_id([a[0], {"key": "beta", "head": "3" * 40}])
    assert set_check.set_id(a) != set_check.set_id([{"key": "alpha", "head": "1" * 40}, {"key": "gamma", "head": "2" * 40}])


def test_D7_check_show_with_a_review_reads_another_reviews_set_record_as_stale(slice2):
    run_set(slice2)
    m = slice2["members"]["alpha"]

    def show(*extra):
        res = runner.invoke(app, ["check", "show", str(m["repo"]), "--remote", str(m["remote"]), "--head", m["head"],
                                  "--require", "set", *extra])
        return res.exit_code, json.loads(res.output.strip().splitlines()[-1])

    code, body = show()
    assert code == 0 and body["status"] == "pass", body
    code, body = show("--review", slice2["review"])
    assert code == 0 and body["status"] == "pass", body
    code, body = show("--review", "gr:" + "f" * 40)
    assert body["status"] == "absent" and body["reason"] == "required_check_stale", body
    res = runner.invoke(app, ["check", "show", str(m["repo"]), "--remote", str(m["remote"]), "--head", m["head"],
                              "--review", slice2["review"]])
    assert res.exit_code == 2 and "review_applies_to_set" in res.output


def test_the_cli_verb_prints_the_record_and_exits_by_result(slice2):
    ok = runner.invoke(app, ["check", "set", str(slice2["author"]), slice2["review"], "--", *PASS])
    assert ok.exit_code == 0, ok.output
    body = json.loads(ok.output.strip().splitlines()[-1])
    assert body["result"] == "pass" and "output_tail" not in body
    bad = runner.invoke(app, ["check", "set", str(slice2["author"]), slice2["review"], "--", *FAIL])
    assert bad.exit_code == 1, bad.output


def test_the_review_help_lists_check_set_as_a_step():
    out = runner.invoke(app, ["review", "--help"])
    assert out.exit_code == 0, out.output
    assert "gr2 check set" in " ".join(out.output.split()), out.output


def test_the_workspace_may_come_from_dash_C_like_the_other_review_head_verbs(slice2):
    ok = runner.invoke(app, ["check", "set", slice2["review"], "-C", str(slice2["author"]), "--", *PASS])
    assert ok.exit_code == 0, ok.output
    assert json.loads(ok.output.strip().splitlines()[-1])["result"] == "pass"


def test_a_command_with_no_review_before_the_dashes_is_refused_by_name(slice2):
    out = runner.invoke(app, ["check", "set", "-C", str(slice2["author"]), "--", *PASS])
    assert out.exit_code == 2 and "a review is required before the command" in out.output, out.output


def _next_lines(receipt):
    return [m["next"] for m in receipt["members"] if m.get("next")]


def test_the_set_absent_refusal_names_the_safe_command_with_this_review_and_never_no_set(slice2):
    code, receipt = merge(slice2)
    assert code == merge_gate.EXIT_REFUSED, receipt
    lines = _next_lines(receipt)
    assert len(lines) == 2, lines
    for line in lines:
        assert "gr2 check set" in line and slice2["review"] in line and "--no-set" not in line, line


def test_an_honest_set_with_a_real_failing_member_check_is_refused_on_that_check_and_points_at_the_set(slice2):
    # Real per-member pytest of a coupled set is red in isolation: each member imports its siblings. Beta's own `test` is
    # recorded red here, and the set check, run over every member together, is green.
    beta = slice2["members"]["beta"]
    check_records.run_check(beta["repo"], str(beta["remote"]), beta["head"], "test", FAIL)
    run_set(slice2)
    code, receipt = merge(slice2, required_checks=("test",))
    assert code == merge_gate.EXIT_REFUSED, receipt
    by = {m["key"]: m for m in receipt["members"]}
    assert by["beta"]["refused"].startswith("check_fail"), by["beta"]
    assert untouched(slice2), "nothing may merge"
    hint = by["beta"]["next"]
    assert "gr2 check set" in hint and slice2["review"] in hint and "--check set" in hint and "--no-set" not in hint, hint
    # and following the hint merges it: the set was green, and the member's own isolated check is not what was asked for
    code, receipt = merge(slice2, required_checks=("set",))
    assert code == merge_gate.EXIT_MERGED, receipt


def test_a_single_member_review_keeps_the_plain_check_fail_advice():
    from gr2.python_cli import next_steps
    line = next_steps.merge_row("check_fail: x", workspace="/ws", review="gr:abc", remote="r", path="p", head="h",
                                checks=("test",))
    assert "gr2 check set" not in line and line.startswith("next: the check failed at this head; fix it")


def test_F1_a_red_set_is_not_cleared_by_a_green_rerun_and_the_refusal_says_so(slice2):
    run_set(slice2, FAIL)
    run_set(slice2, PASS)  # a rerun at the same heads: the red record is still there
    code, receipt = merge(slice2)
    assert code == merge_gate.EXIT_REFUSED, receipt
    lines = _next_lines(receipt)
    assert len(lines) == 2, lines
    for line in lines:
        assert "new head" in line and "does not clear" in line, line
        assert "-- <your combined test command>" not in line, "a bare rerun instruction cannot clear a recorded failure: " + line
    assert untouched(slice2)


def _show(m, *extra, remote=None):
    res = runner.invoke(app, ["check", "show", str(m["repo"]), "--remote", remote or str(m["remote"]), "--head", m["head"],
                              "--require", "set", *extra])
    return res.exit_code, json.loads(res.output.strip().splitlines()[-1])


def test_F2_show_and_the_gate_judge_the_same_records_a_forged_name_only_fail_beside_a_valid_pass(slice2):
    run_set(slice2)
    for m in slice2["members"].values():  # `check run --name set -- false`: the name and none of the producer's fields
        check_records.run_check(m["repo"], str(m["remote"]), m["head"], "set", FAIL)
    code, body = _show(slice2["members"]["alpha"], "--review", slice2["review"])
    assert code == 0 and body["status"] == "pass", body
    code, receipt = merge(slice2)
    assert code == merge_gate.EXIT_MERGED, receipt  # the gate agrees: a stale failure does not block


def test_F3_a_transport_fault_under_review_stays_a_fault_not_an_absence(slice2):
    code, body = _show(slice2["members"]["alpha"], "--review", slice2["review"], remote="/nonexistent/remote.git")
    assert code == 2 and body["status"] == "fail", (code, body)
    control_code, control = _show(slice2["members"]["alpha"], remote="/nonexistent/remote.git")
    assert control_code == 2 and control["status"] == "fail", "the same fault without --review is the control"


def test_a_set_record_for_this_review_but_another_set_identity_does_not_satisfy_the_gate(slice2):
    # same review id, a set identity this review does not have: only the set_id comparison can refuse it
    for m in slice2["members"].values():
        check_records.publish_observation(m["repo"], str(m["remote"]), dict(
            v=1, head=m["head"], observed_head=m["head"], name="set", result="pass", exit_code=0,
            review=slice2["review"], set_id="0" * 64, members=[]))
    code, receipt = merge(slice2)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert all(m["refused"].startswith("check_set_stale") for m in receipt["members"]), receipt
    assert untouched(slice2)


def test_a_transport_fault_reading_the_checks_is_named_by_the_gate_not_reported_as_an_absent_set(slice2, monkeypatch):
    real = check_records.read_remote_check

    def faulty(remote, member, head, required=("test",)):
        out = real(remote, member, head, required)
        out.update(status="fail", reason="cannot_measure_check_ref: boom", records=[])
        return out

    monkeypatch.setattr(check_records, "read_remote_check", faulty)
    code, receipt = merge(slice2)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert all(m["refused"] == "check_fail: cannot_measure_check_ref: boom" for m in receipt["members"]), receipt
    assert untouched(slice2)
