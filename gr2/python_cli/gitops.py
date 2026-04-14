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


def remote_origin_url(path: Path) -> str | None:
    proc = git(path, "config", "--get", "remote.origin.url")
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def ensure_repo_cache(url: str, cache_repo_root: Path) -> bool:
    """Ensure a local bare mirror exists for a repo URL.

    Returns True if a cache was created, False if it already existed and was refreshed.
    """
    if cache_repo_root.exists():
        if not is_git_dir(cache_repo_root):
            raise SystemExit(f"repo cache path exists but is not a git dir: {cache_repo_root}")
        proc = subprocess.run(
            ["git", "--git-dir", str(cache_repo_root), "remote", "update", "--prune"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise SystemExit(f"failed to refresh repo cache {cache_repo_root}:\n{proc.stderr or proc.stdout}")
        return False

    cache_repo_root.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "clone", "--mirror", url, str(cache_repo_root)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"failed to seed repo cache {url} -> {cache_repo_root}:\n{proc.stderr or proc.stdout}")
    return True


def clone_repo(url: str, target_repo_root: Path, *, reference_repo_root: Path | None = None) -> bool:
    if target_repo_root.exists() and is_git_repo(target_repo_root):
        return False
    target_repo_root.parent.mkdir(parents=True, exist_ok=True)
    command = ["git", "clone"]
    if reference_repo_root is not None:
        command.extend(["--reference-if-able", str(reference_repo_root)])
    command.extend([url, str(target_repo_root)])
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"failed to clone {url} -> {target_repo_root}:\n{proc.stderr or proc.stdout}")
    return True


def branch_exists(repo_root: Path, branch: str) -> bool:
    return git(repo_root, "show-ref", "--verify", f"refs/heads/{branch}").returncode == 0


def fetch_ref(repo_root: Path, remote: str, source_ref: str, local_branch: str) -> None:
    proc = git(repo_root, "fetch", remote, f"{source_ref}:refs/heads/{local_branch}")
    if proc.returncode != 0:
        raise SystemExit(
            f"failed to fetch {source_ref} from {remote} into {local_branch} in {repo_root}:\n{proc.stderr or proc.stdout}"
        )


def refresh_existing_branch(repo_root: Path, remote: str, source_ref: str, local_branch: str) -> None:
    proc = git(repo_root, "fetch", remote, source_ref)
    if proc.returncode != 0:
        raise SystemExit(
            f"failed to fetch {source_ref} from {remote} in {repo_root}:\n{proc.stderr or proc.stdout}"
        )
    proc = git(repo_root, "branch", "-f", local_branch, "FETCH_HEAD")
    if proc.returncode != 0:
        raise SystemExit(
            f"failed to refresh existing branch {local_branch} from {source_ref} in {repo_root}:\n{proc.stderr or proc.stdout}"
        )


def is_git_dir(path: Path) -> bool:
    proc = subprocess.run(
        ["git", "--git-dir", str(path), "rev-parse", "--is-bare-repository"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


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
