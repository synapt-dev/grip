"""gr2 knows what the repo knows: check run/show and review merge need no flag the repo already answers.

Each default is witnessed through the CLI, beside its override and the one refusal it keeps for a repo that
genuinely does not say.
"""
from __future__ import annotations

import json
import sys

from typer.testing import CliRunner

from gr2.python_cli.app import app
from tests.test_review_merge_gate import _git_identity, check, git, slice2, world  # noqa: F401  (fixtures)

runner = CliRunner()


def out(result):
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_check_run_and_show_default_to_origin_and_head(world):
    member = world["member"]
    made = runner.invoke(app, ["check", "run", str(member), "--", sys.executable, "-c", "pass"])
    assert made.exit_code == 0, made.output
    assert f"head={world['head']} (HEAD)" in made.stderr and f"remote={world['remote']} (origin)" in made.stderr
    shown = runner.invoke(app, ["check", "show", str(member)])
    assert shown.exit_code == 0, shown.output
    assert out(shown)["status"] == "pass", shown.output


def test_a_given_remote_name_resolves_to_its_url(world):
    made = runner.invoke(app, ["check", "run", str(world["member"]), "--remote", "origin", "--",
                               sys.executable, "-c", "pass"])
    assert made.exit_code == 0, made.output
    assert f"remote={world['remote']} (given remote name)" in made.stderr


def test_no_upstream_and_no_origin_refuses_and_names_the_flag(world):
    git(world["member"], "remote", "rename", "origin", "elsewhere")
    made = runner.invoke(app, ["check", "run", str(world["member"]), "--", sys.executable, "-c", "pass"])
    assert made.exit_code == 2, made.output
    assert out(made)["reason"].startswith("remote_unresolved") and "--remote" in out(made)["reason"]


def test_review_merge_with_no_arguments_merges_the_bind_at_the_current_heads(world):
    assert runner.invoke(app, ["check", "run", str(world["member"]), "--", sys.executable, "-c", "pass"]).exit_code == 0
    merged = runner.invoke(app, ["review", "merge"])
    assert merged.exit_code == 0, merged.output
    assert f"review={world['review']} (bind at the current heads)" in merged.stderr
    assert "from=feat (current branch)" in merged.stderr and "into=main (default branch at the reviewed base)" in merged.stderr
    tip = git(world["remote"], "rev-parse", "main")
    assert git(world["remote"], "rev-list", "--parents", "-n", "1", tip).split()[1:] == [world["base"], world["head"]]


def test_review_merge_overrides_still_win(world):
    assert runner.invoke(app, ["check", "run", str(world["member"]), "--", sys.executable, "-c", "pass"]).exit_code == 0
    merged = runner.invoke(app, ["review", "merge", world["review"], "--from", "feat", "--into", "main"])
    assert merged.exit_code == 0, merged.output
    assert "(given)" in merged.stderr


def test_a_member_moved_locally_matches_no_bind_and_refuses(world):
    (world["member"] / "later.txt").write_text("later\n")
    git(world["member"], "add", "later.txt")
    git(world["member"], "commit", "-m", "later")
    merged = runner.invoke(app, ["review", "merge"])
    assert merged.exit_code == 3, merged.output
    receipt = out(merged)
    assert receipt["refused"].startswith("review_ambiguous: no review bind")
    assert set(receipt) == {"id", "into", "feature", "exit", "members", "refused"} and receipt["members"] == []
    assert git(world["remote"], "rev-parse", "main") == world["base"]


def test_members_on_different_branches_refuse_the_feature_default(slice2):
    git(slice2["members"]["beta"]["repo"], "checkout", "-q", "-b", "other")
    merged = runner.invoke(app, ["review", "merge", slice2["review"]])
    assert merged.exit_code == 3, merged.output
    assert out(merged)["refused"].startswith("feature_unresolved") and "--from" in out(merged)["refused"]


