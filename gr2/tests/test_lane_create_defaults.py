"""`lane create` takes the repos and the branch from what the workspace already holds (tier A3, rows I7 and I8).

Written by the author of the change (not by an independent reader). Each row names what must make it go red:
  * the repos default ignores the workspace spec            -> the defaults row (one repo would be missing)
  * the branch default is not the lane name                  -> the defaults row
  * a typed value does not win, or prints a line             -> the explicit row
  * a remote branch of that name at other work is not refused -> the collision rows
  * the refusal names one reading only                       -> the collision row (both ways out are asserted)
  * an unreachable remote proceeds without saying so          -> the not-checked row
  * the check spends its budget per repository               -> the budget row
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gr2.prototypes import lane_workspace_prototype as lanes
from gr2.python_cli import app as gr2_app
from gr2.python_cli import lane_defaults

from tests.test_lane_create_fork_base_cli import _git, _workspace
from tests.test_store_break_attempts import two_member_ws  # noqa: F401

runner = CliRunner()


def _flat(res) -> str:
    """The output as a person reads it: colour and the error box's drawing characters removed, wrapped lines joined."""
    import re

    text = re.sub(r"\x1b\[[0-9;]*m", "", res.output)
    text = re.sub(r"[│╭╮╰╯─]", " ", text)
    return " ".join(text.split())


def _create(ws: Path, *args: str):
    return runner.invoke(gr2_app.app, ["lane", "create", str(ws), "atlas", "demo", *args])


def _origin(ws: Path, name: str) -> str:
    spec = (ws / ".grip" / "workspace_spec.toml").read_text()
    return next(line.split('"')[1] for line in spec.splitlines() if line.startswith("url") and f"/{name}.git" in line)


def test_a_bare_lane_create_takes_every_repo_and_the_lane_name(tmp_path: Path) -> None:
    """THE SMALLEST WORKING PROOF. No --repos, no --branch: a two-repo lane on a branch named for the lane, and
    two stderr lines saying where each value came from."""
    ws, tips = _workspace(tmp_path, ["alpha", "beta"])
    res = _create(ws)
    assert res.exit_code == 0, res.output
    assert "gr2: repos=alpha,beta (every repo of the workspace spec; this makes 2 clones)" in res.output
    assert "gr2: branch=demo (the lane name)" in res.output
    doc = lanes.load_lane_doc(ws, "atlas", "demo")
    assert sorted(doc["fork_base"]) == ["alpha", "beta"]
    assert all(v["sha"] == tips[k] for k, v in doc["fork_base"].items())
    lane_root = lanes.lane_dir(ws, "atlas", "demo")
    assert _git(lane_root / "repos" / "alpha", "rev-parse", "--abbrev-ref", "HEAD") == "demo"
    assert _git(lane_root / "repos" / "beta", "rev-parse", "--abbrev-ref", "HEAD") == "demo"


def test_typed_values_win_and_print_nothing_about_themselves(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path, ["alpha", "beta"])
    res = _create(ws, "--repos", "alpha", "--branch", "feat/demo")
    assert res.exit_code == 0, res.output
    assert "repos=" not in res.output and "branch=" not in res.output
    doc = lanes.load_lane_doc(ws, "atlas", "demo")
    assert list(doc["fork_base"]) == ["alpha"] and doc["fork_base"]["alpha"]["branch"] == "feat/demo"


def test_the_quiet_flag_in_the_environment_hushes_the_default_lines(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GR2_QUIET_CONTEXT", "1")
    ws, _ = _workspace(tmp_path, ["alpha"])
    res = _create(ws)
    assert res.exit_code == 0 and "gr2: " not in res.output, res.output


def _push_branch(ws: Path, name: str, branch: str) -> str:
    """Put `branch` on the repo's remote at a commit AHEAD of main."""
    src = ws / "repos" / name
    _git(src, "checkout", "-q", "-b", branch)
    (src / "pushed.txt").write_text("work already pushed\n")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "pushed work")
    _git(src, "push", "-q", "origin", branch)
    _git(src, "checkout", "-q", "main")
    return _git(src, "rev-parse", branch)


