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

from tests.test_lane_create_fork_base_cli import _git, _workspace as _native_workspace
from tests.test_store_break_attempts import two_member_ws  # noqa: F401

runner = CliRunner()


def _workspace(tmp_path, repos):
    """Legacy defaults exercise live documents without a native root commit owner."""
    import shutil
    ws, tips = _native_workspace(tmp_path, repos)
    shutil.rmtree(ws / ".git")
    return ws, tips


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
    lane_root = ws / "agents" / "atlas" / "lanes" / "demo"
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
    assert "the lane starts at its remote tip and tracks it" in text  # and the next row proves the sentence
    with pytest.raises(SystemExit, match="lane not found"):
        lanes.load_lane_doc(ws, "atlas", "demo")  # nothing was created


def test_naming_the_existing_branch_is_the_way_through(tmp_path: Path) -> None:
    """The refusal above says --branch continues the pushed work, so it must: the lane's beta is at the pushed tip
    and tracks it. Goes red if the lane branch has no upstream."""
    ws, _ = _workspace(tmp_path, ["alpha", "beta"])
    tip = _push_branch(ws, "beta", "demo")
    res = _create(ws, "--branch", "demo")
    assert res.exit_code == 0, res.output
    beta = ws / "agents" / "atlas" / "lanes" / "demo" / "repos" / "beta"
    assert _git(beta, "rev-parse", "HEAD") == tip
    assert _git(beta, "rev-parse", "--abbrev-ref", "demo@{upstream}") == "origin/demo"


# --- a branch the remote already has is continued, never shadowed -------------


def _lane_repo(ws: Path, name: str, lane: str = "demo") -> Path:
    native = ws / "agents" / "atlas" / "lanes" / lane / "repos" / name
    return native


def test_spec_lane_on_a_pushed_branch_the_source_does_not_hold_starts_at_the_remote_tip(tmp_path: Path) -> None:
    """A spec root: `demo` is on beta's remote and NOT a local branch of the source. Before the fix
    the lane's demo was made at the source's HEAD (main) with no upstream, a second history under the same name.
    Goes red if HEAD is not the remote tip, there is no upstream, or the fork base is not the merge-base with main."""
    ws, tips = _workspace(tmp_path, ["alpha", "beta"])
    tip = _push_branch(ws, "beta", "demo")
    _git(ws / "repos" / "beta", "branch", "-q", "-D", "demo")
    res = _create(ws, "--branch", "demo")
    assert res.exit_code == 0, res.output
    beta = _lane_repo(ws, "beta")
    assert _git(beta, "rev-parse", "HEAD") == tip
    assert _git(beta, "rev-parse", "--abbrev-ref", "demo@{upstream}") == "origin/demo"
    assert f"gr2: beta: continues origin/demo at {tip[:12]}" in _flat(res)
    doc = lanes.load_lane_doc(ws, "atlas", "demo")
    assert doc["fork_base"]["beta"]["sha"] == tips["beta"]  # the merge-base with main, so a review covers the pushed work
    assert doc["fork_base"]["alpha"]["sha"] == tips["alpha"]  # control: a branch the remote lacks starts where it did
    assert _git(_lane_repo(ws, "alpha"), "rev-parse", "HEAD") == tips["alpha"]


def test_native_lane_on_a_pushed_branch_starts_at_the_remote_tip_not_the_pin(tmp_path: Path) -> None:
    """A native root, --branch naming a branch on the remote ahead of the member's pin. Before
    the fix `checkout -B demo <pin>` made demo at the pin with no upstream. Goes red if the remote-tip checkout is
    removed (the mutant) or the upstream is not set."""
    ws, tips = _native_workspace(tmp_path, ["alpha", "beta"])
    tip = _push_branch(ws, "beta", "demo")
    res = _create(ws, "--branch", "demo")
    assert res.exit_code == 0, res.output
    lane_root = lanes.lane_checkout_root(ws, "atlas", "demo")
    beta = next(p for p in lane_root.rglob("beta") if (p / ".git").exists())
    assert _git(beta, "rev-parse", "HEAD") == tip
    assert _git(beta, "rev-parse", "--abbrev-ref", "demo@{upstream}") == "origin/demo"
    assert lanes.load_lane_doc(ws, "atlas", "demo")["fork_base"]["beta"]["sha"] == tips["beta"]


