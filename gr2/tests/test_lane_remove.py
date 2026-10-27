"""A temporary lane must be able to end, and ending it must never destroy unlanded work.

Measured on dev 4ada547e: gr2 has `lane create`, `enter`, `exit`, `show`, `resolve`, `bind` and `lease`, and nothing
removes a lane. `lane exit` only leaves it, so every lane's clones stay on disk for good.
`lane remove` ends a lane, and refuses while any of its repos holds work that
exists nowhere else. First row: a local commit that was never pushed.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gr2.prototypes import lane_workspace_prototype as lane_proto
from gr2.python_cli import app as gr2_app
from tests.conftest import make_cli_runner
from tests.test_lane_enter_hook_warnings import _workspace_with_enter_hook

runner = make_cli_runner()


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", *a], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def _exited_lane_with_a_commit(tmp_path: Path) -> tuple[Path, Path, str]:
    """A one-repo lane, entered, one commit made in it, then exited (a clean tree). Returns (ws, lane repo, commit)."""
    ws, _, _ = _workspace_with_enter_hook(tmp_path, command="exit 0")
    for argv in (["lane", "create", str(ws), "atlas", "x", "--repos", "app", "--branch", "app=feat/x"],
                 ["lane", "enter", str(ws), "atlas", "x", "--actor", "agent:s"]):
        result = runner.invoke(gr2_app.app, argv)
        assert result.exit_code == 0, result.output
    repo = gr2_app._lane_repo_root(ws, "atlas", "x", "app")
    _git(repo, "config", "user.email", "t@e.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("work that exists only here\n")
    _git(repo, "commit", "-q", "-am", "lane work")
    commit = _git(repo, "rev-parse", "HEAD")
    result = runner.invoke(gr2_app.app, ["lane", "exit", str(ws), "atlas", "--actor", "agent:s"])
    assert result.exit_code == 0, result.output
    assert _git(repo, "status", "--porcelain") == ""
    return ws, repo, commit


def _remove(ws: Path):
    return runner.invoke(gr2_app.app, ["lane", "remove", str(ws), "atlas", "x"])


def test_a_lane_holding_an_unpushed_commit_is_not_removed(tmp_path: Path) -> None:
    ws, repo, commit = _exited_lane_with_a_commit(tmp_path)
    assert _git(repo, "log", "HEAD", "--not", "--remotes", "--format=%H") == commit, "the fixture's commit is unpushed"

    result = _remove(ws)

    assert result.exit_code != 0, result.output
    assert "app" in result.output and commit[:12] in result.output, result.output
    assert repo.is_dir() and _git(repo, "rev-parse", "HEAD") == commit, "the lane and its commit are untouched"
    assert lane_proto.lane_file(ws, "atlas", "x").is_file(), "the lane record is untouched"


def test_the_same_lane_once_its_commit_is_pushed_is_removed(tmp_path: Path) -> None:
    ws, repo, commit = _exited_lane_with_a_commit(tmp_path)
    _git(repo, "push", "-q", "origin", "HEAD:refs/heads/feat/x")
    assert _git(repo, "log", "HEAD", "--not", "--remotes", "--format=%H") == "", "control: nothing unpushed"
    member = ws / "repos" / "app"
    member_head = _git(member, "rev-parse", "HEAD")

    result = _remove(ws)

    assert result.exit_code == 0, result.output
    assert not repo.exists(), "the lane's clone is gone"
    assert not lane_proto.lane_file(ws, "atlas", "x").exists(), "the lane record is gone"
    assert _git(member, "rev-parse", "HEAD") == member_head, "the workspace's own clone is untouched"


def _refused_untouched(ws: Path, repo: Path, result, *needles: str) -> None:
    assert result.exit_code != 0, result.output
    for needle in needles:
        assert needle in result.output, (needle, result.output)
    assert repo.is_dir(), "the lane's clone is untouched"
    assert lane_proto.lane_file(ws, "atlas", "x").is_file(), "the lane record is untouched"


def _pushed_exited_lane(tmp_path: Path) -> tuple[Path, Path]:
    """The removable control state: exited, clean, every commit pushed. Each row below adds ONE thing to it."""
    ws, repo, _ = _exited_lane_with_a_commit(tmp_path)
    _git(repo, "push", "-q", "origin", "HEAD:refs/heads/feat/x")
    return ws, repo


def test_the_entered_lane_is_not_removed(tmp_path: Path) -> None:
    ws, repo = _pushed_exited_lane(tmp_path)
    result = runner.invoke(gr2_app.app, ["lane", "enter", str(ws), "atlas", "x", "--actor", "agent:s"])
    assert result.exit_code == 0, result.output
    _refused_untouched(ws, repo, _remove(ws), "lane_is_entered")


def test_a_lane_with_uncommitted_work_is_not_removed(tmp_path: Path) -> None:
    ws, repo = _pushed_exited_lane(tmp_path)
    (repo / "untracked.txt").write_text("not committed anywhere\n")
    _refused_untouched(ws, repo, _remove(ws), "lane_has_uncommitted_work", "app")
    assert (repo / "untracked.txt").is_file()


def test_an_unpushed_commit_on_a_branch_other_than_head_is_not_removed(tmp_path: Path) -> None:
    ws, repo = _pushed_exited_lane(tmp_path)
    _git(repo, "branch", "side")
    _git(repo, "checkout", "-q", "side")
    (repo / "g.txt").write_text("side work\n")
    _git(repo, "add", "g.txt")
    _git(repo, "commit", "-q", "-m", "side work")
    side = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "feat/x")
    assert _git(repo, "log", "HEAD", "--not", "--remotes", "--format=%H") == "", "HEAD alone looks pushed"
    _refused_untouched(ws, repo, _remove(ws), "lane_has_unpushed_commit", side[:12])


def test_a_lane_holding_a_stash_is_not_removed(tmp_path: Path) -> None:
    ws, repo = _pushed_exited_lane(tmp_path)
    (repo / "f.txt").write_text("stashed edit\n")
    _git(repo, "stash", "push", "-q", "-m", "keep me")
    stash = _git(repo, "rev-parse", "stash@{0}")
    assert _git(repo, "status", "--porcelain") == ""
    _refused_untouched(ws, repo, _remove(ws), "lane_has_stash", stash[:12])


def test_a_lane_with_a_live_lease_is_not_removed(tmp_path: Path) -> None:
    ws, repo = _pushed_exited_lane(tmp_path)
    result = runner.invoke(gr2_app.app, ["lane", "lease", "acquire", str(ws), "atlas", "x",
                                         "--actor", "agent:other", "--mode", "edit"])
    assert result.exit_code == 0, result.output
    _refused_untouched(ws, repo, _remove(ws), "lane_has_live_lease", "agent:other")


def _bound_lane(tmp_path: Path) -> tuple[Path, Path]:
    """A lane bound to a worktree the lane did not create. Returns (ws, the bound worktree)."""
    ws, _, _ = _workspace_with_enter_hook(tmp_path, command="exit 0")
    wt = ws / "wt"
    wt.mkdir()
    _git(wt, "init", "-q")
    _git(wt, "config", "user.email", "t@e.invalid")
    _git(wt, "config", "user.name", "t")
    (wt / "f.txt").write_text("someone else's checkout\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-q", "-m", "init")
    _git(wt, "checkout", "-q", "-b", "feat/x")
    result = runner.invoke(gr2_app.app, ["lane", "create", str(ws), "atlas", "x", "--repos", "app",
                                         "--branch", "app=feat/x", "--bind", str(wt)])
    assert result.exit_code == 0, result.output
    return ws, wt


def _tree_state(wt: Path) -> tuple:
    files = {p.relative_to(wt).as_posix(): p.read_bytes() for p in sorted(wt.rglob("*")) if p.is_file()}
    return _git(wt, "rev-parse", "HEAD"), _git(wt, "symbolic-ref", "HEAD"), _git(wt, "status", "--porcelain"), files


def test_a_bound_lane_is_removed_as_a_record_and_its_worktree_is_untouched(tmp_path: Path) -> None:
    ws, wt = _bound_lane(tmp_path)
    # The worktree holds work that is in no remote: it is the worktree's owner's, not the lane's, so it neither
    # blocks the record's removal nor goes with it.
    (wt / "f.txt").write_text("an uncommitted edit\n")
    (wt / "new.txt").write_text("an untracked file\n")
    before = _tree_state(wt)
    assert "atlas\tx\t" in runner.invoke(gr2_app.app, ["lane", "list", str(ws)]).output, "control: the lane is listed"

    result = _remove(ws)

    assert result.exit_code == 0, result.output
    assert not lane_proto.lane_dir(ws, "atlas", "x").exists(), "the lane record is gone"
    assert _tree_state(wt) == before, "the bound worktree's files, HEAD, branch and status are unchanged"
    assert "record only" in result.output and str(wt.resolve()) in result.output, result.output
    listed = runner.invoke(gr2_app.app, ["lane", "list", str(ws)])
    assert listed.exit_code == 0 and "atlas\tx\t" not in listed.output, listed.output


def test_a_leased_bound_lane_is_not_removed(tmp_path: Path) -> None:
    ws, wt = _bound_lane(tmp_path)
    result = runner.invoke(gr2_app.app, ["lane", "lease", "acquire", str(ws), "atlas", "x",
                                         "--actor", "agent:other", "--mode", "edit"])
    assert result.exit_code == 0, result.output
    before = _tree_state(wt)
    _refused_untouched(ws, wt, _remove(ws), "lane_has_live_lease", "agent:other")
    assert _tree_state(wt) == before


def test_a_bound_worktree_inside_the_lane_record_is_not_removed(tmp_path: Path) -> None:
    ws, _ = _bound_lane(tmp_path)
    # A hand-edited record that points the lane at a worktree inside its own record directory: removing the
    # record would remove the worktree with it.
    inner = lane_proto.lane_dir(ws, "atlas", "x") / "inner"
    inner.mkdir()
    _git(inner, "init", "-q")
    _git(inner, "-c", "user.email=t@e.invalid", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    toml = lane_proto.lane_file(ws, "atlas", "x")
    old = lane_proto.load_lane_doc(ws, "atlas", "x")["bound_worktree"]
    toml.write_text(toml.read_text().replace(old, str(inner.resolve())))
    before = _tree_state(inner)
    _refused_untouched(ws, inner, _remove(ws), "lane_record_holds_bound_worktree")
    assert _tree_state(inner) == before


def _inner_worktree(ws: Path, owner_dir: Path) -> Path:
    inner = owner_dir / "x" / "inner"
    inner.mkdir()
    _git(inner, "init", "-q")
    _git(inner, "-c", "user.email=t@e.invalid", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    (inner / "untracked.txt").write_text("work no commit holds\n")
    return inner


def _point_record_at(ws: Path, value: str) -> None:
    toml = lane_proto.lane_file(ws, "atlas", "x")
    old = lane_proto.load_lane_doc(ws, "atlas", "x")["bound_worktree"]
    toml.write_text(toml.read_text().replace(json.dumps(old), json.dumps(value)))
    assert lane_proto.load_lane_doc(ws, "atlas", "x")["bound_worktree"] == value


@pytest.mark.parametrize("cwd", ["workspace", "elsewhere"])
def test_a_relative_bound_worktree_is_refused_from_any_cwd(tmp_path: Path, monkeypatch, cwd: str) -> None:
    ws, _ = _bound_lane(tmp_path)
    inner = _inner_worktree(ws, lane_proto.lane_state_root(ws) / "atlas")
    # A hand-edited record: relative to the workspace it names a worktree inside the record, but a relative
    # path resolves against the CWD, so from elsewhere it would name nothing the record holds.
    value = ".grip/state/lanes/atlas/x/inner"
    _point_record_at(ws, value)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(ws if cwd == "workspace" else elsewhere)
    before = _tree_state(inner)

    result = _remove(ws)

    _refused_untouched(ws, inner, result, "lane_record_invalid: atlas/x:",
                       f"bound lane atlas/x needs an absolute bound_worktree, not {value!r}")
    assert _tree_state(inner) == before, "the worktree inside the record, untracked file included, is unchanged"
    listed = runner.invoke(gr2_app.app, ["lane", "list", str(ws)])
    assert f"atlas\tx\tkeep\tlane_record_invalid: atlas/x: bound lane atlas/x needs an absolute" in listed.output, \
        listed.output


def test_a_bound_worktree_inside_the_record_is_refused_through_a_symlinked_owner_dir(tmp_path: Path) -> None:
    ws, _ = _bound_lane(tmp_path)
    # The record's own path passes through a symlink, so the worktree's resolved path is only inside the record
    # once the record's path is resolved too.
    owners = lane_proto.lane_state_root(ws)
    (owners / "atlas").rename(owners / "atlas-real")
    (owners / "atlas").symlink_to("atlas-real", target_is_directory=True)
    inner = _inner_worktree(ws, owners / "atlas-real")
    _point_record_at(ws, str(inner.resolve()))
    before = _tree_state(inner)
    _refused_untouched(ws, inner, _remove(ws), "lane_record_holds_bound_worktree")
    assert _tree_state(inner) == before


@pytest.mark.parametrize("shape", ["dotdot", "symlink"])
def test_an_absolute_bound_worktree_that_lands_inside_the_record_is_refused(tmp_path: Path, shape: str) -> None:
    ws, wt = _bound_lane(tmp_path)
    inner = _inner_worktree(ws, lane_proto.lane_state_root(ws) / "atlas")
    # Absolute, but only its resolved path shows it is inside the record: `..` segments back into the record,
    # or a symlinked directory outside the record that points into it.
    if shape == "dotdot":
        value = str(wt.resolve() / ".." / ".grip" / "state" / "lanes" / "atlas" / "x" / "inner")
    else:
        (tmp_path / "link").symlink_to(lane_proto.lane_state_root(ws) / "atlas", target_is_directory=True)
        value = str(tmp_path.resolve() / "link" / "x" / "inner")
    _point_record_at(ws, value)
    assert Path(value).resolve() == inner.resolve(), "control: the value names the worktree inside the record"
    before = _tree_state(inner)
    _refused_untouched(ws, inner, _remove(ws), "lane_record_holds_bound_worktree")
    assert _tree_state(inner) == before


def _receipts_under(repo: Path) -> list[Path]:
    return sorted(p for p in (repo / ".git").rglob("grip-review*"))


def _bind(ws: Path, base: str):
    return runner.invoke(gr2_app.app, ["lane", "bind", str(ws), "atlas", "x", "--base", base, "--allow-local"])


@pytest.mark.parametrize("cwd", ["workspace", "elsewhere"])
def test_lane_bind_refuses_a_relative_bound_worktree_from_any_cwd(tmp_path: Path, monkeypatch, cwd: str) -> None:
    ws, wt = _bound_lane(tmp_path)
    base = _git(wt, "rev-parse", "HEAD")
    # From the workspace a relative "wt" names the real worktree; from elsewhere it names a decoy clone that
    # sits at elsewhere/wt with the same head. Neither may receive the receipt.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _git(elsewhere, "clone", "-q", str(wt), "wt")
    decoy = elsewhere / "wt"
    _git(decoy, "checkout", "-q", "feat/x")
    _point_record_at(ws, "wt")
    monkeypatch.chdir(ws if cwd == "workspace" else elsewhere)

    result = _bind(ws, base)

    assert result.exit_code == 2, result.output
    assert "bound lane atlas/x needs an absolute bound_worktree, not 'wt'" in result.output, result.output
    assert _receipts_under(wt) == [], "the real worktree holds no receipt"
    assert _receipts_under(decoy) == [], "the decoy holds no receipt"
    _point_record_at(ws, str(wt.resolve()))
    control = _bind(ws, base)
    assert control.exit_code == 0 and _receipts_under(wt), "control: the absolute record binds from the same CWD"


def test_pr_create_refuses_a_relative_bound_worktree_before_any_push(tmp_path: Path, monkeypatch) -> None:
    ws, wt = _bound_lane(tmp_path)
    base = _git(wt, "rev-parse", "HEAD")
    (wt / "f.txt").write_text("the reviewed change\n")
    _git(wt, "commit", "-q", "-am", "change")
    toml = lane_proto.lane_file(ws, "atlas", "x")
    doc = lane_proto.load_lane_doc(ws, "atlas", "x")
    toml.write_text(lane_proto.serialize_toml({**doc, "bound_head": _git(wt, "rev-parse", "HEAD")}))
    assert _bind(ws, base).exit_code == 0
    pushes: list = []
    monkeypatch.setattr(lane_proto._push, "push_current_branch", lambda repo, **kw: pushes.append(repo))
    monkeypatch.chdir(ws)
    lane_proto.pr_create_bound_lane(ws, "atlas", "x")
    assert pushes == [wt.resolve()], "control: the absolute record reaches the push seam"
    # From the workspace a relative "wt" names that same bound worktree and its valid receipt.
    _point_record_at(ws, "wt")

    with pytest.raises(SystemExit, match="needs an absolute bound_worktree, not 'wt'"):
        lane_proto.pr_create_bound_lane(ws, "atlas", "x")
    assert pushes == [wt.resolve()], "the relative record pushed nothing"


def test_an_entered_bound_lane_is_not_removed(tmp_path: Path) -> None:
    ws, wt = _bound_lane(tmp_path)
    (wt / "new.txt").write_text("an untracked file\n")
    result = runner.invoke(gr2_app.app, ["lane", "enter", str(ws), "atlas", "x", "--actor", "agent:s"])
    assert result.exit_code == 0, result.output
    before = _tree_state(wt)
    _refused_untouched(ws, wt, _remove(ws), "lane_is_entered")
    assert _tree_state(wt) == before


def test_a_lane_whose_origin_cannot_be_fetched_is_not_removed(tmp_path: Path) -> None:
    ws, repo = _pushed_exited_lane(tmp_path)
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "no-such-origin.git"))
    # The remote-tracking refs still hold every commit, so a reader that skipped the fetch would say "all pushed".
    assert _git(repo, "log", "HEAD", "--branches", "--not", "--remotes", "--format=%H") == ""
    _refused_untouched(ws, repo, _remove(ws), "lane_unpushed_unknown", "app")


def test_lane_list_names_each_lane_removable_or_why_not(tmp_path: Path) -> None:
    """A stale lane is a STATE (exited, clean, nothing unpushed), not an age; list reads the same checks remove does."""
    ws, _, commit = _exited_lane_with_a_commit(tmp_path)
    result = runner.invoke(gr2_app.app, ["lane", "create", str(ws), "atlas", "y", "--repos", "app", "--branch", "app=feat/y"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(gr2_app.app, ["lane", "list", str(ws)])

    assert result.exit_code == 0, result.output
    rows = {line.split("\t")[1]: line for line in result.output.splitlines() if line.startswith("atlas\t")}
    assert set(rows) == {"x", "y"}, result.output
    assert "\tremovable" in rows["y"], rows["y"]
    assert "\tkeep" in rows["x"] and "lane_has_unpushed_commit" in rows["x"] and commit[:12] in rows["x"], rows["x"]
    assert "not fetched" in result.output, "the list says it read remote refs without a fetch"


def test_a_lane_whose_directory_escapes_the_workspace_by_symlink_is_not_removed(tmp_path: Path) -> None:
    ws, repo = _pushed_exited_lane(tmp_path)
    lane = lane_proto.lane_dir(ws, "atlas", "x")
    outside = tmp_path / "outside-the-workspace"
    lane.rename(outside)
    lane.symlink_to(outside, target_is_directory=True)
    assert lane.is_symlink() and (outside / "lane.toml").is_file()

    result = _remove(ws)

    assert result.exit_code != 0, result.output
    assert "lane_path_escapes_workspace" in result.output, result.output
    assert (outside / "lane.toml").is_file(), "nothing outside was touched"
    assert (repo / ".git").exists(), "the lane's clone is untouched"
    assert lane.is_symlink(), "the link itself is left for the operator"


def test_a_lane_whose_checkout_escapes_the_workspace_by_symlink_is_not_removed(tmp_path: Path) -> None:
    ws, repo = _pushed_exited_lane(tmp_path)
    checkout = lane_proto.lane_checkout_root(ws, "atlas", "x")
    assert checkout.resolve() != lane_proto.lane_dir(ws, "atlas", "x").resolve(), "the clone and the record are apart"
    outside = tmp_path / "outside-checkout"
    checkout.rename(outside)
    checkout.symlink_to(outside, target_is_directory=True)

    result = _remove(ws)

    assert result.exit_code != 0, result.output
    assert "lane_path_escapes_workspace" in result.output, result.output
    assert (outside / "repos" / "app" / ".git").exists(), "nothing outside was touched"
    assert lane_proto.lane_file(ws, "atlas", "x").is_file(), "the lane record is untouched"


def test_a_git_repo_under_the_checkout_root_that_is_not_a_listed_repo_is_not_removed(tmp_path: Path) -> None:
    """The removal reaches the whole checkout root, so the check must too: an extra repo there can hold the only copy."""
    ws, repo = _pushed_exited_lane(tmp_path)
    extra = lane_proto.lane_checkout_root(ws, "atlas", "x") / "scratch"
    extra.mkdir()
    _git(extra, "init", "-q")
    _git(extra, "config", "user.email", "t@e.invalid")
    _git(extra, "config", "user.name", "t")
    (extra / "only-copy.txt").write_text("exists nowhere else\n")
    _git(extra, "add", ".")
    _git(extra, "commit", "-q", "-m", "only copy")
    only = _git(extra, "rev-parse", "HEAD")

    _refused_untouched(ws, repo, _remove(ws), "lane_has_unlisted_content", "scratch")
    assert _git(extra, "rev-parse", "HEAD") == only


def test_a_file_beside_the_listed_repos_is_named_and_not_removed(tmp_path: Path) -> None:
    ws, repo = _pushed_exited_lane(tmp_path)
    stray = lane_proto.lane_checkout_root(ws, "atlas", "x") / "repos" / "notes.md"
    stray.write_text("written in the lane, outside any repo\n")
    _refused_untouched(ws, repo, _remove(ws), "lane_has_unlisted_content", "notes.md")
    assert stray.is_file()


def test_a_commit_reachable_only_from_a_local_tag_is_not_removed(tmp_path: Path) -> None:
    ws, repo = _pushed_exited_lane(tmp_path)
    _git(repo, "checkout", "-q", "--detach")
    (repo / "t.txt").write_text("tagged only\n")
    _git(repo, "add", "t.txt")
    _git(repo, "commit", "-q", "-m", "tagged only")
    tagged = _git(repo, "rev-parse", "HEAD")
    _git(repo, "tag", "keep-me")
    _git(repo, "checkout", "-q", "feat/x")
    assert _git(repo, "log", "HEAD", "--branches", "--not", "--remotes", "--format=%H") == "", "branches alone look pushed"
    _refused_untouched(ws, repo, _remove(ws), "lane_has_unpushed_commit", tagged[:12])


def _lane_with_a_local_tag_commit(tmp_path: Path) -> tuple[Path, Path, str]:
    """A commit off every branch that only a local tag (0.5.0, never pushed) reaches; the clone back on feat/x."""
    ws, repo = _pushed_exited_lane(tmp_path)
    _git(repo, "checkout", "-q", "--detach")
    (repo / "t.txt").write_text("a stray release commit\n")
    _git(repo, "add", "t.txt")
    _git(repo, "commit", "-q", "-m", "0.5.0")
    tagged = _git(repo, "rev-parse", "HEAD")
    _git(repo, "tag", "0.5.0")
    _git(repo, "checkout", "-q", "feat/x")
    return ws, repo, tagged


def test_the_refusal_names_the_local_tag_that_reaches_the_commit(tmp_path: Path) -> None:
    """A sha alone cannot tell a stray local tag from unpushed work; the row names the ref the commit was found on."""
    ws, repo, tagged = _lane_with_a_local_tag_commit(tmp_path)
    _refused_untouched(ws, repo, _remove(ws), f"lane_has_unpushed_commit: app {tagged} (on refs/tags/0.5.0)")


def test_the_refusal_names_the_branch_when_a_branch_and_a_tag_both_reach_the_commit(tmp_path: Path) -> None:
    """The control: once a local branch reaches the tagged commit it is a desk's work, and the branch is named, not the tag."""
    ws, repo, tagged = _lane_with_a_local_tag_commit(tmp_path)
    _git(repo, "branch", "mine", tagged)
    result = _remove(ws)
    _refused_untouched(ws, repo, result, f"lane_has_unpushed_commit: app {tagged} (on refs/heads/mine)")
    assert "refs/tags/0.5.0" not in result.output, result.output


