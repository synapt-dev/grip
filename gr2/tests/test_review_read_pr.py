"""`review read-pr OWNER/REPO N` opens a PR of a repo that is NOT a member of the reader's workspace, for reading.

`review checkout-pr` reads the repo from the workspace spec, so a reviewer asked to read another project's PR got
"repo not found in workspace spec" and went to raw git. The read form fetches the PR head and its base into a fresh
directory outside any workspace and prints the diff summary; that directory is marked read-only, and gr2's write
verbs (add, commit, push) refuse in it with the same sentence. The rows run the real CLI in a subprocess against a
local bare repo standing in for the forge (`--url`), so no network is touched.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

GR2_ROOT = Path(__file__).parents[1]

_CLI = """
import os, runpy, sys
sys.argv = [os.path.join(os.path.dirname(sys.executable), "gr2")] + sys.argv[1:]
runpy.run_module("gr2.python_cli.app", run_name="__main__")
"""

READ_ONLY_WORDS = "is a read-only review"


def _gr2(cwd: Path, *args: str, home: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(GR2_ROOT)}
    if home is not None:
        # A fixture HOME, so the row controls the global git config the CLI sees.
        env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"))
        env.pop("GIT_CONFIG_GLOBAL", None)
    return subprocess.run([sys.executable, "-P", "-c", _CLI, *args], cwd=cwd, text=True, capture_output=True, env=env)


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@localhost",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@localhost",
    }
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True, env=env).stdout.strip()


@pytest.fixture()
def forge(tmp_path: Path) -> tuple[Path, str, str]:
    """A bare repo whose PR head (refs/pull/7/head) branched from `main`, and `main` one commit past the branch point."""
    work = tmp_path / "upstream"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "a.txt").write_text("one\n", encoding="utf-8")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-q", "-m", "base")
    base = _git(work, "rev-parse", "HEAD")
    _git(work, "checkout", "-q", "-b", "feature")
    (work / "b.txt").write_text("pr change\n", encoding="utf-8")
    _git(work, "add", "b.txt")
    _git(work, "commit", "-q", "-m", "pr")
    head = _git(work, "rev-parse", "HEAD")
    # main moves on after the PR branched, so the PR base (the branch point) differs from main's tip.
    _git(work, "checkout", "-q", "main")
    (work / "c.txt").write_text("main moved\n", encoding="utf-8")
    _git(work, "add", "c.txt")
    _git(work, "commit", "-q", "-m", "main moves on")
    bare = tmp_path / "widget.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(bare, "update-ref", "refs/pull/7/head", head)
    return bare, base, head


@pytest.fixture()
def opened(tmp_path: Path, forge: tuple[Path, str, str]) -> tuple[Path, subprocess.CompletedProcess[str], str, str]:
    bare, base, head = forge
    reader = tmp_path / "reader"
    reader.mkdir()
    dest = reader / "widget-pr7"
    result = _gr2(reader, "review", "read-pr", "acme/widget", "7", "--url", str(bare), "--dir", str(dest))
    return dest, result, base, head


def test_read_pr_outside_any_workspace_opens_the_head_and_its_diff(opened) -> None:
    dest, result, base, head = opened
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert head in result.stdout and base in result.stdout, result.stdout
    assert "b.txt" in result.stdout, result.stdout
    assert _git(dest, "rev-parse", "HEAD") == head
    assert _git(dest, "diff", "--name-only", f"{base}...{head}") == "b.txt"


def test_read_pr_prints_the_branch_point_as_base_not_the_default_branch_tip(opened, forge) -> None:
    dest, result, base, _head = opened
    assert result.returncode == 0, (result.stdout, result.stderr)
    main_tip = _git(forge[0], "rev-parse", "refs/heads/main")
    assert main_tip != base
    assert main_tip not in result.stdout, result.stdout
    assert "c.txt" not in result.stdout, result.stdout


def test_read_pr_does_not_pass_an_option_shaped_url_to_git_as_an_option(tmp_path: Path) -> None:
    witness = tmp_path / "RAN"
    dest = tmp_path / "widget-pr7"
    result = _gr2(tmp_path, "review", "read-pr", "acme/widget", "7", "--dir", str(dest), f"--url=--upload-pack=touch {witness};false")
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert not witness.exists(), "the --url value ran as a git option"
    assert "Traceback" not in result.stderr


def test_a_failed_fetch_leaves_no_directory_so_a_retry_works(tmp_path: Path, forge) -> None:
    bare, _base, head = forge
    dest = tmp_path / "widget-pr7"
    missing = _gr2(tmp_path, "review", "read-pr", "acme/widget", "9", "--url", str(bare), "--dir", str(dest))
    assert missing.returncode == 1, (missing.stdout, missing.stderr)
    assert not dest.exists()
    _git(bare, "update-ref", "refs/pull/9/head", head)
    retry = _gr2(tmp_path, "review", "read-pr", "acme/widget", "9", "--url", str(bare), "--dir", str(dest))
    assert retry.returncode == 0, (retry.stdout, retry.stderr)
    assert _git(dest, "rev-parse", "HEAD") == head


@pytest.mark.parametrize(
    "verb",
    [
        ("commit", "-m", "should not land"),
        ("push",),
        ("add", "a.txt"),
    ],
    ids=["commit", "push", "add"],
)
def test_write_verbs_refuse_in_a_read_review(opened, verb: tuple[str, ...]) -> None:
    dest, result, _base, head = opened
    assert result.returncode == 0, result.stderr
    (dest / "a.txt").write_text("edited by a reader\n", encoding="utf-8")
    refused = _gr2(dest, *verb, "--repo-path", str(dest))
    assert refused.returncode == 1, (refused.stdout, refused.stderr)
    assert f"{dest.resolve()} {READ_ONLY_WORDS}" in refused.stderr, refused.stderr
    assert f"gr2 {verb[0]} refuses" in refused.stderr, refused.stderr
    assert "Traceback" not in refused.stderr, refused.stderr
    assert _git(dest, "rev-parse", "HEAD") == head
    assert _git(dest, "diff", "--cached", "--name-only") == ""


def test_read_pr_refuses_a_name_that_is_not_owner_slash_repo(tmp_path: Path) -> None:
    result = _gr2(tmp_path, "review", "read-pr", "widget", "7", "--url", str(tmp_path / "nope.git"))
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "OWNER/REPO" in result.stderr, result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("owner_repo", ["acme/..", "acme/.", "../widget", "./widget"])
def test_read_pr_refuses_dot_and_dotdot_for_owner_and_repo(tmp_path: Path, forge, owner_repo: str) -> None:
    result = _gr2(tmp_path, "review", "read-pr", owner_repo, "7", "--url", str(forge[0]), "--dir", str(tmp_path / "d"))
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "is not OWNER/REPO" in result.stderr, result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "d").exists()


def test_read_pr_refuses_pr_zero(tmp_path: Path, forge) -> None:
    result = _gr2(tmp_path, "review", "read-pr", "acme/widget", "0", "--url", str(forge[0]), "--dir", str(tmp_path / "d"))
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "PR number must be positive" in result.stderr, result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "d").exists()


def test_read_pr_refuses_a_negative_pr_number(tmp_path: Path, forge) -> None:
    dest = tmp_path / "d"
    result = _gr2(tmp_path, "review", "read-pr", "--url", str(forge[0]), "--dir", str(dest), "--", "acme/widget", "-1")
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "PR number must be positive, got -1" in result.stderr, result.stderr
    assert "Traceback" not in result.stderr
    assert not dest.exists()


def test_read_pr_with_a_huge_pr_number_fails_in_one_line_and_leaves_no_dir(tmp_path: Path, forge) -> None:
    dest = tmp_path / "d"
    result = _gr2(tmp_path, "review", "read-pr", "acme/widget", "9" * 20, "--url", str(forge[0]), "--dir", str(dest))
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert f"fetching PR {'9' * 20} of acme/widget" in result.stderr, result.stderr
    assert "Traceback" not in result.stderr
    assert not dest.exists()


def test_a_bare_word_url_naming_a_global_remote_writes_only_the_two_read_refs(tmp_path: Path, forge) -> None:
    # A tag on the base, so a fetch that follows or copies tags would write one.
    _git(forge[0], "tag", "v1", forge[1])
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        f'[remote "origin"]\n\turl = {forge[0]}\n\tfetch = +refs/pull/*/head:refs/remotes/origin/pr/*\n\ttagOpt = --tags\n',
        encoding="utf-8",
    )
    dest = tmp_path / "d"
    result = _gr2(tmp_path, "review", "read-pr", "acme/widget", "7", "--url", "origin", "--dir", str(dest), home=home)
    assert result.returncode == 0, (result.stdout, result.stderr)
    refs = _git(dest, "for-each-ref", "--format=%(refname)").splitlines()
    assert refs == ["refs/gr2-read/base", "refs/gr2-read/head"], refs


def test_read_pr_refuses_an_existing_dir_and_leaves_it_intact(tmp_path: Path, forge) -> None:
    dest = tmp_path / "mine"
    dest.mkdir()
    (dest / "keep.txt").write_text("mine\n", encoding="utf-8")
    result = _gr2(tmp_path, "review", "read-pr", "acme/widget", "7", "--url", str(forge[0]), "--dir", str(dest))
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "already exists" in result.stderr, result.stderr
    assert "Traceback" not in result.stderr
    assert sorted(p.name for p in dest.iterdir()) == ["keep.txt"]
    assert (dest / "keep.txt").read_text(encoding="utf-8") == "mine\n"


def test_read_pr_refuses_a_dangling_symlink_dir_and_leaves_it_alone(tmp_path: Path, forge) -> None:
    target = tmp_path / "elsewhere" / "made"
    link = tmp_path / "link"
    link.symlink_to(target)
    result = _gr2(tmp_path, "review", "read-pr", "acme/widget", "7", "--url", str(forge[0]), "--dir", str(link))
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "already exists" in result.stderr, result.stderr
    assert "Traceback" not in result.stderr
    assert link.is_symlink() and os.readlink(link) == str(target)
    assert not (tmp_path / "elsewhere").exists()


def test_read_pr_under_a_regular_file_refuses_in_one_line(tmp_path: Path, forge) -> None:
    blocker = tmp_path / "afile"
    blocker.write_text("x\n", encoding="utf-8")
    result = _gr2(tmp_path, "review", "read-pr", "acme/widget", "7", "--url", str(forge[0]), "--dir", str(blocker / "d"))
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "cannot create" in result.stderr, result.stderr
    assert "Traceback" not in result.stderr
    assert blocker.read_text(encoding="utf-8") == "x\n"


def test_read_pr_does_not_echo_url_credentials(tmp_path: Path) -> None:
    secret = "SECRETTOK"
    url = f"file://user:{secret}@/{tmp_path}/nope.git"
    result = _gr2(tmp_path, "review", "read-pr", "acme/widget", "7", "--url", url, "--dir", str(tmp_path / "d"))
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert secret not in result.stderr + result.stdout, result.stderr
    assert "Traceback" not in result.stderr


def test_read_pr_refuses_ext_urls_even_when_the_user_allows_them(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[protocol \"ext\"]\n\tallow = always\n", encoding="utf-8")
    witness = tmp_path / "RAN"
    url = f"ext::sh -c touch% {witness}"
    result = _gr2(tmp_path, "review", "read-pr", "acme/widget", "7", "--url", url, "--dir", str(tmp_path / "d"), home=home)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert not witness.exists(), "the ext:: url ran a command"
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    ("scope", "value"),
    [("global", "true"), ("local", "false")],
    ids=["global-true-does-not-fence", "local-false-does-not-fence"],
)
def test_only_a_local_true_mark_fences_the_write_verbs(tmp_path: Path, scope: str, value: str) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    if scope == "global":
        (home / ".gitconfig").write_text(f"[gr2]\n\treadOnlyReview = {value}\n", encoding="utf-8")
    else:
        _git(repo, "config", "gr2.readOnlyReview", value)
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    added = _gr2(repo, "add", "a.txt", "--repo-path", str(repo), home=home)
    assert added.returncode == 0, (added.stdout, added.stderr)
    assert READ_ONLY_WORDS not in added.stderr
    assert _git(repo, "diff", "--cached", "--name-only") == "a.txt"
