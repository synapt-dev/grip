"""Executable contract for the native single-repository ``gr2 push`` verb."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        capture_output=True,
        text=True,
    )


def _init_repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "user.email", "test@example.com")
    (path / "tracked.txt").write_text("initial\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial")
    return path


def _bare(path: Path) -> Path:
    # -b main pins the bare's symbolic HEAD to refs/heads/main regardless of the
    # host's init.defaultBranch. Without it, a master-default runner (CI) leaves
    # HEAD at refs/heads/master; a later `git clone` of this bare cannot check
    # out (only refs/heads/main is ever pushed here), lands on an unborn master,
    # and `git push origin main` then fails "src refspec main does not match any".
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return path


def _commit(repo: Path, text: str) -> str:
    (repo / "tracked.txt").write_text(text + "\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", text)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_push_uses_the_configured_non_origin_remote_and_verifies_arrival(tmp_path: Path) -> None:
    from gr2.python_cli.push import push_current_branch

    repo = _init_repo(tmp_path / "repo")
    remote = _bare(tmp_path / "remote.git")
    _git(repo, "remote", "add", "upstream", str(remote))

    receipt = push_current_branch(repo, set_upstream=True)

    remote_head = _git(remote, "rev-parse", "refs/heads/main").stdout.strip()
    assert receipt.remote == "upstream"
    assert receipt.branch == "main"
    assert receipt.local_sha == remote_head
    assert receipt.remote_sha == remote_head
    assert _git(repo, "config", "--get", "branch.main.remote").stdout.strip() == "upstream"


def test_multiple_remotes_without_a_configured_or_explicit_choice_refuse(tmp_path: Path) -> None:
    from gr2.python_cli.push import PushError, push_current_branch

    repo = _init_repo(tmp_path / "repo")
    _git(repo, "remote", "add", "one", str(_bare(tmp_path / "one.git")))
    _git(repo, "remote", "add", "two", str(_bare(tmp_path / "two.git")))

    with pytest.raises(PushError, match="multiple remotes"):
        push_current_branch(repo)


def test_branch_push_remote_takes_precedence_over_fetch_remote(tmp_path: Path) -> None:
    from gr2.python_cli.push import push_current_branch

    repo = _init_repo(tmp_path / "repo")
    fetcher = _bare(tmp_path / "fetcher.git")
    pusher = _bare(tmp_path / "pusher.git")
    _git(repo, "remote", "add", "fetcher", str(fetcher))
    _git(repo, "remote", "add", "pusher", str(pusher))
    _git(repo, "config", "branch.main.remote", "fetcher")
    _git(repo, "config", "branch.main.pushRemote", "pusher")

    receipt = push_current_branch(repo)

    assert receipt.remote == "pusher"
    assert _git(pusher, "rev-parse", "refs/heads/main").stdout.strip() == receipt.local_sha
    assert _git(fetcher, "show-ref", "--verify", "refs/heads/main", check=False).returncode != 0


def test_explicit_remote_selects_one_of_multiple_remotes(tmp_path: Path) -> None:
    from gr2.python_cli.push import push_current_branch

    repo = _init_repo(tmp_path / "repo")
    one = _bare(tmp_path / "one.git")
    two = _bare(tmp_path / "two.git")
    _git(repo, "remote", "add", "one", str(one))
    _git(repo, "remote", "add", "two", str(two))

    receipt = push_current_branch(repo, remote="two")

    assert receipt.remote == "two"
    assert _git(two, "rev-parse", "refs/heads/main").stdout.strip() == receipt.local_sha
    assert _git(one, "show-ref", "--verify", "refs/heads/main", check=False).returncode != 0


def test_detached_head_refuses_before_any_push(tmp_path: Path) -> None:
    from gr2.python_cli.push import PushError, push_current_branch

    repo = _init_repo(tmp_path / "repo")
    remote = _bare(tmp_path / "remote.git")
    _git(repo, "remote", "add", "upstream", str(remote))
    _git(repo, "checkout", "--detach", "HEAD")

    with pytest.raises(PushError, match="detached HEAD"):
        push_current_branch(repo)

    assert _git(remote, "show-ref", "--verify", "refs/heads/main", check=False).returncode != 0


def test_divergent_push_refuses_without_rewriting_the_remote(tmp_path: Path) -> None:
    from gr2.python_cli.push import PushError, push_current_branch

    repo = _init_repo(tmp_path / "repo")
    remote = _bare(tmp_path / "remote.git")
    _git(repo, "remote", "add", "upstream", str(remote))
    push_current_branch(repo, set_upstream=True)

    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", str(remote), str(other)], check=True, capture_output=True, text=True
    )
    _git(other, "config", "user.name", "Other")
    _git(other, "config", "user.email", "other@example.com")
    remote_advanced = _commit(other, "remote advance")
    _git(other, "push", "origin", "main")

    _commit(repo, "local divergence")
    with pytest.raises(PushError):
        push_current_branch(repo)

    assert _git(remote, "rev-parse", "refs/heads/main").stdout.strip() == remote_advanced


def test_acknowledged_push_without_matching_receipt_evidence_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gr2.python_cli import push as push_ops

    repo = _init_repo(tmp_path / "repo")

    def fake_git(_repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        if args == ("branch", "--show-current"):
            return subprocess.CompletedProcess(["git", *args], 0, "main\n", "")
        if args[:2] == ("config", "--get"):
            if args[2] == "branch.main.remote":
                return subprocess.CompletedProcess(["git", *args], 0, "upstream\n", "")
            return subprocess.CompletedProcess(["git", *args], 1, "", "")
        if args == ("remote",):
            return subprocess.CompletedProcess(["git", *args], 0, "upstream\n", "")
        if args == ("rev-parse", "--verify", "HEAD"):
            return subprocess.CompletedProcess(["git", *args], 0, "a" * 40 + "\n", "")
        if args[:4] == ("push", "--porcelain", "--", "upstream"):
            return subprocess.CompletedProcess(["git", *args], 0, "", "")
        if args[:4] == ("ls-remote", "--heads", "--", "upstream"):
            return subprocess.CompletedProcess(["git", *args], 0, "", "")
        raise AssertionError(args)

    monkeypatch.setattr(push_ops, "git", fake_git)

    with pytest.raises(push_ops.PushEvidenceError, match="did not provide"):
        push_ops.push_current_branch(repo)


def test_force_with_lease_is_threaded_to_git_without_raw_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gr2.python_cli import push as push_ops

    repo = _init_repo(tmp_path / "repo")
    push_args: tuple[str, ...] | None = None

    def fake_git(_repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        nonlocal push_args
        if args == ("branch", "--show-current"):
            return subprocess.CompletedProcess(["git", *args], 0, "main\n", "")
        if args == ("remote",):
            return subprocess.CompletedProcess(["git", *args], 0, "upstream\n", "")
        if args[:2] == ("config", "--get"):
            if args[2] == "branch.main.remote":
                return subprocess.CompletedProcess(["git", *args], 0, "upstream\n", "")
            return subprocess.CompletedProcess(["git", *args], 1, "", "")
        if args == ("rev-parse", "--verify", "HEAD"):
            return subprocess.CompletedProcess(["git", *args], 0, "a" * 40 + "\n", "")
        if args[0] == "push":
            push_args = args
            return subprocess.CompletedProcess(["git", *args], 0, "", "")
        if args[:4] == ("ls-remote", "--heads", "--", "upstream"):
            row = "a" * 40 + "\trefs/heads/main\n"
            return subprocess.CompletedProcess(["git", *args], 0, row, "")
        raise AssertionError(args)

    monkeypatch.setattr(push_ops, "git", fake_git)

    push_ops.push_current_branch(repo, force_with_lease=True)

    assert push_args is not None
    assert "--force-with-lease" in push_args
    assert "--force" not in push_args


def test_cli_exposes_force_with_lease_but_never_raw_force(tmp_path: Path) -> None:
    from gr2.python_cli.app import app

    repo = _init_repo(tmp_path / "repo")
    remote = _bare(tmp_path / "remote.git")
    _git(repo, "remote", "add", "upstream", str(remote))
    runner = CliRunner()

    safe = runner.invoke(
        app,
        ["push", "--repo-path", str(repo), "--set-upstream", "--force-with-lease"],
    )
    unsafe = runner.invoke(app, ["push", "--repo-path", str(repo), "--force"])

    assert safe.exit_code == 0, safe.output
    # Assert the BEHAVIOR — raw --force is rejected and never offered — not the
    # exact wording of Typer's usage error. That wording is a Rich-formatted
    # panel that varies by Typer/Click version (it changed from a plain
    # "No such option: --force" line to a coloured panel), and matching its text
    # is what made this test brittle. Exit code + the --help option surface are
    # the stable contract.
    assert unsafe.exit_code != 0, unsafe.output
    help_output = _strip_ansi(runner.invoke(app, ["push", "--help"]).output)
    assert "--force-with-lease" in help_output
    # --force-with-lease contains the substring "--force"; strip it before
    # asserting a bare --force option is absent.
    assert "--force" not in help_output.replace("--force-with-lease", "")
    assert _git(repo, "config", "--get", "branch.main.remote").stdout.strip() == "upstream"


def test_push_cli_honors_explicit_repo_path_and_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gr2.python_cli.app import app

    repo = _init_repo(tmp_path / "repo")
    one = _bare(tmp_path / "one.git")
    two = _bare(tmp_path / "two.git")
    _git(repo, "remote", "add", "one", str(one))
    _git(repo, "remote", "add", "two", str(two))
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        app,
        ["push", "--repo-path", str(repo), "--remote", "two"],
    )

    assert result.exit_code == 0, result.output
    assert _git(two, "rev-parse", "refs/heads/main").stdout.strip() == _git(
        repo, "rev-parse", "HEAD"
    ).stdout.strip()
    assert _git(one, "show-ref", "--verify", "refs/heads/main", check=False).returncode != 0


def test_push_cli_threads_force_with_lease_when_remote_has_advanced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gr2.python_cli.app import app
    from gr2.python_cli.push import push_current_branch

    repo = _init_repo(tmp_path / "repo")
    remote = _bare(tmp_path / "remote.git")
    _git(repo, "remote", "add", "upstream", str(remote))
    push_current_branch(repo, set_upstream=True)

    local_sha = _commit(repo, "local divergence")
    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", str(remote), str(other)], check=True, capture_output=True, text=True
    )
    _git(other, "config", "user.name", "Other")
    _git(other, "config", "user.email", "other@example.com")
    remote_advanced = _commit(other, "remote advance")
    _git(other, "push", "origin", "main")
    _git(repo, "fetch", "upstream", "main")
    assert _git(repo, "rev-parse", "upstream/main").stdout.strip() == remote_advanced
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "push",
            "--repo-path",
            str(repo),
            "--remote",
            "upstream",
            "--force-with-lease",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _git(remote, "rev-parse", "refs/heads/main").stdout.strip() == local_sha
