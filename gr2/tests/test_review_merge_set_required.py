"""A review over several members merges only on a passing `set` record: the members tested together at the pinned heads.

Measured in a multi-agent pilot (gitgrip 2.0.0a9): a three-member feature merged with two real approvals and NO `set`
record. The author pinned with --rows-json and no source, so `check set` could not run (row_carries_no_objects); a
per-member `test` record was red; the author recorded a check under a new name and merged with
`review merge --no-set --check unit`. The requirement was the merging author's to choose. Here it is the bind's.
"""
from __future__ import annotations

import json
import sys

import pytest

from gr2.python_cli import check_records, merge_gate, set_check
from gr2.python_cli.app import app
from tests.review_ref_helper import REVIEW_REF_PREFIX
from tests.test_review_merge_gate import git, mains, runner, slice2, world  # noqa: F401  (fixtures)

PASS = [sys.executable, "-c", "pass"]


def bases(s):
    return {k: m["base"] for k, m in s["members"].items()}


def record_set(s):
    rec = set_check.run_set_check(s["author"], s["review"], "set", PASS)
    assert rec.get("result") == "pass", rec


def test_the_pilot_merge_no_set_with_a_check_of_the_authors_naming_refuses_and_nothing_lands(slice2):  # noqa: F811
    for m in slice2["members"].values():
        check_records.run_check(m["repo"], str(m["remote"]), m["head"], "unit", PASS)
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat",
                                            required_checks=("unit",), no_set=True, required_approvals=0)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert mains(slice2) == bases(slice2), "no member may merge"
    assert receipt["refused"].startswith("set_required"), receipt


def test_no_set_alone_refuses_on_a_multi_member_review(slice2):  # noqa: F811
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat", no_set=True, required_approvals=0)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert mains(slice2) == bases(slice2)
    assert receipt["refused"].startswith("set_required"), receipt


def test_a_check_name_the_workspace_did_not_configure_refuses_even_beside_a_passing_set(slice2):  # noqa: F811
    record_set(slice2)
    for m in slice2["members"].values():
        check_records.run_check(m["repo"], str(m["remote"]), m["head"], "unit", PASS)
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat",
                                            required_checks=("unit", "set"), required_approvals=0)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert mains(slice2) == bases(slice2)
    assert receipt["refused"].startswith("check_not_configured") and "unit" in receipt["refused"], receipt


def test_control_a_passing_set_merges_with_the_default_requirement(slice2):  # noqa: F811
    record_set(slice2)
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat", required_approvals=0)
    assert code == merge_gate.EXIT_MERGED, receipt


def test_control_a_single_member_review_still_takes_no_set(world):  # noqa: F811
    check_records.run_check(world["member"], str(world["remote"]), world["head"], "test", PASS)
    code, receipt = merge_gate.review_merge(world["author"], world["review"], into="main", feature="feat", no_set=True, required_approvals=0)
    assert code == merge_gate.EXIT_MERGED, receipt


def _fresh_heads_rows(s, tmp_path, with_source):
    rows = []
    for name, m in s["members"].items():
        (m["repo"] / "second.txt").write_text(f"{name} second\n")
        git(m["repo"], "add", "second.txt")
        git(m["repo"], "commit", "-m", "second change")
        row = {"key": name, "path": name, "remote": str(m["remote"]), "base": m["base"],
               "head": git(m["repo"], "rev-parse", "HEAD"), "ref": "refs/heads/main"}
        if with_source(name):
            row["source"] = str(m["repo"])
        rows.append(row)
    p = tmp_path / "rows-nosource.json"
    p.write_text(json.dumps(rows))
    return p


def _review_refs(author):
    return set(git(author, "for-each-ref", "--format=%(refname)", REVIEW_REF_PREFIX).split())


def test_a_multi_member_pin_whose_rows_carry_no_source_refuses_and_mints_no_review(slice2, tmp_path):  # noqa: F811
    before = _review_refs(slice2["author"])
    rows = _fresh_heads_rows(slice2, tmp_path, with_source=lambda name: False)
    out = runner.invoke(app, ["review", "pin", "--rows-json", str(rows)])
    assert out.exit_code != 0, out.output
    assert "source" in out.output and "check set" in out.output, out.output
    assert _review_refs(slice2["author"]) == before, "no review id may be minted"


def test_one_row_without_a_source_is_enough_to_refuse_the_pin(slice2, tmp_path):  # noqa: F811
    before = _review_refs(slice2["author"])
    rows = _fresh_heads_rows(slice2, tmp_path, with_source=lambda name: name == "alpha")
    out = runner.invoke(app, ["review", "pin", "--rows-json", str(rows)])
    assert out.exit_code != 0, out.output
    assert "beta" in out.output, out.output
    assert _review_refs(slice2["author"]) == before


# `--check X` on a review of two or more members: a record for X written by `check set --name X` for THIS review at
# THIS set of heads decides X; the members' own X records (each tested alone) are not read for it. A record that
# carries a review or set identity but not this one is stale and never counted. `set` is still required on its own.

FAIL = [sys.executable, "-c", "raise SystemExit(1)"]


def _each(s, name, argv):
    for m in s["members"].values():
        check_records.run_check(m["repo"], str(m["remote"]), m["head"], name, argv)


def _record_set_as(s, name, argv):
    set_check.run_set_check(s["author"], s["review"], name, argv)


