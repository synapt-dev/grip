"""`gr2 branch` inside a lane repo: the lane's branch is lane state, so a branch verb that moves one
member off it refuses and names the way out (a new lane).

Before this, `gr2 branch other` inside a lane member switched that repo and left the lane doc's
`branch_map` (and its recorded fork base) on the old branch: `lane push` then pushed `other` and
exited 3 against the lane's branch, and `pr create` opened from the stale branch. Updating the doc
from a single-repo verb would leave the other members and the recorded base describing a branch
this repo no longer carries, so the verb refuses instead; switching to the lane's own branch, and
any repo outside a lane, are untouched.
"""
from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from gr2.python_cli import app as gr2_app
from tests.test_lane_create_fork_base_cli import _commit_native_workspace, _source_repo
from tests.test_lane_push import BRANCH, REPOS, _commit, _git, _lane, _push, _remote_sha, runner


def _branch(repo: Path, *a: str):
    return runner.invoke(gr2_app.app, ["branch", *a, "--repo-path", str(repo)])


def test_branch_in_a_lane_member_refuses_and_lane_push_still_lands(tmp_path: Path) -> None:
    ws, repos = _lane(tmp_path)
    alpha = repos["alpha"]
    moved = _branch(alpha, "other")
    assert moved.exit_code == 2, moved.output
    assert "new lane" in moved.output, moved.output
    assert BRANCH in moved.output, moved.output
    assert _git(alpha, "branch", "--show-current") == BRANCH
    assert _git(alpha, "branch", "--list", "other") == ""
    pushed = _push(ws)
    assert pushed.exit_code == 0, pushed.output
    assert _remote_sha(tmp_path, "alpha") == _git(alpha, "rev-parse", "HEAD")


def test_branch_from_below_a_lane_member_refuses(tmp_path: Path, monkeypatch) -> None:
    ws, repos = _lane(tmp_path)
    sub = repos["beta"] / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    moved = runner.invoke(gr2_app.app, ["branch", "other"])
    assert moved.exit_code == 2, moved.output
    assert _git(repos["beta"], "branch", "--show-current") == BRANCH


def test_switching_back_to_the_lanes_own_branch_is_allowed(tmp_path: Path) -> None:
    ws, repos = _lane(tmp_path)
    gamma = repos["gamma"]
    _git(gamma, "checkout", "-q", "-b", "stray")
    back = _branch(gamma, BRANCH)
    assert back.exit_code == 0, back.output
    assert _git(gamma, "branch", "--show-current") == BRANCH


