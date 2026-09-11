"""`git review` single-repo entry point: open records {repo, base, head} under
`.git/grip/review.json`, status reads it, close removes it (and the empty store dir).
base defaults to the merge-base of HEAD and the default branch. Outside a git repo it
refuses. The console script is registered as `git-review` so git resolves `git review`."""
import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from python_cli import git_review


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    _run(r, "init", "-q")
    _run(r, "config", "user.email", "a@b")
    _run(r, "config", "user.name", "a")
    (r / "f").write_text("one")
    _run(r, "add", "-A")
    _run(r, "commit", "-q", "-m", "c1", "--no-gpg-sign")
    return r


def _sha(repo: Path, rev: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", rev], capture_output=True, text=True
    ).stdout.strip()


def test_open_writes_record_under_dot_git_grip(tmp_path):
    r = _repo(tmp_path)
    assert not (r / ".git" / "grip").exists()
    rec = git_review.open_review(r)
    stored = r / ".git" / "grip" / "review.json"
    assert stored.is_file()
    assert json.loads(stored.read_text()) == rec
    assert rec["repo"] == r.name
    assert rec["head"] == _sha(r, "HEAD")


def test_base_defaults_to_merge_base_with_default_branch(tmp_path):
    r = _repo(tmp_path)
    _run(r, "branch", "-m", "main")  # name the default branch
    base_sha = _sha(r, "HEAD")       # divergence point
    _run(r, "checkout", "-q", "-b", "feature")
    (r / "f").write_text("two")
    _run(r, "add", "-A")
    _run(r, "commit", "-q", "-m", "c2", "--no-gpg-sign")
    rec = git_review.open_review(r)
    assert rec["base"] == base_sha           # merge-base(feature, main) == c1
    assert rec["head"] == _sha(r, "HEAD")     # c2
    assert rec["base"] != rec["head"]


def test_status_and_close_roundtrip(tmp_path):
    r = _repo(tmp_path)
    assert git_review.read_review(r) is None
    git_review.open_review(r)
    assert git_review.read_review(r) is not None
    assert git_review.close_review(r) is True
    assert git_review.read_review(r) is None
    assert not (r / ".git" / "grip").exists()  # empty store dir removed
    assert git_review.close_review(r) is False  # idempotent: nothing to close


def test_outside_a_git_repo_refuses(tmp_path):
    with pytest.raises(git_review.GitReviewError):
        git_review._repo_root(tmp_path)  # tmp_path is not a git repo


def test_console_entry_point_is_registered():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    scripts = data["project"]["scripts"]
    assert scripts.get("git-review") == "gr2.python_cli.git_review:main"


def test_base_resolves_via_remote_tracking_ref_when_local_default_deleted(tmp_path):
    """Fix (1): merge-base runs against refs/remotes/origin/<name>, so a clone whose
    LOCAL default branch is deleted still records base != HEAD (not a silent empty diff)."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _run(origin, "init", "-q", "-b", "dev")  # default branch is 'dev', not main/master
    _run(origin, "config", "user.email", "a@b")
    _run(origin, "config", "user.name", "a")
    (origin / "f").write_text("one")
    _run(origin, "add", "-A")
    _run(origin, "commit", "-q", "-m", "c1", "--no-gpg-sign")
    c1 = _sha(origin, "HEAD")

    clone = tmp_path / "clone"
    _run(tmp_path, "clone", "-q", str(origin), str(clone))
    _run(clone, "config", "user.email", "a@b")
    _run(clone, "config", "user.name", "a")
    # a feature branch diverging from dev, then DELETE the local default branch
    _run(clone, "checkout", "-q", "-b", "feature")
    (clone / "f").write_text("two")
    _run(clone, "add", "-A")
    _run(clone, "commit", "-q", "-m", "c2", "--no-gpg-sign")
    _run(clone, "branch", "-D", "dev")  # local default gone; origin/HEAD -> dev remains

    rec = git_review.open_review(clone)
    assert rec["base"] == c1                # merge-base(feature, refs/remotes/origin/dev)
    assert rec["base"] != rec["head"]       # NOT a silent base==HEAD


def test_store_root_is_absolute_git_dir_when_dot_git_is_a_file(tmp_path):
    """Fix (2): a repo whose .git is a FILE (separate git dir) stores under the resolved
    git dir instead of tracebacking on a .git/grip path."""
    gitdir = tmp_path / "gd"
    work = tmp_path / "work"
    work.mkdir()
    _run(work, "init", "-q", f"--separate-git-dir={gitdir}")
    _run(work, "config", "user.email", "a@b")
    _run(work, "config", "user.name", "a")
    (work / "f").write_text("one")
    _run(work, "add", "-A")
    _run(work, "commit", "-q", "-m", "c1", "--no-gpg-sign")
    assert (work / ".git").is_file()  # .git is a file, not a dir

    rec = git_review.open_review(work)  # must not raise NotADirectoryError
    assert (gitdir / "grip" / "review.json").is_file()
    assert rec["repo"] == work.name


def test_base_equals_head_fallback_warns(tmp_path, capsys, monkeypatch):
    """Fix (3): the base == HEAD fallback (no diverging branch) prints a warning line."""
    r = tmp_path / "solo"
    r.mkdir()
    _run(r, "init", "-q", "-b", "trunk")  # default is neither main nor master, no origin
    _run(r, "config", "user.email", "a@b")
    _run(r, "config", "user.name", "a")
    (r / "f").write_text("one")
    _run(r, "add", "-A")
    _run(r, "commit", "-q", "-m", "c1", "--no-gpg-sign")
    monkeypatch.chdir(r)          # main() resolves repo from cwd
    rc = git_review.main(["open"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "base = HEAD" in out and "warning" in out


def test_dash_h_prints_usage_and_exits_zero(capsys):
    """Fix-forward: -h/--help prints usage and exits 0, not 'unknown subcommand' exit 2."""
    rc = git_review.main(["-h"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "usage: git review" in out
