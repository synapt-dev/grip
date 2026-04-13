from __future__ import annotations

import subprocess
from pathlib import Path


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def is_git_repo(path: Path) -> bool:
    proc = git(path, "rev-parse", "--is-inside-work-tree")
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def repo_dirty(path: Path) -> bool:
    proc = git(path, "status", "--porcelain")
    return proc.returncode == 0 and bool(proc.stdout.strip())


def ensure_lane_checkout(
    *,
    source_repo_root: Path,
    target_repo_root: Path,
    branch: str,
) -> bool:
    """Ensure a real lane checkout exists.

    Returns True if this was first materialization, False if already present.
    """
    if target_repo_root.exists() and is_git_repo(target_repo_root):
        return False

    target_repo_root.parent.mkdir(parents=True, exist_ok=True)

    branch_exists = git(source_repo_root, "show-ref", "--verify", f"refs/heads/{branch}").returncode == 0
    if branch_exists:
        proc = git(source_repo_root, "worktree", "add", str(target_repo_root), branch)
    else:
        proc = git(source_repo_root, "worktree", "add", "-b", branch, str(target_repo_root), "HEAD")

    if proc.returncode != 0:
        raise SystemExit(
            f"failed to create lane checkout for {source_repo_root.name} on {branch}:\n{proc.stderr or proc.stdout}"
        )
    return True


def checkout_branch(repo_root: Path, branch: str) -> None:
    proc = git(repo_root, "checkout", branch)
    if proc.returncode != 0:
        raise SystemExit(f"failed to checkout {branch} in {repo_root}:\n{proc.stderr or proc.stdout}")


def stash_if_dirty(repo_root: Path, message: str) -> bool:
    if not repo_dirty(repo_root):
        return False
    proc = git(repo_root, "stash", "push", "-u", "-m", message)
    if proc.returncode != 0:
        raise SystemExit(f"failed to stash dirty work in {repo_root}:\n{proc.stderr or proc.stdout}")
    return True
