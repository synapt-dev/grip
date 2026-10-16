"""A review records the target ref it was pinned against, and merge and update name THAT ref after it moves.

Before the target was recorded, merge and update inferred it from what sits at the reviewed base on the remote. Once
the target moves nothing sits there, and no rule over the remote's branches tells "dev moved, and main absorbed its old
tip in a promotion" from "main moved": both guesses merged the wrong branch. These rows pin a review on a branch that is
not the remote's default (dev, release/1.x), move it, and expect base_moved on the PINNED ref and an update that merges
it, including after a promotion.
"""
from __future__ import annotations

import json
import re
import sys

from typer.testing import CliRunner

from gr2.python_cli import grip
from gr2.python_cli.app import app
from tests.test_review_merge_gate import REVIEW_REF_PREFIX, _git_identity, git, publish  # noqa: F401  (fixtures)

runner = CliRunner()


def _commit_on(member, remote, start, name, branch):
    """One commit on `start`, pushed to the remote as `branch`; returns its sha."""
    git(member, "checkout", "-q", "-b", f"tmp-{name}", start)
    (member / f"{name}.txt").write_text(name + "\n")
    git(member, "add", f"{name}.txt")
    git(member, "commit", "-q", "-m", name)
    git(member, "push", "-q", "-f", str(remote), f"tmp-{name}:{branch}")
    return git(member, "rev-parse", "HEAD")