def test_a_local_branch_with_unpushed_work_is_kept_and_tracks_the_remote(tmp_path: Path) -> None:
    """The author's own local demo is one commit ahead of the pushed demo. Continuing must not drop that commit:
    the lane keeps it and tracks origin/demo, and says so. Goes red if the lane is reset to the remote tip."""
    ws, _ = _workspace(tmp_path, ["beta"])
    pushed = _push_branch(ws, "beta", "demo")
    src = ws / "repos" / "beta"
    _git(src, "checkout", "-q", "demo")
    (src / "unpushed.txt").write_text("not on the remote yet\n")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "unpushed work")
    local = _git(src, "rev-parse", "HEAD")
    _git(src, "checkout", "-q", "main")
    res = _create(ws, "--branch", "demo")
    assert res.exit_code == 0, res.output
    beta = _lane_repo(ws, "beta")
    assert _git(beta, "rev-parse", "HEAD") == local != pushed
    assert _git(beta, "rev-parse", "--abbrev-ref", "demo@{upstream}") == "origin/demo"


def test_a_new_lane_on_a_stale_pin_says_how_far_behind_it_starts(tmp_path: Path) -> None:
    """A new branch forks at the workspace commit's pin, which can be far behind the remote's
    integration tip. The pin still owns the fork point (10-06 rule); create now says how far behind it is. Goes red
    if the line is missing; the control is the same create with the remote not moved, which prints no such line."""
    ws, tips = _native_workspace(tmp_path, ["alpha"])
    control = _create(ws, "--branch", "fresh")
    assert control.exit_code == 0 and "behind origin/" not in _flat(control), control.output
    src = ws / "repos" / "alpha"
    (src / "later.txt").write_text("main moved on\n")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "main moved")
    _git(src, "push", "-q", "origin", "main")
    res = runner.invoke(gr2_app.app, ["lane", "create", str(ws), "atlas", "second", "--branch", "fresh2", "--at-pin"])
    assert res.exit_code == 0, res.output
    assert f"gr2: alpha: fresh2 starts at {tips['alpha'][:12]}" in _flat(res) and "1 commit behind origin/" in _flat(res)


def _move_main(ws: Path, name: str) -> str:
    """Push one commit onto the member's remote main AFTER the workspace pinned it; returns the new remote tip."""
    src = ws / "repos" / name
    (src / "later.txt").write_text("main moved on\n")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "main moved")
    _git(src, "push", "-q", "origin", "main")
    return _git(src, "rev-parse", "HEAD")


def test_a_branch_new_to_the_remote_starts_at_the_live_tip_and_the_doc_says_tip(tmp_path: Path) -> None:
    """The remote's main moved past the workspace pin. A bare create on a branch the remote lacks starts
    at the LIVE tip of the target (ls-remote), not the pin, and the doc and the output say `tip <sha>`.
    Goes red if the branch is based at the pin (the mutant base-from-pin)."""
    ws, tips = _native_workspace(tmp_path, ["alpha"])
    live = _move_main(ws, "alpha")
    res = _create(ws, "--branch", "fresh")
    assert res.exit_code == 0, res.output
    alpha = next(p for p in lanes.lane_checkout_root(ws, "atlas", "demo").rglob("alpha") if (p / ".git").exists())
    assert _git(alpha, "rev-parse", "HEAD") == live != tips["alpha"]
    assert lanes.load_lane_doc(ws, "atlas", "demo")["fork_base"]["alpha"] == {"branch": "fresh", "sha": live, "start": "tip"}
    assert f"gr2: alpha: fresh tip {live}" in _flat(res)


def test_workspace_commit_keeps_its_own_meaning_and_does_not_choose_the_pin(tmp_path: Path) -> None:
    """--workspace-commit names which workspace commit is materialized, nothing more: with the remote moved and no
    --at-pin, the new branch still starts at the live tip. Goes red if --workspace-commit is read as --at-pin."""
    ws, tips = _native_workspace(tmp_path, ["alpha"])
    live = _move_main(ws, "alpha")
    res = _create(ws, "--branch", "fresh", "--workspace-commit", "HEAD")
    assert res.exit_code == 0, res.output
    alpha = next(p for p in lanes.lane_checkout_root(ws, "atlas", "demo").rglob("alpha") if (p / ".git").exists())
    assert _git(alpha, "rev-parse", "HEAD") == live != tips["alpha"]
    assert f"gr2: alpha: fresh tip {live}" in _flat(res)


