"""V5: every `review merge` run stores its receipt in the root `.git`, and `review show --json` prints it.

Rows reuse the merge-gate fixtures: one run per row, read back through `review show --json`, never through the
receipt the run itself returned, so a store write that is skipped cannot pass.
"""
from __future__ import annotations

import json

from gr2.python_cli import check_records, merge_gate
from gr2.python_cli.app import app
from tests.test_review_merge_gate import (  # noqa: F401  (fixtures)
    _git_identity, check, git, merge, move, runner, slice2, world,
)

REVIEW_V1 = "refs/dev.synapt.grip/__reviews__/v1/"


def show(author, review):
    out = runner.invoke(app, ["review", "show", review, "--json"])
    assert out.exit_code == 0, out.output
    return json.loads(out.output)


def test_a_merged_two_member_set_shows_commit_records_and_no_refusal(slice2):
    code, _ = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat", no_set=True)
    assert code == merge_gate.EXIT_MERGED
    view = show(slice2["author"], slice2["review"])
    assert view["merge_runs"] == 1
    receipt = view["merge"]
    assert receipt["exit"] == merge_gate.EXIT_MERGED
    by_key = {m["key"]: m for m in receipt["members"]}
    assert set(by_key) == set(slice2["members"])
    sha = slice2["review"].removeprefix("gr:")
    for k, m in slice2["members"].items():
        row = by_key[k]
        assert row["merged"] == git(m["remote"], "rev-parse", "main"), k
        assert row["records"] == {"review": REVIEW_V1 + sha, "checks": check_records.CHECK_REF}, k
        assert row["refusal"] is None, k


def test_a_refused_run_names_the_member_and_both_heads(world):
    check(world)
    (world["member"] / "moved.txt").write_text("after review\n")
    git(world["member"], "add", "moved.txt")
    git(world["member"], "commit", "-m", "moved after review")
    git(world["member"], "push", world["remote"], "feat")
    moved = git(world["member"], "rev-parse", "HEAD")
    code, _ = merge(world)
    assert code == merge_gate.EXIT_REFUSED
    receipt = show(world["author"], world["review"])["merge"]
    assert receipt["exit"] == merge_gate.EXIT_REFUSED
    row = receipt["members"][0]
    assert row["refusal"] == {"code": "feature_moved", "member": "member",
                              "expected": world["head"], "observed": moved}
    assert row["refused"].startswith("feature_moved")  # the string field is unchanged


def test_a_rerun_appends_a_run(world):
    check(world)
    assert merge(world)[0] == merge_gate.EXIT_MERGED
    assert merge(world)[0] == merge_gate.EXIT_MERGED
    view = show(world["author"], world["review"])
    assert view["merge_runs"] == 2
    assert view["merge"]["members"][0]["state"] == "already_merged"  # the latest run, not the first


def test_a_review_never_merged_shows_merge_null(world):
    view = show(world["author"], world["review"])
    assert view["merge"] is None
    assert view["merge_runs"] == 0


def test_a_partial_run_is_stored_with_exit_4(slice2):
    code, _ = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat", no_set=True)
    assert code == merge_gate.EXIT_MERGED
    beta = slice2["members"]["beta"]
    git(beta["remote"], "update-ref", "refs/heads/main", beta["base"])
    move(beta)
    code, _ = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat", no_set=True)
    assert code == merge_gate.EXIT_PARTIAL
    view = show(slice2["author"], slice2["review"])
    assert view["merge_runs"] == 2
    assert view["merge"]["exit"] == merge_gate.EXIT_PARTIAL
    beta_row = next(m for m in view["merge"]["members"] if m["key"] == "beta")
    assert beta_row["refusal"]["code"] == "feature_moved"
    assert beta_row["refusal"]["member"] == "beta"


def test_the_text_form_prints_one_merge_line_per_member(world):
    check(world)
    merge(world)
    out = runner.invoke(app, ["review", "show", world["review"]])
    assert out.exit_code == 0, out.output
    tip = git(world["remote"], "rev-parse", "main")
    assert f"merge: merged {tip[:12]}" in out.output, out.output
