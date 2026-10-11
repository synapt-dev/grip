"""A declared set's OTHER members are tied to heads somebody reviewed or merged, not to whatever the root commit pins.

THE HOLE (stated in the declared-sets change): the gate judged the member under review against the set's record, and
every other member of the set only by what the root commit pinned. A root commit can pin beta at a commit that exists
nowhere but the author's clone, or one that was pushed and never looked at, and alpha's merge then proceeded on the
strength of a pin nobody had read.

THE SPECIFIED SHAPE: each other member's pin must be merged into that member's target on its remote, or a review
published on that remote must name that member at exactly that head. A commit that merely exists remotely is the easy
case of the same rule and refuses too.
"""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from gr2.python_cli import check_records, declared_sets, merge_gate
from gr2.python_cli.app import app
from tests.test_declared_sets import PASS, SET, merge, record, store_commit, untouched, world  # noqa: F401
from tests.test_review_merge_gate import git

runner = CliRunner()


def _move_beta(world, push: bool):
    """beta gains a commit nobody reviewed; the store pins it, and the set is recorded over that root commit."""
    beta = world["members"]["beta"]
    (beta["repo"] / "payload.txt").write_text("beta, changed after review\n")
    git(beta["repo"], "add", "payload.txt")
    git(beta["repo"], "commit", "-q", "-m", "beta moved after review")
    moved = git(beta["repo"], "rev-parse", "HEAD")
    if push:
        git(beta["repo"], "push", "origin", "feat")
    if push:
        root = store_commit(world["author"], "beta moved")
    else:
        # `store commit` itself refuses a pin that is not on origin ("push it first"), so a local-only pin reaches the
        # gate only in a root commit somebody built by hand; the gate must not trust the commit for it.
        git(world["author"], "update-index", "--cacheinfo", f"160000,{moved},beta")
        git(world["author"], "commit", "-q", "--no-verify", "-m", "beta pinned at a local-only commit")
        root = git(world["author"], "rev-parse", "HEAD")
    assert record(world, PASS, commit=root)["result"] == "pass"
    return moved


def _refusal(world):
    code, receipt = merge(world, "alpha")
    return code, receipt["members"][0]["refused"] or "", receipt


def test_a_pin_that_exists_only_in_a_local_clone_refuses_the_other_members_merge(world):
    moved = _move_beta(world, push=False)

    code, why, receipt = _refusal(world)

    assert code == merge_gate.EXIT_REFUSED, receipt
    assert why.startswith("declared_set_pin_unreviewed") and "beta" in why and moved[:12] in why, why
    assert untouched(world)


def test_a_pin_pushed_to_the_remote_with_no_review_at_that_head_refuses_too(world):
    """Existing remotely is not reviewed: the review that covers beta names beta's OLD head."""
    moved = _move_beta(world, push=True)

    code, why, receipt = _refusal(world)

    assert code == merge_gate.EXIT_REFUSED, receipt
    assert why.startswith("declared_set_pin_unreviewed") and "beta" in why and moved[:12] in why, why
    assert untouched(world)


def test_a_pin_at_a_commit_already_merged_into_the_target_is_allowed(world):
    """The base is merged by definition; a root commit that leaves beta at its base is the ordinary partial change."""
    author, beta = world["author"], world["members"]["beta"]
    git(author, "update-index", "--cacheinfo", f"160000,{beta['base']},beta")
    git(author, "commit", "-q", "--no-verify", "-m", "beta pinned at its base")
    root = git(author, "rev-parse", "HEAD")
    assert record(world, PASS, commit=root)["result"] == "pass"

    code, receipt = merge(world, "alpha")

    assert code == merge_gate.EXIT_MERGED, receipt


def test_a_review_that_names_another_member_at_that_head_does_not_cover_this_members_pin(world):
    """A review is read for the MEMBER it names: beta pinned at gamma's reviewed commit is not a reviewed beta, even when
    gamma's review sits on beta's own remote."""
    author, beta, gamma = world["author"], world["members"]["beta"], world["members"]["gamma"]
    git(beta["repo"], "fetch", str(gamma["repo"]), gamma["head"])  # the record needs beta's repo to hold the commit
    published = runner.invoke(app, ["review", "publish", gamma["review"], "--remote", str(beta["remote"])])
    assert published.exit_code == 0, published.output
    git(author, "update-index", "--cacheinfo", f"160000,{gamma['head']},beta")
    git(author, "commit", "-q", "--no-verify", "-m", "beta pinned at gamma's reviewed commit")
    root = git(author, "rev-parse", "HEAD")
    assert record(world, PASS, commit=root)["result"] == "pass"

    code, why, receipt = _refusal(world)

    assert code == merge_gate.EXIT_REFUSED, receipt
    assert why.startswith("declared_set_pin_unreviewed") and "beta" in why, why
    assert untouched(world)


def _commit_mode(world, mode: str):
    """The working grip.toml gains a top-level `mode`, committed by hand (store commit does not carry the key)."""
    author = world["author"]
    text = (author / "grip.toml").read_text()
    (author / "grip.toml").write_text(f'mode = "{mode}"\n' + text)
    git(author, "add", "grip.toml")
    git(author, "commit", "-q", "--no-verify", "-m", f"mode {mode}")
    return git(author, "rev-parse", "HEAD")


def test_the_gate_refuses_a_declared_set_member_by_name_when_grip_toml_says_full_mode(world):
    """A full-mode member entry is a gitlink to a FIELD-COMMIT; this reader would take its id for the member's pin."""
    assert record(world, PASS)["result"] == "pass"
    _commit_mode(world, "full")

    code, why, receipt = _refusal(world)

    assert code == merge_gate.EXIT_REFUSED, receipt
    assert why.startswith("declared_set_mode_full"), why
    assert untouched(world)


def test_the_set_check_refuses_by_name_at_a_full_mode_root_commit(world):
    full = _commit_mode(world, "full")

    with pytest.raises(check_records.CheckRefused, match="declared_set_mode_full"):
        record(world, PASS, commit=full)


def test_a_mode_that_is_neither_compat_nor_full_is_an_invalid_declaration():
    with pytest.raises(declared_sets.DeclaredSetError, match="mode must be"):
        declared_sets.parse({"mode": "fulll"})


def test_a_pin_that_a_published_review_names_at_that_exact_head_is_allowed(world):
    """The fixture's own state: every member is pinned at the head its published review names."""
    assert record(world, PASS)["result"] == "pass"

    code, receipt = merge(world, "alpha")

    assert code == merge_gate.EXIT_MERGED, receipt