def _pinned_on(tmp_path, monkeypatch, target, *, record=True):
    """A remote whose default is main, with `target` one commit ahead of main; a review of feat pinned on `target`.
    With record=False the bind is made the way it was before the target was recorded."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    remote, seed, author = tmp_path / "remote.git", tmp_path / "seed", tmp_path / "author"
    git(tmp_path, "init", "--bare", "-b", "main", remote)
    seed.mkdir()
    git(seed, "init", "-b", "main")
    (seed / "payload.txt").write_text("m0\n")
    git(seed, "add", "payload.txt")
    git(seed, "commit", "-m", "m0")
    git(seed, "push", remote, "main")
    (seed / "target.txt").write_text("t0\n")
    git(seed, "add", "target.txt")
    git(seed, "commit", "-m", "t0")
    git(seed, "push", remote, f"HEAD:{target}")
    base = git(seed, "rev-parse", "HEAD")
    author.mkdir()
    git(author, "clone", "--no-local", "--branch", target, remote, "member")
    assert runner.invoke(app, ["store", "init", str(author)]).exit_code == 0
    member = author / "member"
    git(member, "checkout", "-b", "feat")
    (member / "payload.txt").write_text("reviewed\n")
    git(member, "add", "payload.txt")
    git(member, "commit", "-m", "reviewed")
    head = git(member, "rev-parse", "HEAD")
    monkeypatch.chdir(author)
    argv = ["review", "bind", "--repo", "member", "--remote", str(remote), "--base", base, "--head", head,
            "--ref", f"refs/heads/{target}", "--source", str(member)]
    with monkeypatch.context() as mp:
        if not record:
            mp.setattr(grip, "_PINNED_TARGET", re.compile(r"(?!)"))
        made = runner.invoke(app, argv)
    assert made.exit_code == 0, made.output
    review = "gr:" + git(author, "for-each-ref", "--format=%(refname)", REVIEW_REF_PREFIX).rsplit("/", 1)[1]
    git(member, "push", remote, "feat")
    publish(author, review, remote)
    shown = grip.show_review_commit(author, review.removeprefix("gr:"))["members"][0]
    assert shown.get("target") == (f"refs/heads/{target}" if record else None), shown
    return dict(remote=remote, author=author, member=member, base=base, head=head, review=review)


def _check(w):
    git(w["member"], "checkout", "-q", "feat")
    assert runner.invoke(app, ["check", "run", str(w["member"]), "--", sys.executable, "-c", "pass"]).exit_code == 0


def _check_and_merge(w):
    _check(w)
    return runner.invoke(app, ["review", "merge", "--approvals", "0", w["review"]])


def _receipt(result):
    return json.loads(result.stdout.strip().splitlines()[-1])


def _expect_moved_target_named(w, target, tip):
    """merge refuses base_moved on the pinned target and prints an update that names it; update then merges it."""
    _check(w)
    before = git(w["remote"], "for-each-ref", "--format=%(refname) %(objectname)")
    merged = runner.invoke(app, ["review", "merge", "--approvals", "0", w["review"]])
    assert merged.exit_code == 3, merged.output
    assert f"into={target} (pinned target)" in merged.stderr, merged.stderr
    assert f"base_moved: remote {target} is {tip}, reviewed base {w['base']}" in merged.stdout, merged.stdout
    assert f"gr2 review update {w['author']} {w['review']} --into {target}" in merged.stdout, merged.stdout
    assert git(w["remote"], "for-each-ref", "--format=%(refname) %(objectname)") == before, "a refusal pushes nothing"
    upd = runner.invoke(app, ["review", "update", str(w["author"]), w["review"]])
    assert upd.exit_code == 0, upd.output
    receipt = _receipt(upd)
    assert receipt["into"] == target and receipt["members"][0]["base"] == tip, receipt
    assert git(w["member"], "log", "-1", "--format=%s") == f"Merge {target} ({tip[:12]}) into feat"
    assert git(w["member"], "rev-parse", "HEAD^2") == tip
    new = grip.show_review_commit(w["author"], receipt["new_review"].removeprefix("gr:"))["members"][0]
    assert new["target"] == f"refs/heads/{target}" and new["base"] == tip, new


def test_dev_moved_while_main_moved_apart_names_dev(tmp_path, monkeypatch):
    """S1: pinned on dev; main gets its own commit, then dev moves. The target is dev, never main."""
    w = _pinned_on(tmp_path, monkeypatch, "dev")
    _commit_on(w["member"], w["remote"], git(w["remote"], "rev-parse", "main"), "m1", "main")
    dev1 = _commit_on(w["member"], w["remote"], w["base"], "d1", "dev")
    _expect_moved_target_named(w, "dev", dev1)


def test_dev_moved_after_a_promotion_names_dev_not_main(tmp_path, monkeypatch):
    """S2, our promotion: main merges dev's tip (the reviewed base is now an ancestor of main), then dev moves.
    Any rule over what sits at the base picks main here and would land dev work on main."""
    w = _pinned_on(tmp_path, monkeypatch, "dev")
    member = w["member"]
    git(member, "checkout", "-q", "-b", "promo", git(w["remote"], "rev-parse", "main"))
    git(member, "merge", "-q", "--no-ff", "-m", "promote dev", w["base"])
    git(member, "push", "-q", str(w["remote"]), "promo:main")
    assert git(member, "merge-base", "--is-ancestor", w["base"], git(w["remote"], "rev-parse", "main")) == ""
    dev1 = _commit_on(member, w["remote"], w["base"], "d1", "dev")
    _expect_moved_target_named(w, "dev", dev1)


def test_a_moved_release_branch_names_the_release_branch(tmp_path, monkeypatch):
    """Pinned on release/1.x; release/1.x moves, main does not. The target is release/1.x, never main."""
    w = _pinned_on(tmp_path, monkeypatch, "release/1.x")
    tip = _commit_on(w["member"], w["remote"], w["base"], "r1", "release/1.x")
    _expect_moved_target_named(w, "release/1.x", tip)


def test_an_unmoved_target_merges_into_it(tmp_path, monkeypatch):
    """The control: pinned on dev, nothing moves; the merge lands on dev and main is untouched."""
    w = _pinned_on(tmp_path, monkeypatch, "dev")
    main = git(w["remote"], "rev-parse", "main")
    merged = _check_and_merge(w)
    assert merged.exit_code == 0, merged.output
    assert "into=dev (pinned target)" in merged.stderr, merged.stderr
    assert git(w["remote"], "rev-list", "--parents", "-n", "1", "dev").split()[1:] == [w["base"], w["head"]]
    assert git(w["remote"], "rev-parse", "main") == main


def test_an_old_pin_whose_target_moved_refuses_into_unresolved_and_update_asks_for_into(tmp_path, monkeypatch):
    """A review pinned before the target was recorded: merge reads the remote as before and refuses rather than
    guess; update has no target to merge and asks for --into instead of merging the remote's default."""
    w = _pinned_on(tmp_path, monkeypatch, "dev", record=False)
    _commit_on(w["member"], w["remote"], git(w["remote"], "rev-parse", "main"), "m1", "main")
    dev1 = _commit_on(w["member"], w["remote"], w["base"], "d1", "dev")
    merged = _check_and_merge(w)
    assert merged.exit_code == 3, merged.output
    assert _receipt(merged)["refused"].startswith("into_unresolved"), merged.output
    head = git(w["member"], "rev-parse", "HEAD")
    upd = runner.invoke(app, ["review", "update", str(w["author"]), w["review"]])
    assert upd.exit_code == 2, upd.output
    assert _receipt(upd)["refused"] == ("into_unresolved: member: this review was pinned before its target was "
                                        "recorded; pass --into <the branch it was pinned against>"), upd.output
    assert git(w["member"], "rev-parse", "HEAD") == head, "nothing may be merged"
    named = runner.invoke(app, ["review", "update", str(w["author"]), w["review"], "--into", "dev"])
    assert named.exit_code == 0, named.output
    assert git(w["member"], "rev-parse", "HEAD^2") == dev1


