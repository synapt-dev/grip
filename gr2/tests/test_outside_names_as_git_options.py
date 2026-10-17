"""A name read from a committed document never reaches git as an option.

Two readers hand outside text to git as a positional: a store member's `upstream` (its remote part is the
remote for `fetch` and `push`) and a lane's `branch_map` (the branch for `checkout`). Each is refused where the
document loads, and the git call carries `--`, so a value shaped like `--upload-pack=...` or `-f` is never read
as an option. Each row drives the real verb over the real document.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import app as gr2_app
from tests.conftest import make_cli_runner
from tests.test_lane_exit_keeps_dirty_work import _entered_lane
from tests.test_store_break_attempts import _assert_init_ran, _cli, _set_member_upstream, two_member_ws  # noqa: F401


def test_a_store_upstream_shaped_like_an_option_is_refused_before_fetch_runs_it(
    two_member_ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = two_member_ws
    marker = tmp_path / "PWNED"
    monkeypatch.setenv("PWN_MARK", str(marker))
    assert _cli("store", "init")[0] == 0
    _assert_init_ran(root)
    assert _cli("store", "commit", "-m", "first")[0] == 0
    toml_path = root / "grip.toml"
    # No '/' inside the command: upstream is split at its first '/', and the remote part is what reaches git.
    toml_path.write_text(_set_member_upstream(toml_path.read_text(), "alpha", "--upload-pack=touch $PWN_MARK; false/main"))

    for verb in (["store", "check"], ["store", "commit", "-m", "second"]):
        rc, out = _cli(*verb)
        assert not marker.exists(), f"{' '.join(verb)} ran the upload-pack command from grip.toml: rc={rc} {out}"
        assert rc == 4, f"{' '.join(verb)} must refuse the upstream with exit 4, got {rc}: {out}"
        assert "alpha" in out and "plain remote name" in out, out


@pytest.mark.parametrize("remote", ["-x", ".hidden", "a..b", "up;x", "a b"])
def test_other_remote_shapes_that_are_not_plain_names_are_refused(two_member_ws: Path, remote: str) -> None:
    assert _cli("store", "init")[0] == 0
    assert _cli("store", "commit", "-m", "first")[0] == 0
    toml_path = two_member_ws / "grip.toml"
    toml_path.write_text(_set_member_upstream(toml_path.read_text(), "alpha", f"{remote}/main"))
    rc, out = _cli("store", "check")
    assert rc == 4 and "plain remote name" in out, (rc, out)


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", *a], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


@pytest.mark.parametrize("branch", ["-f", "--detach", "a..b", "x:y"])
def test_a_lane_branch_shaped_like_an_option_is_refused_and_uncommitted_work_survives(
    tmp_path: Path, branch: str
) -> None:
    ws, repo = _entered_lane(tmp_path)
    (repo / "f.txt").write_text("uncommitted\n")
    head = _git(repo, "symbolic-ref", "-q", "HEAD")
    from gr2.prototypes import lane_workspace_prototype as lane_proto

    lane_toml = lane_proto.lane_file(ws, "atlas", "x")
    text, n = re.subn(r'(?m)^app\s*=\s*"feat/x"$', f'app = "{branch}"', lane_toml.read_text())
    assert n == 1, lane_toml.read_text()
    lane_toml.write_text(text)

    result = make_cli_runner().invoke(gr2_app.app, ["lane", "enter", str(ws), "atlas", "x", "--actor", "agent:s"])

    assert (repo / "f.txt").read_text() == "uncommitted\n", "lane enter discarded uncommitted work"
    assert _git(repo, "symbolic-ref", "-q", "HEAD") == head, "lane enter moved or detached HEAD"
    assert result.exit_code != 0, result.output
    assert "branch_map" in result.output + (result.stderr or ""), result.output


# The second layer: each git call is safe on its own, for a caller that reaches it without the loader.


def test_the_coverage_fetch_reads_its_remote_after_dashdash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gr2.python_cli import grip_cli

    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    _git(work, "-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit", "-q", "--allow-empty", "-m", "c")
    # With no remote to fall back on, `fetch --upload-pack=...` has nothing to run it against.
    subprocess.run(["git", "init", "-q", "--bare", str(tmp_path / "remote.git")], check=True)
    _git(work, "remote", "add", "origin", str(tmp_path / "remote.git"))
    marker = tmp_path / "PWNED"
    monkeypatch.setenv("PWN_MARK", str(marker))
    monkeypatch.setattr(grip_cli, "_native_member_working_root", lambda root, member, claimed: work)
    member = {"name": "m", "upstream": "--upload-pack=touch $PWN_MARK; false/main"}
    with pytest.raises(grip_cli.NativeStoreRefusal):
        grip_cli._member_coverage(work, member, _git(work, "rev-parse", "HEAD"), None)
    assert not marker.exists(), "fetch read the remote as an option"


def test_the_status_fetch_reads_its_remote_after_dashdash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gr2.python_cli import grip_cli

    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    _git(work, "-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit", "-q", "--allow-empty", "-m", "c")
    subprocess.run(["git", "init", "-q", "--bare", str(tmp_path / "remote.git")], check=True)
    _git(work, "remote", "add", "origin", str(tmp_path / "remote.git"))
    marker = tmp_path / "PWNED"
    monkeypatch.setenv("PWN_MARK", str(marker))
    member = {"name": "m", "path": "m", "pin": _git(work, "rev-parse", "HEAD"),
              "upstream": "--upload-pack=touch $PWN_MARK; false/main"}
    monkeypatch.setattr(grip_cli, "_grip_document", lambda root: {})
    monkeypatch.setattr(grip_cli, "_native_members", lambda root, rev=None: [member])
    monkeypatch.setattr(grip_cli, "_require_root_commit", lambda root: None)
    monkeypatch.setattr(grip_cli, "_claimed_member_paths", lambda *a, **k: set())
    monkeypatch.setattr(grip_cli, "_native_member_working_root", lambda root, member, claimed: work)
    monkeypatch.setattr(grip_cli, "_recorded_pin", lambda *a: None)
    try:
        grip_cli._native_store_status(tmp_path)
    except (grip_cli.NativeStoreRefusal, RuntimeError):  # the root read after the fetch: tmp_path is no repository
        pass
    assert not marker.exists(), "status fetch read the remote as an option"


def test_the_member_push_reads_its_remote_after_dashdash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gr2.python_cli import grip_cli

    member = {"name": "m", "upstream": "-x/main", "remote": "elsewhere", "pin": "0" * 40}
    calls: list[tuple[str, ...]] = []

    def store_git(path, *args, check=True):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1 if args[0] == "push" else 0, f"{tmp_path}\n", "refused")

    monkeypatch.setattr(grip_cli, "_native_members", lambda root, rev=None: [member])
    monkeypatch.setattr(grip_cli, "_claimed_member_paths", lambda *a, **k: set())
    monkeypatch.setattr(grip_cli, "_native_member_working_root", lambda root, member, claimed: tmp_path)
    monkeypatch.setattr(grip_cli, "_store_git", store_git)

    def uncovered(*a, **k):
        raise grip_cli.NativeStoreRefusal("not covered", 3)

    monkeypatch.setattr(grip_cli, "_member_coverage", uncovered)
    with pytest.raises(grip_cli.NativeStoreRefusal):
        grip_cli._push_uncovered_members(tmp_path, "same")
    push = next(args for args in calls if args[0] == "push")
    assert push[push.index("--") + 1] == "-x", push


# "f.txt" is a valid branch name that no branch has: without `--`, checkout reads it as a path and restores the file.
@pytest.mark.parametrize("branch", ["-f", "f.txt"])
def test_checkout_branch_refuses_an_option_and_keeps_uncommitted_work(tmp_path: Path, branch: str) -> None:
    from gr2.python_cli import gitops

    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    (work / "f.txt").write_text("committed\n")
    _git(work, "add", "f.txt")
    _git(work, "-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit", "-q", "-m", "f")
    (work / "f.txt").write_text("uncommitted\n")
    with pytest.raises(SystemExit):
        gitops.checkout_branch(work, branch)
    assert (work / "f.txt").read_text() == "uncommitted\n"


# End to end from a remote: what someone with push to a workspace's store can reach in a teammate's box.


def test_a_hostile_upstream_pushed_to_the_store_runs_nothing_in_a_teammates_clone(tmp_path: Path) -> None:
    from tests.test_store_commit_keeps_record import _root_with_declarations
    from tests.test_store_git_native_smoke import configure_identity, git, gr2

    root, _ = _root_with_declarations(tmp_path)
    # The pusher edits the committed document with plain git; no gr2 verb on their side has to accept it.
    # The command names a relative file, so the witness is "a file appeared anywhere", not a test-only env var.
    hostile = "--upload-pack=touch PWNED; false/main"
    toml_path = root / "grip.toml"
    toml_path.write_text(_set_member_upstream(toml_path.read_text(), "alpha", hostile))
    git(root, "commit", "-q", "-am", "retarget alpha")
    # A hostile pusher skips their own pre-push check, which runs the same store check (and the command, on dev).
    git(root, "push", "-q", "--no-verify", "origin", "HEAD:main")

    teammate = tmp_path / "teammate"
    git(tmp_path, "clone", "-q", str(tmp_path / "root.git"), str(teammate))
    for name in ("alpha", "beta"):
        git(teammate, "clone", "-q", str(tmp_path / f"{name}.git"), name)
        configure_identity(teammate / name)
    configure_identity(teammate)
    assert hostile in (teammate / "grip.toml").read_text(), "precondition: the teammate's clone carries the upstream"

    for verb in (["check"], ["status"], ["push"]):
        result = gr2(teammate, *verb, check=False)
        ran = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("PWNED"))
        assert ran == [], f"gr2 store {verb[0]} in a teammate's clone ran the pushed command: {ran} {result.stderr}"
        assert result.returncode == 4 and "plain remote name" in result.stdout + result.stderr, (verb, result)


def test_a_hostile_member_url_pushed_to_the_store_runs_nothing_at_a_teammates_lane_create(tmp_path: Path) -> None:
    """A member's `origin` url is the spec's `repos[].url`, which lane create hands to `ls-remote` when the
    checkout has no recorded `origin/HEAD` (a clone made before its remote had refs). The pusher sets the url
    with plain git; the teammate's `gr2 lane create` runs nothing and refuses the url by name."""
    import os
    import sys

    from tests.test_store_break_attempts import _set_member_remote
    from tests.test_store_commit_keeps_record import _root_with_declarations
    from tests.test_store_git_native_smoke import configure_identity, git

    root, _ = _root_with_declarations(tmp_path)
    hostile = "--upload-pack=touch PWNED"
    toml_path = root / "grip.toml"
    toml_path.write_text(_set_member_remote(toml_path.read_text(), "alpha", "origin", hostile))
    git(root, "commit", "-q", "-am", "retarget alpha")
    git(root, "push", "-q", "--no-verify", "origin", "HEAD:main")

    teammate = tmp_path / "teammate"
    git(tmp_path, "clone", "-q", str(tmp_path / "root.git"), str(teammate))
    for name in ("alpha", "beta"):
        git(teammate, "clone", "-q", str(tmp_path / f"{name}.git"), name)
        configure_identity(teammate / name)
    configure_identity(teammate)
    git(teammate / "alpha", "remote", "set-head", "origin", "-d")
    assert hostile in (teammate / "grip.toml").read_text(), "precondition: the teammate's clone carries the url"

    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])}
    result = subprocess.run(
        [sys.executable, "-m", "gr2.python_cli.app", "lane", "create", str(teammate), "default", "x", "--branch", "feat/x"],
        cwd=teammate, text=True, capture_output=True, env=env,
    )
    ran = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("PWNED"))
    assert ran == [], f"gr2 lane create in a teammate's clone ran the pushed command: {ran} {result.stderr}"
    assert result.returncode != 0 and "starts with '-'" in result.stdout + result.stderr, result