def _lane_with_a_commit_only_a_pushed_tag_holds(tmp_path: Path) -> tuple[Path, Path, str]:
    """A release tag off every branch, pushed (an annotated tag is this shape), and it kept every lane: refs/remotes/*
    holds no tags, so `--tags --not --remotes` read the commit as unpushed."""
    ws, repo = _pushed_exited_lane(tmp_path)
    _git(repo, "checkout", "-q", "--detach")
    (repo / "r.txt").write_text("a release, off every branch\n")
    _git(repo, "add", "r.txt")
    _git(repo, "commit", "-q", "-m", "release")
    tagged = _git(repo, "rev-parse", "HEAD")
    _git(repo, "tag", "-a", "-m", "release", "v9.9.9")
    _git(repo, "push", "-q", "origin", "refs/tags/v9.9.9")
    _git(repo, "checkout", "-q", "feat/x")
    assert _git(repo, "log", "HEAD", "--branches", "--tags", "--not", "--remotes", "--format=%H") == tagged, \
        "the old query reads the pushed tag's commit as unpushed"
    return ws, repo, tagged


def test_a_commit_reachable_only_from_a_tag_the_remote_holds_is_removed(tmp_path: Path) -> None:
    ws, repo, _ = _lane_with_a_commit_only_a_pushed_tag_holds(tmp_path)

    result = _remove(ws)

    assert result.exit_code == 0, result.output
    assert not repo.exists(), "the lane's clone is gone"
    assert not lane_proto.lane_file(ws, "atlas", "x").exists(), "the lane record is gone"


