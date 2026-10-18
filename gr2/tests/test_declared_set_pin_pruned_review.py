"""A review the remote advertises but can no longer serve (its oid pruned) does not hide the others.

The tie reads every review on a sibling's remote in one fetch by id. When one oid is gone that fetch fails whole, so the
tie falls back to one fetch per review: a review that still reads and names the pin ties it, as the review-by-review
read did before. When none does, the refusal names the ref(s) whose oid failed and prints git's fatal line once."""
from __future__ import annotations

import subprocess

from gr2.python_cli import merge_gate
from tests.test_declared_set_pins import _move_beta, _refusal
from tests.test_declared_sets import merge
from tests.test_declared_sets import PASS, record, untouched, world  # noqa: F401  (fixtures)

PRUNED = "refs/dev.synapt.grip/__reviews__/zz-pruned"
GONE = "d" * 40


def _plant_pruned(world, ref=PRUNED):
    """beta's remote advertises a review whose objects it no longer has (a ref file written by hand)."""
    remote = world["members"]["beta"]["remote"]
    path = remote / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(GONE + "\n")
    shown = subprocess.run(["git", "ls-remote", str(remote), ref], capture_output=True, text=True).stdout
    assert shown.split() == [GONE, ref], shown  # was there: the remote advertises it


def test_a_pruned_review_does_not_block_a_pin_another_review_names(world):
    assert record(world, PASS)["result"] == "pass"
    _plant_pruned(world)  # after the record: a push to a remote with a broken ref fails on its own

    code, receipt = merge(world, "alpha")

    assert code == merge_gate.EXIT_MERGED, receipt


def test_a_pruned_review_with_no_other_match_refuses_naming_the_ref_and_git_once(world):
    moved = _move_beta(world, push=True)
    _plant_pruned(world)  # after the record, as above

    code, why, receipt = _refusal(world)

    assert code == merge_gate.EXIT_REFUSED, receipt
    assert why.startswith(f"declared_set_pin_unmeasured: beta's pin {moved[:12]} "), why
    assert PRUNED in why, why
    assert why.count("not our ref") == 1 and "\n" not in why and "\\n" not in why, why  # git's fatal line, once
    assert untouched(world)


def test_a_pruned_review_ref_with_a_bidi_override_is_printed_escaped(world):
    moved = _move_beta(world, push=True)
    _plant_pruned(world, PRUNED + "\u202e")

    code, why, receipt = _refusal(world)

    assert code == merge_gate.EXIT_REFUSED, receipt
    assert "\u202e" not in why and "zz-pruned\\u202e" in why, why
    assert untouched(world)