def test_a_spec_url_shaped_like_an_option_is_refused_where_the_spec_loads(tmp_path: Path) -> None:
    from gr2.python_cli.spec_apply import load_workspace_spec_doc
    from tests.test_lane_create_refuses_own_branch import _workspace

    ws = _workspace(tmp_path, url="--upload-pack=touch PWNED")
    with pytest.raises(SystemExit, match="starts with '-'"):
        load_workspace_spec_doc(ws)


def test_the_origin_default_lookup_reads_its_url_after_dashdash(tmp_path: Path) -> None:
    from gr2.prototypes import lane_workspace_prototype as lane_proto
    from tests.test_lane_create_refuses_own_branch import _workspace

    ws = _workspace(tmp_path, origin_head=False)
    marker = tmp_path / "PWNED"
    assert lane_proto._origin_default_branch(ws / "repos" / "repo-a", f"--upload-pack=touch {marker}") is None
    assert not marker.exists(), "ls-remote read the url as --upload-pack and ran the command"


def test_the_repo_cache_clone_reads_its_url_after_dashdash(tmp_path: Path) -> None:
    from gr2.python_cli import gitops

    url = "--upload-pack=touch PWNED"
    with pytest.raises(SystemExit) as exc:
        gitops.ensure_repo_cache(url, tmp_path / "cache" / "a.git")
    # Read as the repository, git names the url; read as an option, it names the cache path as the repository.
    assert f"'{url}'" in str(exc.value), str(exc.value)