def test_two_binds_at_the_current_heads_refuse_and_list_both(world, monkeypatch):
    # Two binds of the same content in the same second are ONE commit, so the second is dated apart.
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2001-01-01T00:00:00Z")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2001-01-01T00:00:00Z")
    again = runner.invoke(app, ["review", "bind", "--repo", "member", "--remote", str(world["remote"]),
                                "--base", world["base"], "--head", world["head"], "--ref", "refs/heads/main",
                                "--source", str(world["member"]), "--ratified", "second-look"])
    assert again.exit_code == 0, again.output
    merged = runner.invoke(app, ["review", "merge"])
    assert merged.exit_code == 3, merged.output
    reason = out(merged)["refused"]
    assert reason.startswith("review_ambiguous: several binds") and reason.count("gr:") == 2, reason
    assert git(world["remote"], "rev-parse", "main") == world["base"]


def test_two_branches_at_the_reviewed_base_refuse_even_when_the_remote_head_names_one(world):
    """With dev cut at the base and the remote's HEAD moved to dev, no default is guessed."""
    git(world["remote"], "branch", "dev", world["base"])
    git(world["remote"], "symbolic-ref", "HEAD", "refs/heads/dev")
    assert runner.invoke(app, ["check", "run", str(world["member"]), "--", sys.executable, "-c", "pass"]).exit_code == 0
    merged = runner.invoke(app, ["review", "merge"])
    assert merged.exit_code == 3, merged.output
    assert out(merged)["refused"].startswith("into_unresolved: no remote's default branch") and "--into" in out(merged)["refused"]
    assert git(world["remote"], "rev-parse", "dev") == world["base"] == git(world["remote"], "rev-parse", "main")
    named = runner.invoke(app, ["review", "merge", "--into", "main"])
    assert named.exit_code == 0, named.output
    assert git(world["remote"], "rev-parse", "dev") == world["base"]


def test_an_upstream_whose_names_hold_a_slash_resolves_its_remote(world):
    """A remote named team/fork, and a branch tracking a local branch with a slash, both resolve."""
    member = world["member"]
    git(member, "remote", "add", "team/fork", str(world["remote"]))
    git(member, "config", f"branch.{git(member, 'symbolic-ref', '--short', 'HEAD')}.remote", "team/fork")
    made = runner.invoke(app, ["check", "run", str(member), "--", sys.executable, "-c", "pass"])
    assert made.exit_code == 0, made.output
    assert f"remote={world['remote']} (upstream)" in made.stderr
    git(member, "branch", "topic/y")
    git(member, "checkout", "-q", "-b", "child", "--track", "topic/y")
    local = runner.invoke(app, ["check", "run", str(member), "--", sys.executable, "-c", "pass"])
    assert local.exit_code == 0, local.output
    assert f"remote={world['remote']} (origin)" in local.stderr


def test_a_member_dir_that_is_not_its_own_repo_never_borrows_the_enclosing_branch(slice2, tmp_path):
    """A member path inside the workspace repo that is not a repository never borrows its branch."""
    from gr2.python_cli import defaults
    outer = tmp_path / "outer"
    (outer / "member").mkdir(parents=True)
    git(outer, "init", "-q", "-b", "main")
    assert defaults.own_repo(outer) and not defaults.own_repo(outer / "member")
    assert defaults.current_head(outer / "member") is None
    try:
        defaults.branch([outer / "member"], None)
    except defaults.Unresolved as exc:
        assert "member=not a repository" in str(exc)
    else:
        raise AssertionError("a non-repository member resolved a branch")