def test_a_remote_branch_of_the_lane_name_at_other_work_refuses_naming_both_ways_out(tmp_path: Path) -> None:
    """The control for the proof row: the same call, with `demo` already on beta's remote ahead of main. Goes red if
    the collision is not refused, or if the message gives only one way out."""
    ws, _ = _workspace(tmp_path, ["alpha", "beta"])
    tip = _push_branch(ws, "beta", "demo")
    res = _create(ws)
    assert res.exit_code != 0
    text = _flat(res)
    assert "'demo' already exists on beta's remote" in text and tip[:12] in text
    assert "pass --branch demo to use the existing branch" in text and "pick another lane name" in text
    with pytest.raises(SystemExit, match="lane not found"):
        lanes.load_lane_doc(ws, "atlas", "demo")  # nothing was created


def test_naming_the_existing_branch_is_the_way_through(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path, ["alpha", "beta"])
    _push_branch(ws, "beta", "demo")
    res = _create(ws, "--branch", "demo")
    assert res.exit_code == 0, res.output


def test_an_unreadable_grip_toml_refuses_a_defaulted_branch_instead_of_skipping_the_check(tmp_path: Path) -> None:
    """The check cannot be made, so the defaulted branch is refused and nothing is created (the head's probe F1: the
    lane was made on a branch already pushed ahead, with no line saying it was not checked). The control is the same
    call on a readable root, which refuses naming beta's remote. Goes red if the unreadable file is read as 'no repos'."""
    ws, _ = _workspace(tmp_path, ["alpha", "beta"])
    _push_branch(ws, "beta", "demo")
    control = _create(ws, "--repos", "alpha,beta")
    assert control.exit_code == 2 and "already exists on beta's remote" in _flat(control)
    (ws / "grip.toml").write_text("this is [ not toml")
    res = _create(ws, "--repos", "alpha,beta")
    assert res.exit_code == 2, res.output
    assert "cannot be checked against the remotes" in _flat(res) and "--branch" in _flat(res)
    assert not lanes.lane_dir(ws, "atlas", "demo").exists()


def test_a_typed_repo_the_workspace_files_do_not_declare_is_named_as_not_checked(tmp_path: Path) -> None:
    """A chosen repo with no declaration cannot be asked; it is named, never silently dropped from the check."""
    ws, _ = _workspace(tmp_path, ["alpha"])
    res = _create(ws, "--repos", "alpha,ghost")
    assert "ghost (not declared in the workspace files)" in _flat(res)


def test_the_repos_default_reads_the_spec_only_and_never_offers_a_grip_toml_member_the_lane_refuses(tmp_path: Path) -> None:
    """Spec alpha,beta plus grip.toml members alpha,gamma: the default is alpha,beta with a true label and exit 0 (the
    head's probe F2 announced alpha,beta,gamma and then died on 'unknown repos for lane: gamma'). Goes red if the
    grip.toml members are unioned back into the default."""
    ws, _ = _workspace(tmp_path, ["alpha", "beta"])
    (ws / "grip.toml").write_text(
        '[[members]]\nname = "alpha"\npath = "alpha"\n\n[[members]]\nname = "gamma"\npath = "gamma"\n'
    )
    res = _create(ws)
    assert res.exit_code == 0, res.output
    assert "gr2: repos=alpha,beta (every repo of the workspace spec; this makes 2 clones)" in _flat(res)
    assert sorted(lanes.load_lane_doc(ws, "atlas", "demo")["fork_base"]) == ["alpha", "beta"]


def test_a_remote_branch_at_the_base_is_not_a_collision(tmp_path: Path) -> None:
    ws, tips = _workspace(tmp_path, ["alpha"])
    src = ws / "repos" / "alpha"
    _git(src, "push", "-q", "origin", "main:refs/heads/demo")  # the same commit as main
    assert _create(ws).exit_code == 0


def test_an_unreachable_remote_proceeds_and_says_which_repos_were_not_checked(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path, ["alpha", "beta"])
    spec = ws / ".grip" / "workspace_spec.toml"
    text = spec.read_text().replace(_origin(ws, "alpha"), str(tmp_path / "nowhere.git"))
    spec.write_text(text)
    res = _create(ws)
    assert "'demo' was not checked against the remote for:" in res.output and "alpha (" in res.output
    assert "beta (not asked)" in res.output  # the check stopped at the first remote it could not ask


def test_the_remote_check_spends_one_budget_for_the_whole_workspace(monkeypatch) -> None:
    """Goes red if the budget is per repository: ten hanging remotes must not wait ten times."""
    asked: list[float] = []

    def slow(url: str, ref: str, timeout: float):
        asked.append(timeout)
        time.sleep(0.3)
        raise TimeoutError("did not answer in time")

    monkeypatch.setattr(lane_defaults, "_ls_remote", slow)
    repos = [lane_defaults.Repo(f"r{i}", f"/x/{i}", None) for i in range(10)]
    t = time.monotonic()
    result = lane_defaults.check_remote_branch(repos, "demo", budget=5.0)
    assert len(asked) == 1 and time.monotonic() - t < 2.0
    assert len(result.not_checked) == 10 and result.not_checked[0].startswith("r0 (") and result.not_checked[-1] == "r9 (not asked)"