def _set_fields(s):
    m = next(iter(s["members"].values()))
    rows = check_records.read_remote_check(str(m["remote"]), {"path": str(m["repo"]), "key": None, "remote": str(m["remote"])},
                                           m["head"], ("set",))["records"]
    row = next(r for r in rows if r["name"] == "set")
    return row["review"], row["set_id"], row["members"]


def _forge(s, name, review, sid, members):
    for m in s["members"].values():
        row = dict(v=1, head=m["head"], observed_head=m["head"], name=name, result="pass", exit_code=0,
                   review=review, set_id=sid, members=members, reproduced=False)
        check_records.publish_observation(m["repo"], str(m["remote"]), row)


def _merge_test(s, **kw):
    return merge_gate.review_merge(s["author"], s["review"], feature="feat", required_checks=("test",),
                                   required_approvals=0, **kw)


def test_a_test_run_as_the_set_decides_test_when_each_member_alone_fails(slice2):  # noqa: F811
    _each(slice2, "test", FAIL)
    _record_set_as(slice2, "test", PASS)
    record_set(slice2)
    code, receipt = _merge_test(slice2)
    assert code == merge_gate.EXIT_MERGED, receipt
    assert all(row["check"]["source"] == {"test": "set"} for row in receipt["members"]), receipt


def test_a_test_set_record_for_another_review_is_stale_and_does_not_rescue_a_failed_test(slice2):  # noqa: F811
    _each(slice2, "test", FAIL)
    record_set(slice2)
    review, sid, members = _set_fields(slice2)
    _forge(slice2, "test", "gr:another-review", sid, members)
    code, receipt = _merge_test(slice2)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert mains(slice2) == bases(slice2)
    assert all((r["refused"] or "").startswith("check_fail: required_check_failed") for r in receipt["members"]), receipt
    assert all(row["check"]["source"] == {"test": "member"} for row in receipt["members"]), receipt


def test_a_test_set_record_with_this_review_but_another_set_id_is_stale_and_does_not_rescue_a_failed_test(slice2):  # noqa: F811
    _each(slice2, "test", FAIL)
    record_set(slice2)
    review, sid, members = _set_fields(slice2)
    _forge(slice2, "test", review, "0" * 64, members)
    code, receipt = _merge_test(slice2)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert mains(slice2) == bases(slice2)
    assert all((r["refused"] or "").startswith("check_fail: required_check_failed") for r in receipt["members"]), receipt
    assert all(row["check"]["source"] == {"test": "member"} for row in receipt["members"]), receipt


@pytest.fixture
def slice2_no_test(request):
    """slice2 without the member-own passing `test` record the shared fixture seeds at each head."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(check_records, "run_check", lambda *a, **k: None)
        s = request.getfixturevalue("slice2")
    return s


def _test_rows(s):
    out = []
    for m in s["members"].values():
        rows = check_records.read_remote_check(str(m["remote"]), {"path": str(m["repo"]), "key": None, "remote": str(m["remote"])},
                                               m["head"], ("test",))["records"]
        out += [r for r in rows if r["name"] == "test"]
    return out


def _stale_alone(s, review, sid, members):
    _forge(s, "test", review, sid, members)
    rows = _test_rows(s)
    assert rows and all("review" in r for r in rows), "the only test records must be the stale ones"
    assert all(r["result"] == "pass" for r in rows), rows
    code, receipt = _merge_test(s)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert mains(s) == bases(s), "no member may merge"
    assert all((r["refused"] or "").startswith("check_test_stale") for r in receipt["members"]), receipt


def test_a_passing_test_record_for_another_review_alone_is_stale_and_refuses(slice2_no_test):
    record_set(slice2_no_test)
    _, sid, members = _set_fields(slice2_no_test)
    _stale_alone(slice2_no_test, "gr:another-review", sid, members)


def test_a_passing_test_record_with_this_review_but_another_set_id_alone_is_stale_and_refuses(slice2_no_test):
    record_set(slice2_no_test)
    review, _, members = _set_fields(slice2_no_test)
    _stale_alone(slice2_no_test, review, "0" * 64, members)


def test_a_failed_test_set_record_refuses_even_beside_passing_member_records(slice2):  # noqa: F811
    _each(slice2, "test", PASS)
    _record_set_as(slice2, "test", FAIL)
    record_set(slice2)
    code, receipt = _merge_test(slice2)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert mains(slice2) == bases(slice2)
    assert all((r["refused"] or "").startswith("check_fail: the test check failed as a set") for r in receipt["members"]), receipt


def test_a_test_set_record_does_not_stand_in_for_set(slice2):  # noqa: F811
    _record_set_as(slice2, "test", PASS)
    code, receipt = _merge_test(slice2)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert mains(slice2) == bases(slice2)
    assert all((r["refused"] or "").startswith("check_set_absent") for r in receipt["members"]), receipt


def test_a_single_member_review_still_reads_its_own_test_record(world):  # noqa: F811
    check_records.run_check(world["member"], str(world["remote"]), world["head"], "test", FAIL)
    set_check.run_set_check(world["author"], world["review"], "test", PASS)
    code, receipt = merge_gate.review_merge(world["author"], world["review"], into="main", feature="feat",
                                            required_checks=("test",), no_set=True, required_approvals=0)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert all((r["refused"] or "").startswith("check_fail: required_check_failed") for r in receipt["members"]), receipt