def test_a_repo_outside_any_lane_still_branches(tmp_path: Path) -> None:
    ws, _repos = _lane(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    _git(plain, "init", "-q")
    _git(plain, "config", "user.email", "t@e.invalid")
    _git(plain, "config", "user.name", "t")
    _commit(plain, "a.txt", "a\n")
    made = _branch(plain, "other")
    assert made.exit_code == 0, made.output
    assert _git(plain, "branch", "--show-current") == "other"


def test_a_repo_inside_the_workspace_but_in_no_lane_still_branches(tmp_path: Path) -> None:
    ws, _repos = _lane(tmp_path)
    plain = ws / "plain"
    plain.mkdir()
    _git(plain, "init", "-q")
    _git(plain, "config", "user.email", "t@e.invalid")
    _git(plain, "config", "user.name", "t")
    _commit(plain, "a.txt", "a\n")
    made = _branch(plain, "other")
    assert made.exit_code == 0, made.output
    assert _git(plain, "branch", "--show-current") == "other"


def test_an_unreadable_lane_doc_beside_does_not_hide_the_member(tmp_path: Path) -> None:
    from gr2.prototypes import lane_workspace_prototype as lanes

    ws, repos = _lane(tmp_path)
    broken = lanes.lane_file(ws, "atlas", "aaa")  # sorts before feature in the same unit: read first
    broken.parent.mkdir(parents=True)
    broken.write_text("this is = not [ toml\n")
    moved = _branch(repos["alpha"], "other")
    assert moved.exit_code == 2, moved.output
    assert _git(repos["alpha"], "branch", "--show-current") == BRANCH


# Lane docs that parse as TOML but carry the wrong types. Each is skipped as naming no member: a repo in no
# lane still branches beside it, and a real member is still found through the lanes after it.
MALFORMED = {
    "repos-int": "repos = 5\n",
    "repos-int-list": "repos = [1]\n",
    "bound-worktree-int": 'lane_kind = "bound"\nrepos = ["solo"]\nbound_worktree = 5\n',
    "checkout-root-int": 'repos = ["alpha"]\ncheckout_root = 5\n',
    "checkout-root-absolute": 'repos = ["alpha"]\ncheckout_root = "/elsewhere"\n',
    "bound-worktree-nul": 'lane_kind = "bound"\nrepos = ["solo"]\nbound_worktree = "a\\u0000b"\n',
    # load_lane_doc reads branch_map with dict(): a string or a list of non-pairs does not load.
    "branch-map-string": 'repos = ["alpha"]\nbranch_map = "x"\n',
    "branch-map-list": 'repos = ["alpha"]\nbranch_map = [1]\n',
}


def _plain(root: Path) -> Path:
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@e.invalid")
    _git(root, "config", "user.name", "t")
    _commit(root, "a.txt", "a\n")
    return root


def _doc_beside(ws: Path, lane: str, text: str) -> None:
    from gr2.prototypes import lane_workspace_prototype as lanes

    # Units come from an unsorted iterdir(); lanes inside a unit are sorted. Beside the member's lane in its own
    # unit, "aaa" sorts before "feature" and is read first.
    path = lanes.lane_file(ws, "atlas", lane)
    path.parent.mkdir(parents=True)
    path.write_text(text)


@pytest.mark.parametrize("text", MALFORMED.values(), ids=MALFORMED.keys())
def test_a_no_lane_repo_still_branches_beside_a_malformed_lane_doc(tmp_path: Path, text: str) -> None:
    ws, _repos = _lane(tmp_path)
    _doc_beside(ws, "aaa", text)
    plain = _plain(ws / "plain")
    made = _branch(plain, "other")
    assert made.exit_code == 0, made.output
    assert _git(plain, "branch", "--show-current") == "other"


@pytest.mark.parametrize("text", MALFORMED.values(), ids=MALFORMED.keys())
def test_a_member_is_still_refused_beside_a_malformed_lane_doc(tmp_path: Path, text: str) -> None:
    ws, repos = _lane(tmp_path)
    _doc_beside(ws, "aaa", text)
    moved = _branch(repos["alpha"], "other")
    assert moved.exit_code == 2, moved.output
    assert _git(repos["alpha"], "branch", "--show-current") == BRANCH


def _bound_doc(ws: Path, repo: Path, extra: str, repos: str = '["solo"]') -> None:
    _doc_beside(ws, "solo", f'lane_kind = "bound"\nrepos = {repos}\nbound_worktree = "{repo}"\n{extra}')


@pytest.mark.parametrize(("extra", "lane_branch"), [("", "solo"), ('branch_map = [["solo", "feat/solo"]]\n', "feat/solo")],
                         ids=["branch-map-absent", "branch-map-pairs"])
def test_a_bound_members_branch_is_read_as_the_lane_doc_load_reads_it(tmp_path: Path, extra: str, lane_branch: str) -> None:
    # Rule: the lane's branch for a member is branch_map read as load_lane_doc reads it (dict()), so the verb names the
    # branch the load checked; absent, it is the lane's name, as `lane create` writes it. Another name is refused.
    ws, _repos = _lane(tmp_path)
    repo = _plain(ws / "solo-repo")
    _bound_doc(ws, repo, extra)
    moved = _branch(repo, "other")
    assert moved.exit_code == 2, moved.output
    assert f"'{lane_branch}'" in moved.output, moved.output
    assert _git(repo, "branch", "--list", "other") == ""
    back = _branch(repo, lane_branch)
    assert back.exit_code == 0, back.output
    assert _git(repo, "branch", "--show-current") == lane_branch


UNLOADABLE_BRANCH_MAP = {
    "string": 'branch_map = "x"\n',  # ValueError
    "list": "branch_map = [1]\n",  # TypeError
    "refused-name": 'branch_map = { solo = "-x" }\n',  # SystemExit
    "range-name": 'branch_map = { solo = "a..b" }\n',  # SystemExit
    "refused-name-on-another-key": 'branch_map = { solo = "feat/solo", ghost = "-x" }\n',  # SystemExit
}


@pytest.mark.parametrize("extra", UNLOADABLE_BRANCH_MAP.values(), ids=UNLOADABLE_BRANCH_MAP.keys())
def test_a_bound_repo_whose_branch_map_does_not_load_is_refused(tmp_path: Path, extra: str) -> None:
    # Rule: a lane doc the load refuses still binds its repo; the repo is refused until the branch_map is fixed,
    # and no lane create is offered, since the lane names no branch.
    ws, _repos = _lane(tmp_path)
    repo = _plain(ws / "solo-repo")
    before = _git(repo, "branch", "--show-current")
    _bound_doc(ws, repo, extra)
    moved = _branch(repo, "other")
    assert moved.exit_code == 2, moved.output
    assert _git(repo, "branch", "--show-current") == before
    assert _git(repo, "branch", "--list", "other") == ""
    assert "atlas/solo" in moved.output and "branch_map" in moved.output, moved.output
    assert "lane create" not in moved.output, moved.output


def test_a_bound_member_whose_repo_key_is_not_a_string_is_still_refused(tmp_path: Path) -> None:
    ws, _repos = _lane(tmp_path)
    repo = _plain(ws / "solo-repo")
    _bound_doc(ws, repo, 'branch_map = { solo = "feat/solo" }\n', repos="[[1]]")
    moved = _branch(repo, "other")
    assert moved.exit_code == 2, moved.output
    assert _git(repo, "branch", "--list", "other") == ""


def _run_the_suggested_lane_create(ws: Path, refusal: str):
    """The command the refusal names, with its two placeholders filled, run as printed."""
    printed = refusal.split("new lane: ", 1)[1].strip()
    argv = shlex.split(printed.replace("<workspace>", str(ws)).replace("<lane>", "next"))
    assert argv[:3] == ["gr2", "lane", "create"], printed
    return printed, runner.invoke(gr2_app.app, argv[1:])


def test_the_suggested_lane_create_runs_in_a_native_workspace(tmp_path: Path) -> None:
    # A native workspace lane materializes every member of the selected workspace commit: `--repos beta` alone
    # exits 2 there, so the refusal names no --repos and lane create takes them all.
    ws, repos = _lane(tmp_path)
    moved = _branch(repos["beta"], "other")
    assert moved.exit_code == 2, moved.output
    printed, made = _run_the_suggested_lane_create(ws, moved.output)
    assert "--repos" not in printed, printed
    assert made.exit_code == 0, (printed, made.output)
    assert _git(lanes_root(ws, "next", "beta"), "branch", "--show-current") == "other"


def test_the_suggested_lane_create_runs_after_the_store_grows_a_repo(tmp_path: Path) -> None:
    # The member set lane create checks is the workspace commit's, not the lane's: a repo added to the store
    # after the lane was made must not turn the printed command into an exit 2.
    ws, repos = _lane(tmp_path)
    url, _tip = _source_repo(tmp_path, ws, "delta")
    spec = ws / ".grip" / "workspace_spec.toml"
    text = spec.read_text().replace('repos = ["alpha", "beta", "gamma"]', 'repos = ["alpha", "beta", "gamma", "delta"]')
    spec.write_text(text + f'\n[[repos]]\nname = "delta"\npath = "repos/delta"\nurl = "{url}"\n')
    _commit_native_workspace(ws)
    moved = _branch(repos["beta"], "other")
    assert moved.exit_code == 2, moved.output
    printed, made = _run_the_suggested_lane_create(ws, moved.output)
    assert made.exit_code == 0, (printed, made.output)
    assert _git(lanes_root(ws, "next", "beta"), "branch", "--show-current") == "other"
    assert lanes_root(ws, "next", "delta").is_dir(), made.output


def _spec_lane(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    """The same three-repo lane in a spec workspace (no native root): there a lane may take any subset."""
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    urls = {r: _source_repo(tmp_path, ws, r)[0] for r in REPOS}
    blocks = "".join(f'\n[[repos]]\nname = "{r}"\npath = "repos/{r}"\nurl = "{urls[r]}"\n' for r in REPOS)
    (ws / ".grip" / "workspace_spec.toml").write_text(
        f'schema_version = 1\nworkspace_name = "m"\n{blocks}\n'
        f'[[units]]\nname = "atlas"\npath = "agents/atlas"\nrepos = ["alpha", "beta", "gamma"]\n'
    )
    made = runner.invoke(gr2_app.app, ["lane", "create", str(ws), "atlas", "feature",
                                       "--repos", ",".join(REPOS), "--branch", BRANCH])
    assert made.exit_code == 0, made.output
    return ws, {r: lanes_root(ws, "feature", r) for r in REPOS}


def test_the_suggested_lane_create_runs_in_a_spec_workspace_with_the_one_repo(tmp_path: Path) -> None:
    ws, repos = _spec_lane(tmp_path)
    moved = _branch(repos["beta"], "other")
    assert moved.exit_code == 2, moved.output
    printed, made = _run_the_suggested_lane_create(ws, moved.output)
    assert "--repos beta " in printed, printed
    assert made.exit_code == 0, (printed, made.output)
    assert _git(lanes_root(ws, "next", "beta"), "branch", "--show-current") == "other"


def lanes_root(ws: Path, lane: str, repo: str) -> Path:
    from gr2.prototypes import lane_workspace_prototype as lanes

    return lanes.lane_repo_root(ws, "atlas", lane, repo)


def _set_branch_map(ws: Path, lane: str, extra: str) -> None:
    """Replace the lane doc's [branch_map] table with `extra`, a top-level branch_map the load refuses."""
    from gr2.prototypes import lane_workspace_prototype as lanes

    path = lanes.lane_file(ws, "atlas", lane)
    text = path.read_text()
    start = text.index("[branch_map]")
    end = text.index("\n[", start) + 1
    text = text[:start] + text[end:]
    head = text.index("\n[") + 1
    path.write_text(text[:head] + extra + text[head:])


UNLOADABLE_MATERIALIZED = {"string": 'branch_map = "x"\n', "refused-name": 'branch_map = { alpha = "-x" }\n'}
WORKSPACES = {"native": _lane, "legacy-spec": _spec_lane}


@pytest.mark.parametrize("make", WORKSPACES.values(), ids=WORKSPACES.keys())
@pytest.mark.parametrize("extra", UNLOADABLE_MATERIALIZED.values(), ids=UNLOADABLE_MATERIALIZED.keys())
def test_a_materialized_repo_whose_branch_map_does_not_load_is_refused(tmp_path: Path, make, extra: str) -> None:
    # Rule: as for a bound lane, a materialized lane whose doc the load refuses still holds its checkouts; membership
    # comes from the raw doc's paths, since every lane path helper re-loads the doc.
    ws, repos = make(tmp_path)
    _set_branch_map(ws, "feature", extra)
    moved = _branch(repos["alpha"], "other")
    assert moved.exit_code == 2, moved.output
    assert _git(repos["alpha"], "branch", "--show-current") == BRANCH
    assert _git(repos["alpha"], "branch", "--list", "other") == ""
    assert "atlas/feature" in moved.output and "branch_map" in moved.output, moved.output
    assert "lane create" not in moved.output, moved.output


@pytest.mark.parametrize("make", WORKSPACES.values(), ids=WORKSPACES.keys())
def test_a_repo_outside_an_unloadable_lane_still_branches(tmp_path: Path, make) -> None:
    # Rule: a lane doc the load refuses holds only the repos its raw paths reach; a bound lane on another worktree
    # and a materialized lane elsewhere in the workspace leave a repo in no lane free to branch.
    ws, _repos = make(tmp_path)
    _set_branch_map(ws, "feature", 'branch_map = "x"\n')
    other = _plain(ws / "solo-repo")
    _bound_doc(ws, other, 'branch_map = "x"\n')
    plain = _plain(ws / "plain")
    made = _branch(plain, "other")
    assert made.exit_code == 0, made.output
    assert _git(plain, "branch", "--show-current") == "other"


def _plain_at(root: Path) -> Path:
    root.parent.mkdir(parents=True, exist_ok=True)
    return _plain(root)


@pytest.mark.parametrize("make", WORKSPACES.values(), ids=WORKSPACES.keys())
def test_a_repo_beside_an_unloadable_lanes_roots_with_a_shared_prefix_still_branches(tmp_path: Path, make) -> None:
    # Rule: a lane root holds the paths under it, compared by path parts: <root>2/r shares the root's spelling as
    # a string prefix but sits in no lane.
    from gr2.prototypes import lane_workspace_prototype as lanes

    ws, _repos = make(tmp_path)
    checkout_root = lanes.lane_checkout_root(ws, "atlas", "feature").resolve()
    lane_dir = lanes.lane_dir(ws, "atlas", "feature").resolve()
    _set_branch_map(ws, "feature", 'branch_map = "x"\n')
    for beside in (Path(f"{checkout_root}2") / "r", Path(f"{lane_dir}2") / "r"):
        plain = _plain_at(beside)
        made = _branch(plain, "other")
        assert made.exit_code == 0, (beside, made.output)
        assert _git(plain, "branch", "--show-current") == "other"


@pytest.mark.parametrize("make", WORKSPACES.values(), ids=WORKSPACES.keys())
def test_a_repo_under_an_unloadable_lanes_dir_is_refused(tmp_path: Path, make) -> None:
    # Rule: a materialized lane holds its lane dir as well as its checkout_root.
    from gr2.prototypes import lane_workspace_prototype as lanes

    ws, _repos = make(tmp_path)
    _set_branch_map(ws, "feature", 'branch_map = "x"\n')
    held = _plain(lanes.lane_dir(ws, "atlas", "feature") / "extra")
    before = _git(held, "branch", "--show-current")
    moved = _branch(held, "other")
    assert moved.exit_code == 2, moved.output
    assert _git(held, "branch", "--show-current") == before
    assert _git(held, "branch", "--list", "other") == ""
    assert "atlas/feature" in moved.output, moved.output


def test_an_unloadable_bound_lane_with_a_relative_worktree_holds_nothing(tmp_path: Path, monkeypatch) -> None:
    # Rule: a bound lane's raw bound_worktree holds a repo only as an absolute path; a relative one is not read
    # against the cwd.
    ws, _repos = _lane(tmp_path)
    repo = _plain(ws / "solo-repo")
    _doc_beside(ws, "solo", 'lane_kind = "bound"\nrepos = ["solo"]\nbound_worktree = "solo-repo"\nbranch_map = "x"\n')
    monkeypatch.chdir(ws)
    made = _branch(repo, "other")
    assert made.exit_code == 0, made.output
    assert _git(repo, "branch", "--show-current") == "other"


def test_an_unloadable_bound_lane_with_a_nul_in_its_worktree_holds_nothing(tmp_path: Path) -> None:
    ws, _repos = _lane(tmp_path)
    repo = _plain(ws / "solo-repo")
    _bound_doc(ws, Path(f"{repo}\\u0000"), 'branch_map = "x"\n')
    made = _branch(repo, "other")
    assert made.exit_code == 0, made.output
    assert _git(repo, "branch", "--show-current") == "other"


def test_an_unloadable_bound_lane_without_a_usable_worktree_does_not_hold_its_lane_dir(tmp_path: Path) -> None:
    # Rule: a bound lane holds only its bound worktree; with no usable one it holds nothing, not the
    # materialized roots.
    from gr2.prototypes import lane_workspace_prototype as lanes

    ws, _repos = _lane(tmp_path)
    _doc_beside(ws, "solo", 'lane_kind = "bound"\nrepos = ["solo"]\nbound_worktree = "solo-repo"\nbranch_map = "x"\n')
    repo = _plain(lanes.lane_dir(ws, "atlas", "solo") / "r")
    made = _branch(repo, "other")
    assert made.exit_code == 0, made.output
    assert _git(repo, "branch", "--show-current") == "other"


def test_the_lane_named_in_the_refusal_is_quoted_when_its_directories_hold_shell_text(tmp_path: Path) -> None:
    # Rule: the unit and lane come from the lane doc's directory names, not from the person typing `gr2 branch`,
    # so the printed lane create quotes them: shlex.split gives the unit as one argument, and the line run by a
    # shell (gr2 stubbed) starts nothing the directory names hold.
    from gr2.prototypes import lane_workspace_prototype as lanes
    import subprocess

    ws, _repos = _lane(tmp_path)
    repo = _plain(ws / "solo-repo")
    unit, lane = "u; touch MARKU", "l; touch MARKL"
    path = lanes.lane_file(ws, unit, lane)
    path.parent.mkdir(parents=True)
    path.write_text(f'lane_kind = "bound"\nrepos = ["solo"]\nbound_worktree = "{repo}"\n')
    moved = _branch(repo, "other")
    assert moved.exit_code == 2, moved.output
    assert f"lane {shlex.quote(f'{unit}/{lane}')}," in moved.output, moved.output
    printed = moved.output.split("new lane: ", 1)[1].strip().replace("<workspace>", str(ws)).replace("<lane>", "next")
    argv = shlex.split(printed)
    assert argv[:6] == ["gr2", "lane", "create", str(ws), unit, "next"], printed
    assert argv[-2:] == ["--branch", "other"], printed
    ran = subprocess.run(["bash", "-c", "gr2() { :; }\n" + printed], cwd=tmp_path, capture_output=True, text=True)
    assert ran.returncode == 0, ran.stderr
    assert not (tmp_path / "MARKU").exists() and not (tmp_path / "MARKL").exists(), printed
    assert list(tmp_path.glob("MARK*")) == [], printed


def test_the_member_key_in_the_refusal_is_quoted_when_the_lane_record_holds_shell_text(tmp_path: Path) -> None:
    # Rule: the `--repos` key comes from the lane record, not from the person typing `gr2 branch`, and a plain
    # member key may hold ';' and a space (only separators, NUL and newline are refused), so the printed lane
    # create quotes it: shlex.split gives the key as one argument, and the line run by a shell starts nothing.
    from gr2.prototypes import lane_workspace_prototype as lanes
    import subprocess

    ws = tmp_path / "ws"  # a spec workspace (no native root), where the refusal names --repos
    (ws / ".grip").mkdir(parents=True)
    (ws / ".grip" / "workspace_spec.toml").write_text('schema_version = 1\nworkspace_name = "m"\n')
    repo = _plain(ws / "solo-repo")
    key = "s; touch MARKK #"
    path = lanes.lane_file(ws, "u", "l")
    path.parent.mkdir(parents=True)
    path.write_text(f'lane_kind = "bound"\nrepos = ["{key}"]\nbound_worktree = "{repo}"\n')
    moved = _branch(repo, "other")
    assert moved.exit_code == 2, moved.output
    printed = moved.output.split("new lane: ", 1)[1].strip().replace("<workspace>", str(ws)).replace("<lane>", "next")
    argv = shlex.split(printed)
    assert argv[6:] == ["--repos", key, "--branch", "other"], printed
    ran = subprocess.run(["bash", "-c", "gr2() { :; }\n" + printed], cwd=tmp_path, capture_output=True, text=True)
    assert ran.returncode == 0, ran.stderr
    assert list(tmp_path.glob("MARK*")) == [], printed