def test_a_bind_lane_still_needs_its_repo_named(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path, ["alpha"])
    res = runner.invoke(gr2_app.app, ["lane", "create", str(ws), "atlas", "demo", "--bind", str(tmp_path)])
    assert res.exit_code != 0 and "--repos is required with --bind" in _flat(res)


# --- gr2 add: tier A3, row I9 -----------------------------------------------------------------------------


def _entered_lane(tmp_path: Path):
    ws, _ = _workspace(tmp_path, ["alpha", "beta"])
    assert _create(ws).exit_code == 0
    res = runner.invoke(gr2_app.app, ["lane", "enter", str(ws), "atlas", "demo", "--actor", "human:t"])
    assert res.exit_code == 0, res.output
    lane_root = lanes.lane_dir(ws, "atlas", "demo")
    for repo in ("alpha", "beta"):
        (lane_root / "repos" / repo / "edit.txt").write_text(f"change in {repo}\n")
    return ws, lane_root


def _staged(repo: Path) -> list[str]:
    return [p for p in _git(repo, "diff", "--cached", "--name-only").splitlines() if p]


def test_add_dot_inside_a_lane_repo_stages_that_repo_only(tmp_path: Path, monkeypatch) -> None:
    ws, lane_root = _entered_lane(tmp_path)
    monkeypatch.chdir(lane_root / "repos" / "alpha")
    res = runner.invoke(gr2_app.app, ["add", "."])
    assert res.exit_code == 0, res.output
    assert _staged(lane_root / "repos" / "alpha") == ["edit.txt"] and _staged(lane_root / "repos" / "beta") == []


def test_add_lane_stages_every_repo_of_the_entered_lane_and_says_where_it_read_them(tmp_path: Path, monkeypatch) -> None:
    ws, lane_root = _entered_lane(tmp_path)
    monkeypatch.chdir(ws)
    res = runner.invoke(gr2_app.app, ["add", "--lane", "."])
    assert res.exit_code == 0, res.output
    assert _staged(lane_root / "repos" / "alpha") == ["edit.txt"] and _staged(lane_root / "repos" / "beta") == ["edit.txt"]
    assert "gr2: unit=atlas (the only unit with an entered lane)" in res.output
    assert "gr2: lane=demo (the current lane of unit atlas)" in res.output
    assert "alpha: staged 1 path(s): edit.txt" in res.output and "beta: staged 1 path(s): edit.txt" in res.output


def test_add_dot_at_the_workspace_root_with_a_lane_entered_refuses_and_stages_nothing(tmp_path: Path, monkeypatch) -> None:
    """Goes red if the refusal is dropped (the root's own repository would be staged) or if it names only one way out."""
    ws, lane_root = _entered_lane(tmp_path)
    (ws / "stray.txt").write_text("not for this\n")
    monkeypatch.chdir(ws)
    res = runner.invoke(gr2_app.app, ["add", "."])
    text = _flat(res)
    assert res.exit_code == 2, res.output
    assert "not a lane repo" in text and "atlas/demo" in text and "--lane" in text and "--repo-path" in text
    assert _staged(ws) == [] and _staged(lane_root / "repos" / "alpha") == []


def test_add_repo_path_at_the_root_is_the_explicit_way_to_stage_the_root(tmp_path: Path, monkeypatch) -> None:
    ws, _ = _entered_lane(tmp_path)
    (ws / "stray.txt").write_text("on purpose\n")
    monkeypatch.chdir(ws)
    res = runner.invoke(gr2_app.app, ["add", "--repo-path", ".", "stray.txt"])
    assert res.exit_code == 0, res.output
    assert "stray.txt" in _staged(ws)


def test_add_dot_at_the_root_with_no_lane_entered_is_what_it_always_was(tmp_path: Path, monkeypatch) -> None:
    ws, _ = _workspace(tmp_path, ["alpha"])
    (ws / "stray.txt").write_text("plain\n")
    monkeypatch.chdir(ws)
    res = runner.invoke(gr2_app.app, ["add", "stray.txt"])
    assert res.exit_code == 0, res.output
    assert "stray.txt" in _staged(ws)