def test_a_remote_whose_tags_cannot_be_listed_is_not_removed(tmp_path: Path) -> None:
    ws, repo, _ = _lane_with_a_commit_only_a_pushed_tag_holds(tmp_path)
    # Fetch skips this remote (skipFetchAll), so the fetch passes; asking it for its tags fails. An empty answer there
    # must not read as "nothing to check": the lane is refused, unknown.
    _git(repo, "remote", "add", "dead", str(tmp_path / "no-such-remote.git"))
    _git(repo, "config", "remote.dead.skipFetchAll", "true")
    _refused_untouched(ws, repo, _remove(ws), "lane_unpushed_unknown", "cannot list dead's tags")


def test_a_remote_named_like_an_option_is_refused_before_any_fetch(tmp_path: Path) -> None:
    ws, repo = _pushed_exited_lane(tmp_path)
    marker = tmp_path / "marker"
    # `fetch --all` hands each name to a child fetch with no "--": this name ran its command, then the lane was removed.
    name = f"--upload-pack=touch${{IFS}}{marker};git-upload-pack"
    _git(repo, "config", f"remote.{name}.url", _git(repo, "remote", "get-url", "origin"))
    _refused_untouched(ws, repo, _remove(ws), "lane_unpushed_unknown", "begins with '-'")
    assert not marker.exists(), "the remote's name never ran as a command"