def test_at_pin_with_bind_is_refused_by_name(tmp_path: Path) -> None:
    """--bind materializes no branch, so there is no start for --at-pin to choose; the pair is refused, not ignored."""
    ws, _ = _native_workspace(tmp_path, ["alpha"])
    res = _create(ws, "--bind", str(ws / "repos" / "alpha"), "--at-pin")
    assert res.exit_code != 0
    assert "--at-pin starts a materialized lane branch; --bind materializes nothing" in _flat(res)


def test_a_branch_the_remote_already_has_keeps_its_path_and_records_no_start(tmp_path: Path) -> None:
    """The existing-remote-branch path is unchanged by the tip rule even when main moved: the lane
    continues origin/demo and the doc carries no tip/pin word for it."""
    ws, tips = _native_workspace(tmp_path, ["alpha", "beta"])
    tip = _push_branch(ws, "beta", "demo")
    _move_main(ws, "beta")
    res = _create(ws, "--branch", "demo")
    assert res.exit_code == 0, res.output
    beta = next(p for p in lanes.lane_checkout_root(ws, "atlas", "demo").rglob("beta") if (p / ".git").exists())
    assert _git(beta, "rev-parse", "HEAD") == tip
    assert lanes.load_lane_doc(ws, "atlas", "demo")["fork_base"]["beta"] == {"branch": "demo", "sha": tips["beta"]}
    assert f"gr2: beta: continues origin/demo at {tip[:12]}" in _flat(res)


def test_the_help_names_the_tip_default_and_the_at_pin_form_in_one_sentence() -> None:
    """A user moving from the old pin default reads both starts in one place. Goes red if the help drops either."""
    res = runner.invoke(gr2_app.app, ["lane", "create", "--help"], env={"COLUMNS": "400"})
    assert res.exit_code == 0, res.output
    assert ("A branch new to the remote starts at the live tip of its target, or at the declared pin when that tip "
            "cannot be read; with --at-pin it starts at the declared pin instead. A local branch of that name holding "
            "unpushed work starts at that work either way.") in _flat(res)


def _git_moving_main_at_ls_remote(monkeypatch, ws: Path, name: str, *, fail: bool = False) -> dict:
    """Wrap the CLI's git so the member's remote main moves (or the ls-remote fails) at the moment the live tip is
    read: the lane clone then holds neither the new tip object nor a remote-tracking ref at it."""
    real, seen = gr2_app.git, {}

    def git(repo, *args, **kw):
        if args[:1] == ("ls-remote",) and "live" not in seen:
            seen["live"] = _move_main(ws, name)
            if fail:
                return subprocess.CompletedProcess(["git", *args], 128, "", "fatal: could not read from remote")
        return real(repo, *args, **kw)

    monkeypatch.setattr(gr2_app, "git", git)
    return seen


def test_the_live_tip_is_read_from_the_remote_and_fetched_when_the_clone_lacks_it(tmp_path: Path, monkeypatch) -> None:
    """Main moves on the remote after the lane clone was made. The branch starts at the remote's tip as read then,
    not at the clone's remote-tracking ref, and the missing commit is fetched before the checkout.
    Goes red if the tip is read from refs/remotes/origin, or if the fetch of a missing tip is dropped."""
    ws, tips = _native_workspace(tmp_path, ["alpha"])
    seen = _git_moving_main_at_ls_remote(monkeypatch, ws, "alpha")
    res = _create(ws, "--branch", "fresh")
    assert res.exit_code == 0, res.output
    live = seen["live"]
    alpha = next(p for p in lanes.lane_checkout_root(ws, "atlas", "demo").rglob("alpha") if (p / ".git").exists())
    assert _git(alpha, "rev-parse", "HEAD") == live != tips["alpha"]
    assert lanes.load_lane_doc(ws, "atlas", "demo")["fork_base"]["alpha"] == {"branch": "fresh", "sha": live, "start": "tip"}