def test_add_lane_with_no_entered_lane_refuses_and_with_repo_path_is_a_contradiction(tmp_path: Path, monkeypatch) -> None:
    ws, _ = _workspace(tmp_path, ["alpha"])
    monkeypatch.chdir(ws)
    res = runner.invoke(gr2_app.app, ["add", "--lane", "."])
    assert res.exit_code == 2 and ("--unit" in _flat(res) or "entered lane" in _flat(res)), res.output
    both = runner.invoke(gr2_app.app, ["add", "--lane", "--repo-path", str(ws), "."])
    assert both.exit_code == 2 and "do not combine" in _flat(both)


def test_slow_remotes_that_do_answer_still_share_one_budget(monkeypatch) -> None:
    """Remotes that answer slowly (no failure to stop on) must not each get a fresh budget. Goes red if the deadline
    is per repository or is not enforced once spent."""
    calls: list[str] = []

    def slow(url: str, ref: str, timeout: float):
        calls.append(url)
        time.sleep(0.25)
        return ""

    monkeypatch.setattr(lane_defaults, "_ls_remote", slow)
    repos = [lane_defaults.Repo(f"r{i}", f"/x/{i}", None) for i in range(10)]
    t = time.monotonic()
    result = lane_defaults.check_remote_branch(repos, "demo", budget=0.6)
    assert time.monotonic() - t < 1.5 and 2 <= len(calls) <= 4, (len(calls), time.monotonic() - t)
    assert result.not_checked and "budget was spent" in result.not_checked[-1] and result.not_checked[-1].startswith("r9 ")


# --- a root with no workspace spec: refuse before writing anything (the stranger's `store init` root) ------------


def _store_init_only_root(two_member_ws: Path) -> Path:
    """The root `store init` makes: grip.toml and a root git repository, members beside it, NO workspace spec."""
    from tests.test_store_break_attempts import _cli as store_cli

    assert store_cli("store", "init", str(two_member_ws))[0] == 0
    assert not (two_member_ws / ".grip" / "workspace_spec.toml").exists()
    return two_member_ws


@pytest.mark.parametrize("flags", [[], ["--repos", "alpha", "--branch", "demo"]], ids=["bare", "typed"])
def test_lane_create_on_a_store_init_root_refuses_before_writing_anything(two_member_ws: Path, flags: list[str]) -> None:
    """The control is the same call on dev, which ends in FileNotFoundError out of the lane machinery (RAN, on a root
    made by `store init`: with typed flags too). Goes red if the refusal is removed: the call raises."""
    root = _store_init_only_root(two_member_ws)
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*") if ".git" not in p.parts)
    res = runner.invoke(gr2_app.app, ["lane", "create", str(root), "default", "demo", *flags])
    text = _flat(res)
    assert res.exit_code == 2, res.output
    assert res.exception is None or isinstance(res.exception, SystemExit)
    assert "has no workspace spec" in text and "`store init` holds only grip.toml" in text.replace("a root made by ", "")
    assert f"gr2 workspace init {root}" in text and "Nothing was created" in text
    assert sorted(str(p.relative_to(root)) for p in root.rglob("*") if ".git" not in p.parts) == before


def test_the_way_out_the_refusal_names_works(two_member_ws: Path) -> None:
    """The sentence says to run `workspace init`; this is that sentence followed, then the same bare call."""
    root = _store_init_only_root(two_member_ws)
    assert runner.invoke(gr2_app.app, ["workspace", "init", str(root)]).exit_code == 0
    res = runner.invoke(gr2_app.app, ["lane", "create", str(root), "default", "demo"])
    assert res.exit_code == 0, res.output
    assert "gr2: repos=alpha,beta" in res.output


def test_a_skipped_remote_check_is_printed_even_when_context_lines_are_hushed(tmp_path: Path, monkeypatch) -> None:
    """A safety check that did not run is not information to hush. Goes red if the line is tied to the quiet flag."""
    monkeypatch.setenv("GR2_QUIET_CONTEXT", "1")
    ws, _ = _workspace(tmp_path, ["alpha"])
    spec = ws / ".grip" / "workspace_spec.toml"
    spec.write_text(spec.read_text().replace(_origin(ws, "alpha"), str(tmp_path / "nowhere.git")))
    res = _create(ws)
    assert "was not checked against the remote for:" in res.output and "alpha (" in res.output
    assert "gr2: repos=" not in res.output  # the ordinary lines stay hushed