def test_remote_tag_shas_reads_a_remote_named_like_an_option_as_a_name(tmp_path: Path) -> None:
    from gr2.python_cli.app import _remote_tag_shas
    _, repo, tagged = _lane_with_a_commit_only_a_pushed_tag_holds(tmp_path)
    # The name holds no "/", so git reads it as a configured remote; without "--" ls-remote ran it in the repo.
    name = "--upload-pack=touch${IFS}marker;git-upload-pack"
    _git(repo, "config", f"remote.{name}.url", _git(repo, "remote", "get-url", "origin"))
    _git(repo, "remote", "remove", "origin")
    assert tagged in _remote_tag_shas(repo), "the remote is asked by its name and answers with its tags"
    assert not (repo / "marker").exists(), "the remote's name never ran as a command"


def test_lane_list_names_a_tag_only_commit_unchecked_not_unpushed(tmp_path: Path) -> None:
    """The list does not fetch or ask remotes, so a commit only a tag holds is UNKNOWN there, never 'unpushed'."""
    ws, _, tagged = _lane_with_a_commit_only_a_pushed_tag_holds(tmp_path)

    result = runner.invoke(gr2_app.app, ["lane", "list", str(ws)])

    assert result.exit_code == 0, result.output
    row = next(line for line in result.output.splitlines() if line.startswith("atlas\tx\t"))
    assert "\tkeep" in row and "lane_unpushed_unknown" in row and "reachable only from a tag" in row, row
    assert "lane_has_unpushed_commit" not in row and f"{tagged[:12]} (refs/tags/" in row, row