def test_an_unreadable_live_tip_starts_at_the_pin_and_says_so(tmp_path: Path, monkeypatch) -> None:
    """The remote cannot be asked for its tip: the branch starts at the pin, one line says why, the doc says pin."""
    ws, tips = _native_workspace(tmp_path, ["alpha"])
    _git_moving_main_at_ls_remote(monkeypatch, ws, "alpha", fail=True)
    res = _create(ws, "--branch", "fresh")
    assert res.exit_code == 0, res.output
    alpha = next(p for p in lanes.lane_checkout_root(ws, "atlas", "demo").rglob("alpha") if (p / ".git").exists())
    assert _git(alpha, "rev-parse", "HEAD") == tips["alpha"]
    assert "gr2: alpha: cannot read the live tip of origin/" in _flat(res) and "fresh starts at the pin" in _flat(res)
    assert lanes.load_lane_doc(ws, "atlas", "demo")["fork_base"]["alpha"]["start"] == "pin"


def test_a_fork_base_start_outside_tip_pin_local_is_refused(tmp_path: Path) -> None:
    """The lane doc's start word is one of three; any other is refused by name and nothing is written."""
    ws, tips = _native_workspace(tmp_path, ["alpha"])
    assert _create(ws, "--branch", "fresh").exit_code == 0
    before = lanes.load_lane_doc(ws, "atlas", "demo")["fork_base"]
    with pytest.raises(SystemExit, match="must be tip, pin or local, got: 'head'"):
        lanes.record_fork_base(ws, "atlas", "demo", {"alpha": {"branch": "fresh", "sha": tips["alpha"], "start": "head"}})
    assert lanes.load_lane_doc(ws, "atlas", "demo")["fork_base"] == before


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
    ws, _ = _native_workspace(tmp_path, ["alpha", "beta"])
    assert _create(ws).exit_code == 0
    res = runner.invoke(gr2_app.app, ["lane", "enter", str(ws), "atlas", "demo", "--actor", "human:t"])
    assert res.exit_code == 0, res.output
    lane_root = ws / "agents" / "atlas" / "lanes" / "demo"
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


def test_add_lane_skips_untracked_bytecode_in_every_repo(tmp_path: Path, monkeypatch) -> None:
    """The lane path shares stage_files, and each repo names its own skip."""
    ws, lane_root = _entered_lane(tmp_path)
    for repo in ("alpha", "beta"):
        (lane_root / "repos" / repo / "__pycache__").mkdir()
        (lane_root / "repos" / repo / "__pycache__" / "edit.cpython-313.pyc").write_bytes(b"\x00bytecode")
    monkeypatch.chdir(ws)
    res = runner.invoke(gr2_app.app, ["add", "--lane", "."])
    assert res.exit_code == 0, res.output
    assert _staged(lane_root / "repos" / "alpha") == ["edit.txt"] and _staged(lane_root / "repos" / "beta") == ["edit.txt"]
    for repo in ("alpha", "beta"):
        assert f"{repo}: Skipped 1 untracked bytecode path(s)" in res.output, res.output


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
    from gr2.python_cli import grip_cli
    with (ws / ".gitinclude").open("a") as declaration:
        declaration.write("stray.txt\n")
    grip_cli._regenerate_workspace_gitignore(ws)
    (ws / "stray.txt").write_text("on purpose\n")
    monkeypatch.chdir(ws)
    res = runner.invoke(gr2_app.app, ["add", "--repo-path", ".", "stray.txt"])
    assert res.exit_code == 0, res.output
    assert "stray.txt" in _staged(ws)


def test_add_dot_at_the_root_with_no_lane_entered_is_what_it_always_was(tmp_path: Path, monkeypatch) -> None:
    ws, _ = _native_workspace(tmp_path, ["alpha"])
    from gr2.python_cli import grip_cli
    with (ws / ".gitinclude").open("a") as declaration:
        declaration.write("stray.txt\n")
    grip_cli._regenerate_workspace_gitignore(ws)
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


# --- store-init roots: build from members, roll back a spec on refusal ------------


def _store_init_only_root(two_member_ws: Path, *, commit: bool = True) -> Path:
    """Initialize a native root and normally record C, retaining no workspace spec."""
    from tests.test_store_break_attempts import _cli as store_cli

    assert store_cli("store", "init", str(two_member_ws))[0] == 0
    if commit:
        result = store_cli("store", "commit", "--message", "workspace fixture")
        assert result[0] == 0, result[1]
    assert not (two_member_ws / ".grip" / "workspace_spec.toml").exists()
    return two_member_ws


