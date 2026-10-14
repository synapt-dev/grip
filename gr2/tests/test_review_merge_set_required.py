"""A review over several members merges only on a passing `set` record: the members tested together at the pinned heads.

Measured in a multi-agent pilot (gitgrip 2.0.0a9): a three-member feature merged with two real approvals and NO `set`
record. The author pinned with --rows-json and no source, so `check set` could not run (row_carries_no_objects); a
per-member `test` record was red; the author recorded a check under a new name and merged with
`review merge --no-set --check unit`. The requirement was the merging author's to choose. Here it is the bind's.
"""
from __future__ import annotations

import json
import sys

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
                                            required_checks=("unit",), no_set=True)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert mains(slice2) == bases(slice2), "no member may merge"
    assert receipt["refused"].startswith("set_required"), receipt


def test_no_set_alone_refuses_on_a_multi_member_review(slice2):  # noqa: F811
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat", no_set=True)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert mains(slice2) == bases(slice2)
    assert receipt["refused"].startswith("set_required"), receipt


def test_a_check_name_the_workspace_did_not_configure_refuses_even_beside_a_passing_set(slice2):  # noqa: F811
    record_set(slice2)
    for m in slice2["members"].values():
        check_records.run_check(m["repo"], str(m["remote"]), m["head"], "unit", PASS)
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat",
                                            required_checks=("unit", "set"))
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert mains(slice2) == bases(slice2)
    assert receipt["refused"].startswith("check_not_configured") and "unit" in receipt["refused"], receipt


def test_control_a_passing_set_merges_with_the_default_requirement(slice2):  # noqa: F811
    record_set(slice2)
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat")
    assert code == merge_gate.EXIT_MERGED, receipt


def test_control_a_single_member_review_still_takes_no_set(world):  # noqa: F811
    check_records.run_check(world["member"], str(world["remote"]), world["head"], "test", PASS)
    code, receipt = merge_gate.review_merge(world["author"], world["review"], into="main", feature="feat", no_set=True)
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