def _legacy_lane(tmp_path: Path) -> tuple[Path, Path]:
    """A lane with no recorded checkout_root: its clones live in the record directory beside lane.toml."""
    ws, _ = _pushed_exited_lane(tmp_path)
    checkout = lane_proto.lane_checkout_root(ws, "atlas", "x")
    record = lane_proto.lane_dir(ws, "atlas", "x")
    (checkout / "repos").rename(record / "repos")
    doc = lane_proto.lane_file(ws, "atlas", "x")
    doc.write_text("".join(line for line in doc.read_text().splitlines(keepends=True) if not line.startswith("checkout_root")))
    assert lane_proto.lane_checkout_root(ws, "atlas", "x").resolve() == record.resolve()
    return ws, record / "repos" / "app"


def test_a_lane_whose_clones_live_in_its_record_directory_is_removed(tmp_path: Path) -> None:
    ws, repo = _legacy_lane(tmp_path)
    result = _remove(ws)
    assert result.exit_code == 0, result.output
    assert not repo.exists() and not lane_proto.lane_file(ws, "atlas", "x").exists()


def test_a_stray_file_in_a_record_directory_lane_is_still_named(tmp_path: Path) -> None:
    ws, repo = _legacy_lane(tmp_path)
    stray = lane_proto.lane_dir(ws, "atlas", "x") / "notes.md"
    stray.write_text("not a record file\n")
    _refused_untouched(ws, repo, _remove(ws), "lane_has_unlisted_content", "notes.md")