def test_bare_lane_create_on_a_store_init_root_builds_the_lane_from_the_grip_toml_members(two_member_ws: Path) -> None:
    """The smallest working proof of the first-release must: a root made by `store init` holds grip.toml and no
    workspace spec, and a bare `lane create` makes the lane over its members from the spec derived from grip.toml,
    writes no spec file (grip.toml stays the one record), and the lane holds one clone per member on the lane's
    branch. Goes red if the lane is not built over the members, or if a spec file is written."""
    root = _store_init_only_root(two_member_ws)
    res = runner.invoke(gr2_app.app, ["lane", "create", str(root), "default", "demo"])
    assert res.exit_code == 0, res.output
    text = _flat(res)
    assert "workspace spec written" not in text
    assert "gr2: repos=alpha,beta" in text and "gr2: branch=demo" in text
    assert not (root / ".grip" / "workspace_spec.toml").exists()
    for repo in ("alpha", "beta"):
        clone = root / "agents" / "default" / "lanes" / "demo" / repo
        assert clone.is_dir(), sorted(str(p.relative_to(root)) for p in root.rglob("repos/*"))
        head = subprocess.run(["git", "-C", str(clone), "branch", "--show-current"], capture_output=True, text=True).stdout.strip()
        assert head == "demo", (repo, head)



def _tree_bytes(root: Path) -> dict[str, bytes | None]:
    return {str(p.relative_to(root)): p.read_bytes() if p.is_file() else None
            for p in root.rglob("*")}


def test_native_member_subset_refuses_before_spec_bootstrap(two_member_ws: Path) -> None:
    """A native lane requires the complete selected set before spec bootstrap."""
    root = _store_init_only_root(two_member_ws)
    before = _tree_bytes(root)
    res = runner.invoke(gr2_app.app, ["lane", "create", str(root), "default", "demo", "--repos", "alpha,nosuch"])
    assert res.exit_code == 2, res.output
    assert "complete selected member set" in _flat(res)
    assert not (root / ".grip" / "workspace_spec.toml").exists()
    assert _tree_bytes(root) == before


def test_refused_owner_rolls_back_the_spec_and_a_valid_retry_works(two_member_ws: Path) -> None:
    """A rejected owner must not poison the spec's only unit for the next call."""
    root = _store_init_only_root(two_member_ws)
    before = _tree_bytes(root)
    res = runner.invoke(gr2_app.app, ["lane", "create", str(root), "../evil", "demo"])
    assert res.exit_code == 1, res.output
    assert "invalid owner_unit" in res.output
    assert not (root / ".grip" / "workspace_spec.toml").exists()
    assert _tree_bytes(root) == before
    retry = runner.invoke(gr2_app.app, ["lane", "create", str(root), "default", "demo"])
    assert retry.exit_code == 0, retry.output
    assert (root / ".grip" / "state" / "lanes" / "default" / "demo" / "lane.toml").is_file()


def test_lane_create_on_a_root_with_neither_spec_nor_members_still_refuses_and_writes_nothing(tmp_path: Path) -> None:
    """Nothing to build a lane from: the one-sentence refusal stays, and not a byte is written."""
    root = tmp_path / "empty"
    root.mkdir()
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    res = runner.invoke(gr2_app.app, ["lane", "create", str(root), "default", "demo"])
    assert res.exit_code == 2, res.output
    assert "has no workspace spec" in _flat(res) and "Nothing was created" in _flat(res)
    assert sorted(str(p.relative_to(root)) for p in root.rglob("*")) == before


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


def test_native_default_announces_selected_commit_and_checks_its_remote(tmp_path):
    ws, _ = _native_workspace(tmp_path, ["alpha", "beta"])
    _push_branch(ws, "beta", "demo")
    # An ambient document cannot hide a selected member from the collision check.
    (ws / ".grip" / "workspace_spec.toml").write_text(
        'schema_version=1\n[[repos]]\nname="ambient"\npath="wrong/path"\nurl="https://example.invalid/ambient.git"\n[[units]]\nname="atlas"\npath="agents/atlas/home"\nrepos=["ambient"]\n'
    )
    refused = _create(ws)
    assert refused.exit_code == 2 and "already exists on beta's remote" in _flat(refused)
    assert not lanes.lane_file(ws, "atlas", "demo").exists()
    matched = _create(ws, "--branch", "explicit")
    assert matched.exit_code == 0, matched.output
    assert "every repo of the selected workspace commit; this makes 2 clones" in _flat(matched)
    assert set(lanes.load_lane_doc(ws, "atlas", "demo")["repos"]) == {"alpha", "beta"}