def test_a_stale_branch_left_at_the_base_is_never_the_default_target(world):
    """main moves after review; a hold branch still sits at the reviewed base. Nothing is pushed anywhere."""
    member = world["member"]
    git(member, "checkout", "-q", "-b", "later", world["base"])
    (member / "later.txt").write_text("later\n")
    git(member, "add", "later.txt")
    git(member, "commit", "-q", "-m", "main moves on")
    git(member, "push", "-q", str(world["remote"]), "later:main")
    git(world["remote"], "branch", "hold/stale", world["base"])
    git(member, "checkout", "-q", "feat")
    assert runner.invoke(app, ["check", "run", str(member), "--", sys.executable, "-c", "pass"]).exit_code == 0
    before = git(world["remote"], "for-each-ref", "--format=%(refname) %(objectname)")
    merged = runner.invoke(app, ["review", "merge"])
    assert merged.exit_code == 3, merged.output
    assert out(merged)["refused"].startswith("into_unresolved") and "--into" in out(merged)["refused"]
    after = git(world["remote"], "for-each-ref", "--format=%(refname) %(objectname)")
    assert after == before, "a refused default must push nothing"
    named = runner.invoke(app, ["review", "merge", "--into", "hold/stale"])
    assert named.exit_code == 0, named.output


def test_a_credentialed_remote_url_is_never_printed(world):
    member = world["member"]
    git(member, "remote", "set-url", "origin", "https://user:SECRETTOKEN@127.0.0.1:1/x.git")
    for verb in (["check", "run", str(member), "--", sys.executable, "-c", "pass"], ["check", "show", str(member)]):
        result = runner.invoke(app, verb)
        assert "SECRETTOKEN" not in result.stderr and "SECRETTOKEN" not in result.stdout, result.output
        assert "https://***@127.0.0.1:1/x.git" in result.stderr


def test_a_missing_upstream_remote_is_named(world):
    member = world["member"]
    git(member, "config", f"branch.{git(member, 'symbolic-ref', '--short', 'HEAD')}.remote", "gone")
    made = runner.invoke(app, ["check", "run", str(member), "--", sys.executable, "-c", "pass"])
    assert made.exit_code == 2 and "'gone' is not configured" in out(made)["reason"], made.output


def _moved_local_head_inside_an_opened_review(world, tmp_path, monkeypatch):
    check(world)
    (world["member"] / "local-new.txt").write_text("not the reviewed head\n")
    git(world["member"], "add", "local-new.txt")
    git(world["member"], "commit", "-m", "local head after bind")
    lane = tmp_path / "opened-review"
    (lane / "member").mkdir(parents=True)
    (lane / ".grip-review-open.json").write_text(json.dumps({
        "kind": "review-open", "workspace_root": str(world["author"]),
        "gr_commit": world["review"].removeprefix("gr:")}))
    monkeypatch.chdir(lane / "member")


def test_an_opened_review_marker_supplies_the_workspace_never_the_review(world, tmp_path, monkeypatch):
    """Inside an opened review, a bare merge still selects by the members' current heads, not the marker's bind."""
    _moved_local_head_inside_an_opened_review(world, tmp_path, monkeypatch)
    merged = runner.invoke(app, ["review", "merge"])
    assert merged.exit_code == 3, merged.output
    assert out(merged)["refused"].startswith("review_ambiguous: no review bind")
    assert git(world["remote"], "rev-parse", "main") == world["base"]


def test_an_explicit_old_review_still_merges_from_inside_a_marker(world, tmp_path, monkeypatch):
    _moved_local_head_inside_an_opened_review(world, tmp_path, monkeypatch)
    merged = runner.invoke(app, ["review", "merge", world["review"], "--from", "feat", "--into", "main"])
    assert merged.exit_code == 0, merged.output
    assert git(world["remote"], "rev-list", "--parents", "-n", "1", "main").split()[1:] == [world["base"], world["head"]]


def test_an_explicit_head_overrides_HEAD(world):
    member = world["member"]
    made = runner.invoke(app, ["check", "run", str(member), "--head", world["base"], "--", sys.executable, "-c", "pass"])
    assert made.exit_code == 0, made.output
    assert f"head={world['base']} (given)" in made.stderr