def test_lane_list_names_a_lane_with_a_broken_record_and_still_lists_the_rest(tmp_path: Path) -> None:
    """One malformed lane record must not hide every lane after it: list reports it as keep and goes on."""
    ws, _, _ = _exited_lane_with_a_commit(tmp_path)
    result = runner.invoke(gr2_app.app, ["lane", "create", str(ws), "atlas", "y", "--repos", "app", "--branch", "app=feat/y"])
    assert result.exit_code == 0, result.output
    doc = lane_proto.lane_file(ws, "atlas", "x")
    lines = doc.read_text().splitlines(keepends=True)
    assert sum(line.startswith("checkout_root") for line in lines) == 1
    doc.write_text("".join('checkout_root = ""\n' if line.startswith("checkout_root") else line for line in lines))

    result = runner.invoke(gr2_app.app, ["lane", "list", str(ws)])

    assert result.exit_code == 0, result.output
    rows = {line.split("\t")[1]: line for line in result.output.splitlines() if line.startswith("atlas\t")}
    assert set(rows) == {"x", "y"}, result.output
    assert "\tkeep" in rows["x"] and "lane_record_invalid" in rows["x"], rows["x"]
    assert "\tremovable" in rows["y"], rows["y"]
    removed = _remove(ws)
    assert removed.exit_code != 0 and "lane_record_invalid" in removed.output, removed.output