def test_members_pinned_against_different_targets_refuse_into_unresolved():
    from gr2.python_cli import defaults
    try:
        defaults.pinned_target([{"key": "a", "target": "refs/heads/dev"}, {"key": "b", "target": "refs/heads/main"}])
    except defaults.Unresolved as exc:
        assert str(exc) == ("into_unresolved: the members were pinned against different targets "
                            "(a=refs/heads/dev, b=refs/heads/main); pass --into <branch>")
    else:
        raise AssertionError("two targets resolved one --into")
    assert defaults.pinned_target([{"key": "a"}, {"key": "b"}]) is None
    assert defaults.pinned_target([{"key": "a", "target": "refs/heads/dev"},
                                   {"key": "b", "target": "refs/heads/dev"}]) == ("dev", "pinned target")


def test_a_review_pinned_with_no_rows_records_its_target(tmp_path, monkeypatch):
    """`gr2 review pin <ws>`, the form the skill teaches: the member's branch is the workspace's pin, and the target
    recorded is the one the base check matched."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    remote, seed, ws = tmp_path / "app.git", tmp_path / "seed", tmp_path / "ws"
    git(tmp_path, "init", "--bare", "-b", "main", remote)
    seed.mkdir()
    git(seed, "init", "-b", "main")
    git(seed, "commit", "--allow-empty", "-m", "base")
    git(seed, "push", remote, "main")
    ws.mkdir()
    git(ws, "clone", "-q", str(remote), "app")
    assert runner.invoke(app, ["store", "init", str(ws)]).exit_code == 0
    git(ws / "app", "checkout", "-q", "-b", "feat")
    (ws / "app" / "f.txt").write_text("change\n")
    git(ws / "app", "add", "f.txt")
    git(ws / "app", "commit", "-q", "-m", "change")
    monkeypatch.chdir(ws)
    pinned = runner.invoke(app, ["review", "pin", str(ws)])
    assert pinned.exit_code == 0, pinned.output
    rid = re.search(r"gr:([0-9a-f]{40})", pinned.output).group(1)
    member = grip.show_review_commit(ws, rid)["members"][0]
    assert member["target"] == "refs/heads/main" and member["base"] == git(remote, "rev-parse", "main"), member