@pytest.mark.parametrize("upstream", ["-x/main", "--git-dir/main"])
def test_status_refuses_an_upstream_whose_remote_part_is_an_option(two_member_ws: Path, upstream: str) -> None:
    # status hands the whole upstream to merge-base and rev-parse (grip_cli status); the load refuses it first.
    assert _cli("store", "init")[0] == 0
    assert _cli("store", "commit", "-m", "first")[0] == 0
    toml_path = two_member_ws / "grip.toml"
    toml_path.write_text(_set_member_upstream(toml_path.read_text(), "alpha", upstream))
    rc, out = _cli("store", "status")
    assert rc == 4 and "plain remote name" in out, (rc, out)


def test_a_store_member_url_shaped_like_an_option_is_refused_by_name_where_the_members_load(
    two_member_ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_store_break_attempts import _set_member_remote

    marker = tmp_path / "PWNED"
    monkeypatch.setenv("PWN_MARK", str(marker))
    assert _cli("store", "init")[0] == 0
    assert _cli("store", "commit", "-m", "first")[0] == 0
    toml_path = two_member_ws / "grip.toml"
    toml_path.write_text(_set_member_remote(toml_path.read_text(), "alpha", "origin", "--upload-pack=touch $PWN_MARK"))

    for verb in (["store", "check"], ["store", "materialize"]):
        rc, out = _cli(*verb)
        assert not marker.exists(), f"{' '.join(verb)} ran the upload-pack command from grip.toml: rc={rc} {out}"
        assert rc == 4 and "alpha" in out and "starts with '-'" in out, (verb, rc, out)


def test_the_materialize_clone_reads_its_url_after_dashdash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A caller that reaches the clone without the load's refusal: the url is the repository, not an option.
    from gr2.python_cli import gitops, grip_cli

    url = "--upload-pack=touch PWNED"
    member = {"name": "m", "path": "m", "pin": "0" * 40, "remote": url}
    monkeypatch.setattr(grip_cli, "_grip_document", lambda root: {})
    monkeypatch.setattr(grip_cli, "_native_members", lambda root, rev=None: [member])
    monkeypatch.setattr(grip_cli, "_claimed_member_paths", lambda *a, **k: set())
    monkeypatch.setattr(grip_cli, "_native_member_working_root", lambda root, member, claimed: tmp_path / "m")
    errors: list[str] = []
    real_clone = gitops.clone

    def clone(*args, **kwargs):
        result = real_clone(*args, **kwargs)
        errors.append(result.stderr)
        return result

    monkeypatch.setattr(gitops, "clone", clone)
    with pytest.raises(grip_cli.NativeStoreRefusal, match="cannot be served by origin"):
        grip_cli._native_store_materialize.__wrapped__(tmp_path)
    # Read as the repository, git names the url; read as an option, it names the member path as the repository.
    assert errors and f"'{url}'" in errors[0], errors


def test_the_member_clone_reads_its_url_after_dashdash(tmp_path: Path) -> None:
    from gr2.python_cli import gitops

    url = "--upload-pack=touch PWNED"
    with pytest.raises(SystemExit) as exc:
        gitops.clone_repo(url, tmp_path / "staging" / "a", reference_repo_root=tmp_path / "none")
    # The message also echoes the url unquoted; git's own line quotes the repository it was handed.
    assert f"'{url}'" in str(exc.value), str(exc.value)