def _two_lanes_with_a_broken_record(tmp_path: Path, break_it) -> tuple[Path, Path]:
    """Lane x (with an unpushed commit) whose lane.toml text goes through `break_it`, beside a fine lane y."""
    ws, repo, _ = _exited_lane_with_a_commit(tmp_path)
    result = runner.invoke(gr2_app.app, ["lane", "create", str(ws), "atlas", "y", "--repos", "app", "--branch", "app=feat/y"])
    assert result.exit_code == 0, result.output
    record = lane_proto.lane_file(ws, "atlas", "x")
    record.write_text(break_it(record.read_text()))
    return ws, repo


def _broken_record_rows(ws: Path, repo: Path) -> None:
    listed = runner.invoke(gr2_app.app, ["lane", "list", str(ws)])
    assert listed.exit_code == 0, listed.output
    assert "lane_record_invalid" in listed.output, listed.output
    rows = [line for line in listed.output.splitlines() if "\tremovable" in line]
    assert any(line.split("\t")[1] == "y" for line in rows), listed.output
    removed = _remove(ws)
    assert removed.exit_code != 0 and "lane_record_invalid" in removed.output, removed.output
    assert lane_proto.lane_file(ws, "atlas", "x").is_file(), "the record is kept"
    assert repo.is_dir(), "the clone is kept"


def test_a_lane_record_that_is_not_valid_toml_is_named_and_kept(tmp_path: Path) -> None:
    _broken_record_rows(*_two_lanes_with_a_broken_record(tmp_path, lambda _: "this is [ not toml\n"))


def test_a_lane_record_without_a_lane_name_is_named_and_kept(tmp_path: Path) -> None:
    def drop_lane_name(text: str) -> str:
        lines = text.splitlines(keepends=True)
        assert sum(line.startswith("lane_name") for line in lines) == 1
        return "".join(line for line in lines if not line.startswith("lane_name"))
    _broken_record_rows(*_two_lanes_with_a_broken_record(tmp_path, drop_lane_name))


def test_an_empty_lane_record_is_named_and_neither_record_nor_clone_is_removed(tmp_path: Path) -> None:
    _broken_record_rows(*_two_lanes_with_a_broken_record(tmp_path, lambda _: ""))