def test_native_lane_requires_a_committed_workspace_before_materializing(two_member_ws):
    root = _store_init_only_root(two_member_ws, commit=False)
    before = _tree_bytes(root)
    refused = runner.invoke(gr2_app.app, ["lane", "create", str(root), "default", "demo"])
    assert refused.exit_code == 2, refused.output
    assert "store commit" in _flat(refused)
    assert not (root / ".grip" / "workspace_spec.toml").exists()
    assert not lanes.lane_file(root, "default", "demo").exists()
    assert _tree_bytes(root) == before


def _unpushed_fresh(ws: Path, name: str) -> str:
    """The author's own local `fresh` in the source, one commit the remote does not have; returns that commit."""
    src = ws / "repos" / name
    _git(src, "checkout", "-q", "-b", "fresh")
    (src / "unpushed.txt").write_text("not on the remote yet\n")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "unpushed work")
    local = _git(src, "rev-parse", "HEAD")
    _git(src, "checkout", "-q", "main")
    return local


def test_a_local_branch_new_to_the_remote_keeps_its_unpushed_work_and_the_doc_says_local(tmp_path: Path) -> None:
    """A spec root: the author's local `fresh` holds one unpushed commit, and the remote's main moved on. The
    branch is new to the remote, but the lane must start at the local commit, not the live tip, so no unpushed work
    is dropped, and the doc says `local`. Goes red if the local start is dropped (the lane lands on the live tip)."""
    ws, _ = _workspace(tmp_path, ["beta"])
    local = _unpushed_fresh(ws, "beta")
    live = _move_main(ws, "beta")
    res = _create(ws, "--branch", "fresh")
    assert res.exit_code == 0, res.output
    assert _git(_lane_repo(ws, "beta"), "rev-parse", "HEAD") == local != live
    assert lanes.load_lane_doc(ws, "atlas", "demo")["fork_base"]["beta"] == {"branch": "fresh", "sha": local, "start": "local"}
    assert f"gr2: beta: fresh local {local}" in _flat(res)


def test_at_pin_never_starts_a_local_branch_with_unpushed_work_at_the_pin(tmp_path: Path) -> None:
    """--at-pin beside the author's unpushed local `fresh` on a spec root: the local work outranks the pin, so the lane
    starts at the local commit and the doc says `local`. Goes red if the pin is chosen ahead of the local work."""
    ws, _ = _workspace(tmp_path, ["beta"])
    local = _unpushed_fresh(ws, "beta")
    _move_main(ws, "beta")
    res = _create(ws, "--branch", "fresh", "--at-pin")
    assert res.exit_code == 0, res.output
    assert _git(_lane_repo(ws, "beta"), "rev-parse", "HEAD") == local
    assert lanes.load_lane_doc(ws, "atlas", "demo")["fork_base"]["beta"] == {"branch": "fresh", "sha": local, "start": "local"}
    assert f"gr2: beta: fresh local {local}" in _flat(res)


def test_a_local_branch_with_no_unpushed_work_starts_at_the_live_tip_and_the_doc_says_tip(tmp_path: Path) -> None:
    """A spec root: the author's local `fresh` sits at the pin with nothing the remote lacks, and the remote's main
    moved on. There is no work to keep, so the lane starts at the live tip and the doc says `tip`, not `local`.
    Goes red if a local branch counts as work only because it exists (the lane lands on the pin)."""
    ws, _ = _workspace(tmp_path, ["beta"])
    src = ws / "repos" / "beta"
    _git(src, "branch", "fresh")
    pin = _git(src, "rev-parse", "fresh")
    live = _move_main(ws, "beta")
    res = _create(ws, "--branch", "fresh")
    assert res.exit_code == 0, res.output
    assert _git(_lane_repo(ws, "beta"), "rev-parse", "HEAD") == live != pin
    assert lanes.load_lane_doc(ws, "atlas", "demo")["fork_base"]["beta"] == {"branch": "fresh", "sha": live, "start": "tip"}
    assert f"gr2: beta: fresh tip {live}" in _flat(res)


