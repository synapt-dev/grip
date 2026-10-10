"""A temporary lane must be able to end, and ending it must never destroy unlanded work.

Measured on dev 4ada547e: gr2 has `lane create`, `enter`, `exit`, `show`, `resolve`, `bind` and `lease`, and nothing
removes a lane. `lane exit` only leaves it, so every lane's clones stay on disk for good.
`lane remove` ends a lane, and refuses while any of its repos holds work that
exists nowhere else. First row: a local commit that was never pushed.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

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


def test_a_bound_lane_is_not_removed_and_its_worktree_is_untouched(tmp_path: Path) -> None:
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
    _refused_untouched(ws, wt, _remove(ws), "lane_is_bound")
    assert (wt / ".git").is_dir()


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