def _native_lane(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """The README walk up to `lane create` on a workspace with a snapshot store: the lane root
    agents/default/lanes/x is itself a store checkout. Returns (ws, lane root)."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / ".gitconfig").write_text("[user]\n\tname = t\n\temail = t@e.invalid\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    remote = tmp_path / "app.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    _git(tmp_path, "clone", "-q", str(remote), str(ws / "app"))
    _git(ws / "app", "checkout", "-q", "-B", "main")
    (ws / "app" / "README").write_text("app\n")
    _git(ws / "app", "add", "README")
    _git(ws / "app", "commit", "-qm", "init")
    _git(ws / "app", "push", "-q", "origin", "main")
    monkeypatch.chdir(ws)
    for argv in (["workspace", "init", str(ws)], ["workspace", "materialize", str(ws), "--yes"],
                 ["store", "init", str(ws)], ["store", "commit", "-m", "first snapshot"],
                 ["lane", "create", str(ws), "default", "x", "--repos", "app", "--branch", "feat/x"]):
        result = runner.invoke(gr2_app.app, argv)
        assert result.exit_code == 0, f"{argv}: {result.output}"
    root = ws / "agents" / "default" / "lanes" / "x"
    assert (root / ".git").is_dir() and (root / "grip.toml").is_file(), "fixture check: the lane root is a store checkout"
    return ws, root


def _remove_native(ws: Path):
    return runner.invoke(gr2_app.app, ["lane", "remove", str(ws), "default", "x"])


def test_a_native_lane_whose_store_and_repos_are_all_pushed_is_removed(tmp_path: Path, monkeypatch) -> None:
    ws, root = _native_lane(tmp_path, monkeypatch)
    result = _remove_native(ws)
    assert result.exit_code == 0, result.output
    assert not root.exists()


def test_a_native_lane_root_holding_an_unpushed_store_commit_is_not_removed(tmp_path: Path, monkeypatch) -> None:
    ws, root = _native_lane(tmp_path, monkeypatch)
    _git(root, "commit", "-q", "--allow-empty", "-m", "store work that exists only here")
    commit = _git(root, "rev-parse", "HEAD")
    result = _remove_native(ws)
    assert result.exit_code != 0
    assert f"lane_has_unpushed_commit: lane-root {commit} (on " in result.output, result.output
    assert "lane_has_unlisted_content" not in result.output, result.output
    assert _git(root, "rev-parse", "HEAD") == commit


def test_a_native_lane_root_with_an_edited_store_file_is_not_removed(tmp_path: Path, monkeypatch) -> None:
    ws, root = _native_lane(tmp_path, monkeypatch)
    with (root / "grip.toml").open("a") as fh:
        fh.write("# an edit that exists only here\n")
    result = _remove_native(ws)
    assert result.exit_code != 0
    assert "lane_has_uncommitted_work: lane-root" in result.output, result.output
    assert (root / "grip.toml").is_file()


def test_a_native_lane_root_holding_a_store_stash_is_not_removed(tmp_path: Path, monkeypatch) -> None:
    ws, root = _native_lane(tmp_path, monkeypatch)
    with (root / "grip.toml").open("a") as fh:
        fh.write("# stashed\n")
    _git(root, "stash", "-q")
    result = _remove_native(ws)
    assert result.exit_code != 0
    assert "lane_has_stash: lane-root" in result.output, result.output
    assert root.exists()


def test_a_stray_file_in_a_native_lane_root_is_still_named(tmp_path: Path, monkeypatch) -> None:
    ws, root = _native_lane(tmp_path, monkeypatch)
    (root / "notes.txt").write_text("ignored by the store, so only the unlisted check sees it\n")
    (root / ".grip" / "mine").write_text("not the mirror cache\n")
    result = _remove_native(ws)
    assert result.exit_code != 0
    assert f"lane_has_unlisted_content: {root / 'notes.txt'}" in result.output, result.output
    assert f"lane_has_unlisted_content: {root / '.grip' / 'mine'}" in result.output, result.output
    assert f"lane_has_unlisted_content: {root / '.git'}" not in result.output, result.output
    assert (root / "notes.txt").is_file()


def test_a_remote_inside_the_checkout_root_does_not_hold_a_tagged_commit(tmp_path: Path) -> None:
    """A remote resolving into the directory remove deletes advertises the lane's own tags; the commit stays listed."""
    ws, repo, tagged = _lane_with_a_local_tag_commit(tmp_path)
    _git(repo, "remote", "add", "self", str(repo))
    assert tagged in _git(repo, "ls-remote", "--tags", "self")
    _refused_untouched(ws, repo, _remove(ws), "lane_has_unpushed_commit", tagged[:12])


def test_a_remote_spelled_file_localhost_inside_the_checkout_root_does_not_hold_a_tagged_commit(tmp_path: Path) -> None:
    """file://localhost/<path> names /<path>, not a path relative to the repo; the lane's own tag stays listed."""
    ws, repo, tagged = _lane_with_a_local_tag_commit(tmp_path)
    _git(repo, "remote", "add", "self", f"file://localhost{repo}")
    assert tagged in _git(repo, "ls-remote", "--tags", "self")
    _refused_untouched(ws, repo, _remove(ws), "lane_has_unpushed_commit", tagged[:12])


def test_a_remote_spelled_file_otherhost_inside_the_checkout_root_does_not_hold_a_tagged_commit(tmp_path: Path) -> None:
    """Off Windows git ignores the host of file://otherhost/<path> and reads /<path>; the lane's own tag stays listed."""
    ws, repo, tagged = _lane_with_a_local_tag_commit(tmp_path)
    _git(repo, "remote", "add", "self", f"file://otherhost{repo}")
    assert tagged in _git(repo, "ls-remote", "--tags", "self")
    _refused_untouched(ws, repo, _remove(ws), "lane_has_unpushed_commit", tagged[:12])


def test_a_remote_spelled_file_localhost_outside_the_checkout_root_holds_a_tagged_commit(tmp_path: Path) -> None:
    """file://localhost/<path> outside the lane is asked, not read as a path under the repo: its tag holds the commit."""
    ws, repo, tagged = _lane_with_a_local_tag_commit(tmp_path)
    keep = tmp_path / "keep.git"
    _git(tmp_path, "clone", "-q", "--bare", str(repo), str(keep))
    _git(repo, "push", "-q", str(keep), "refs/tags/0.5.0")
    _git(repo, "remote", "add", "keep", f"file://localhost{keep}")
    assert tagged in _git(repo, "ls-remote", "--tags", "keep")

    result = _remove(ws)

    assert result.exit_code == 0, result.output
    assert not repo.exists(), "the lane's clone is gone"


@pytest.mark.parametrize("url", ["C:/x", "C:\\x"])
def test_a_drive_path_remote_url_is_local(url: str) -> None:
    assert gr2_app._remote_url_local_path(url) == url


@pytest.mark.parametrize("url", ["host:path", "git@host:o/r", "https://host/o/r"])
def test_a_scp_like_or_network_remote_url_is_not_local(url: str) -> None:
    assert gr2_app._remote_url_local_path(url) is None


@pytest.mark.parametrize("url", ["file://localhost/abs", "file:///abs"])
def test_a_file_url_with_no_host_or_localhost_names_the_absolute_path(url: str) -> None:
    assert gr2_app._remote_url_local_path(url) == "/abs"


def test_a_file_url_naming_another_host_is_a_share_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(gr2_app.os, "name", "nt")
    assert gr2_app._remote_url_local_path("file://otherhost/abs") is None


def test_a_file_url_naming_another_host_names_the_absolute_path_off_windows(monkeypatch) -> None:
    monkeypatch.setattr(gr2_app.os, "name", "posix")
    assert gr2_app._remote_url_local_path("file://otherhost/abs") == "/abs"


def test_a_sibling_remote_inside_the_checkout_root_does_not_hold_a_tagged_commit(tmp_path: Path) -> None:
    """A bare clone beside the lane repo, inside the checkout root, holds the lane's tag; remove deletes it too,
    so it holds nothing elsewhere. Measures removing= at lane remove: the repo itself is not the remote here."""
    ws, repo, tagged = _lane_with_a_local_tag_commit(tmp_path)
    sibling = lane_proto.lane_checkout_root(ws, "atlas", "x") / "mirror.git"
    _git(tmp_path, "clone", "-q", "--bare", str(repo), str(sibling))
    _git(repo, "push", "-q", str(sibling), "refs/tags/0.5.0")
    _git(repo, "remote", "add", "sibling", str(sibling))
    assert tagged in _git(repo, "ls-remote", "--tags", "sibling")
    _refused_untouched(ws, repo, _remove(ws), f"lane_has_unpushed_commit: app {tagged}")