def test_a_review_type_lane_with_a_stale_local_branch_still_starts_at_the_live_tip(tmp_path: Path) -> None:
    """The sibling of the row above under `--type review`. Only `review checkout-pr` keeps a seed its integration
    ref already contains (the fetched PR head); the lane type does not, so a review-type lane on a stale local
    branch still starts at the live tip. Goes red if keeping the seed is read from the lane type."""
    ws, _ = _workspace(tmp_path, ["beta"])
    src = ws / "repos" / "beta"
    _git(src, "branch", "fresh")
    pin = _git(src, "rev-parse", "fresh")
    live = _move_main(ws, "beta")
    assert subprocess.run(["git", "merge-base", "--is-ancestor", pin, live], cwd=src).returncode == 0
    res = _create(ws, "--type", "review", "--branch", "fresh")
    assert res.exit_code == 0, res.output
    assert lanes.load_lane_doc(ws, "atlas", "demo")["lane_type"] == "review"
    assert _git(_lane_repo(ws, "beta"), "rev-parse", "HEAD") == live != pin
    assert lanes.load_lane_doc(ws, "atlas", "demo")["fork_base"]["beta"] == {"branch": "fresh", "sha": live, "start": "tip"}


def test_a_member_that_declares_a_ref_starts_at_the_live_tip_of_that_ref_not_the_remote_default(tmp_path: Path) -> None:
    """A spec root whose alpha declares `ref = "dev"`; dev is pushed ahead and main moves on too. A branch new to the
    remote starts at the live tip of dev, the member's integration ref, not of the remote's HEAD (main).
    Goes red if the live tip is always read from HEAD (the mutant target-always-HEAD)."""
    ws, tips = _workspace(tmp_path, ["alpha"])
    spec = ws / ".grip" / "workspace_spec.toml"
    url_line = next(line for line in spec.read_text().splitlines() if line.startswith("url") and "/alpha.git" in line)
    spec.write_text(spec.read_text().replace(url_line, f'{url_line}\nref = "dev"'))
    src = ws / "repos" / "alpha"
    _git(src, "checkout", "-q", "-b", "dev")
    (src / "dev.txt").write_text("dev moved on\n")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "dev moved")
    _git(src, "push", "-q", "origin", "dev")
    dev = _git(src, "rev-parse", "HEAD")
    _git(src, "checkout", "-q", "main")
    main = _move_main(ws, "alpha")
    res = _create(ws, "--branch", "fresh")
    assert res.exit_code == 0, res.output
    assert _git(_lane_repo(ws, "alpha"), "rev-parse", "HEAD") == dev != main
    assert dev != tips["alpha"]
    assert lanes.load_lane_doc(ws, "atlas", "demo")["fork_base"]["alpha"] == {"branch": "fresh", "sha": dev, "start": "tip"}
    assert f"gr2: alpha: fresh tip {dev}" in _flat(res)


def test_a_direct_call_without_at_pin_materializes_at_the_live_tip(tmp_path: Path, capsys) -> None:
    """lane_create called from Python, with --at-pin left at its typer default (a truthy OptionInfo): the lane is
    materialized at the live tip, not the pin. Goes red if the `is True` normalisation is dropped."""
    ws, tips = _native_workspace(tmp_path, ["alpha"])
    live = _move_main(ws, "alpha")
    gr2_app.lane_create(ws, "atlas", "demo", repos="alpha", branch="fresh", lane_type="feature", source="manual",
                        command=[], manual_hooks=False, bind=None, workspace_commit=None, root=None, unit=None)
    alpha = next(p for p in lanes.lane_checkout_root(ws, "atlas", "demo").rglob("alpha") if (p / ".git").exists())
    assert _git(alpha, "rev-parse", "HEAD") == live != tips["alpha"]
    assert lanes.load_lane_doc(ws, "atlas", "demo")["fork_base"]["alpha"] == {"branch": "fresh", "sha": live, "start": "tip"}
    assert f"gr2: alpha: fresh tip {live}" in capsys.readouterr().err


def _declare_ref(ws: Path, ref: str) -> None:
    spec = ws / ".grip" / "workspace_spec.toml"
    url_line = next(line for line in spec.read_text().splitlines() if line.startswith("url") and "/alpha.git" in line)
    spec.write_text(spec.read_text().replace(url_line, f'{url_line}\nref = "{ref}"'))


def test_a_remote_branch_whose_name_ends_in_the_target_does_not_answer_for_it(tmp_path: Path) -> None:
    """ls-remote matches by tail: refs/heads/a/refs/heads/main answers a query for refs/heads/main and sorts first.
    The lane starts at the live main, not at that branch. Goes red if the first ls-remote line is taken as the tip."""
    ws, tips = _workspace(tmp_path, ["alpha"])
    _declare_ref(ws, "main")
    _git(ws / "repos" / "alpha", "push", "-q", "origin", f"{tips['alpha']}:refs/heads/a/refs/heads/main")
    live = _move_main(ws, "alpha")
    res = _create(ws, "--branch", "fresh")
    assert res.exit_code == 0, res.output
    assert _git(_lane_repo(ws, "alpha"), "rev-parse", "HEAD") == live != tips["alpha"]
    assert lanes.load_lane_doc(ws, "atlas", "demo")["fork_base"]["alpha"] == {"branch": "fresh", "sha": live, "start": "tip"}


def test_a_target_the_remote_holds_only_as_a_tag_says_no_branch_not_unreadable(tmp_path: Path) -> None:
    """The remote answered; it has a tag v1 and no branch v1. The line says so, not that the tip could not be read.
    Goes red if an empty answer is worded as an unreachable remote."""
    ws, _ = _workspace(tmp_path, ["alpha"])
    _declare_ref(ws, "v1")
    src = ws / "repos" / "alpha"
    _git(src, "tag", "v1")
    _git(src, "push", "-q", "origin", "v1")
    res = _create(ws, "--branch", "fresh")
    assert res.exit_code == 0, res.output
    assert "gr2: alpha: the remote has no branch origin/v1; fresh starts at the pin" in _flat(res)
    assert "cannot read the live tip" not in _flat(res)
    assert lanes.load_lane_doc(ws, "atlas", "demo")["fork_base"]["alpha"]["start"] == "pin"


def test_a_live_tip_the_clone_cannot_fetch_refuses_instead_of_recording_a_start_it_did_not_take(tmp_path: Path, monkeypatch) -> None:
    """Main moves on the remote at the ls-remote and the fetch of that tip fails: the checkout cannot start there, so
    lane create refuses by name and no fork base says tip. Goes red if the failed checkout is ignored (the lane would
    sit at the pin while the doc records the live tip)."""
    ws, _ = _native_workspace(tmp_path, ["alpha"])
    seen = _git_moving_main_at_ls_remote(monkeypatch, ws, "alpha")
    moving = gr2_app.git

    def git(repo, *args, **kw):
        if args[:1] == ("fetch",) and "live" in seen:
            return subprocess.CompletedProcess(["git", *args], 128, "", "fatal: could not read from remote")
        return moving(repo, *args, **kw)

    monkeypatch.setattr(gr2_app, "git", git)
    res = _create(ws, "--branch", "fresh")
    assert res.exit_code != 0
    assert "cannot start fresh at the live tip of origin/main in alpha" in _flat(res)
    doc = lanes.load_lane_doc(ws, "atlas", "demo") if (ws / ".grip" / "state" / "lanes" / "atlas" / "demo" / "lane.toml").exists() else {}
    assert doc.get("fork_base", {}).get("alpha", {}).get("start") != "tip"


def test_a_failed_ls_remote_is_not_read_for_a_tip_even_when_it_printed_one(tmp_path: Path, monkeypatch) -> None:
    """ls-remote exits non-zero yet its stdout carries a row for the target: a failed answer is not a tip, so the
    branch starts at the pin and the line says the tip could not be read. Goes red if stdout is read without rc 0."""
    ws, tips = _native_workspace(tmp_path, ["alpha"])
    live = _move_main(ws, "alpha")
    real = gr2_app.git

    def git(repo, *args, **kw):
        if args[:1] == ("ls-remote",):
            return subprocess.CompletedProcess(["git", *args], 128, f"{live}\trefs/heads/main\n", "fatal: early EOF")
        return real(repo, *args, **kw)

    monkeypatch.setattr(gr2_app, "git", git)
    _declare_ref(ws, "main")
    res = _create(ws, "--branch", "fresh")
    assert res.exit_code == 0, res.output
    assert _git(_lane_repo(ws, "alpha"), "rev-parse", "HEAD") == tips["alpha"] != live
    assert "gr2: alpha: cannot read the live tip of origin/main; fresh starts at the pin" in _flat(res)
    assert lanes.load_lane_doc(ws, "atlas", "demo")["fork_base"]["alpha"]["start"] == "pin"
