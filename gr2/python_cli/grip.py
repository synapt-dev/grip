"""Grip object model: git-native multi-repo workspace snapshots.

Uses git plumbing (hash-object, mktree, commit-tree, update-ref) to store
workspace state as content-addressable objects in a dedicated .grip/ repo.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from python_cli.gitops import git


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GripInitError(Exception):
    """Raised when .grip/ repo is missing or not properly initialized."""


class GripCorruptError(Exception):
    """Raised when .grip/ repo state is corrupt (bad HEAD, missing objects)."""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class GripCommitInfo:
    sha: str
    message: str
    repos: list[str]
    timestamp: str = ""


@dataclass
class GripDiff:
    changed: dict[str, dict[str, str]] = field(default_factory=dict)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _grip_git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return git(workspace / ".grip", *args)


def _validate_grip_repo(workspace: Path) -> None:
    """Verify .grip/ is a valid git repo. Raises GripInitError if not."""
    grip_dir = workspace / ".grip"
    if not grip_dir.exists():
        raise GripInitError(
            f"No .grip/ directory at {workspace}. Run grip_init first."
        )
    git_dir = grip_dir / ".git"
    if not git_dir.exists():
        raise GripInitError(
            f".grip/ exists but has no .git/ at {workspace}. Run grip_init to repair."
        )
    if git_dir.is_file():
        raise GripInitError(
            f".grip/.git is a file, not a directory (corrupt). "
            f"Remove {git_dir} and run grip_init to repair."
        )


def _hash_blob(workspace: Path, content: str) -> str:
    proc = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=workspace / ".grip",
        input=content,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"hash-object failed: {proc.stderr}")
    return proc.stdout.strip()


def _mktree(workspace: Path, entries: list[str]) -> str:
    tree_input = "\n".join(entries) + "\n" if entries else ""
    proc = subprocess.run(
        ["git", "mktree"],
        cwd=workspace / ".grip",
        input=tree_input,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"mktree failed: {proc.stderr}")
    return proc.stdout.strip()


def _commit_tree(
    workspace: Path, tree_sha: str, *, parent: str | None = None, message: str = ""
) -> str:
    args = ["git", "commit-tree", tree_sha]
    if parent:
        args.extend(["-p", parent])
    args.extend(["-m", message or "grip snapshot"])
    proc = subprocess.run(
        args,
        cwd=workspace / ".grip",
        capture_output=True,
        text=True,
        check=False,
        env=_git_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"commit-tree failed: {proc.stderr}")
    return proc.stdout.strip()


def _git_env() -> dict[str, str]:
    import os

    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "grip")
    env.setdefault("GIT_AUTHOR_EMAIL", "grip@synapt.dev")
    env.setdefault("GIT_COMMITTER_NAME", "grip")
    env.setdefault("GIT_COMMITTER_EMAIL", "grip@synapt.dev")
    return env


def _current_head(workspace: Path, *, strict: bool = False) -> str | None:
    """Get current HEAD of .grip/ repo.

    Returns None if no commits yet. Raises GripCorruptError if HEAD exists
    but points to invalid state (when strict=True or when HEAD file is missing/corrupt).
    """
    head_path = workspace / ".grip" / ".git" / "HEAD"
    if not head_path.exists():
        raise GripCorruptError(
            f".grip/.git/HEAD is missing at {workspace}. "
            "The grip repo may be corrupt."
        )

    proc = _grip_git(workspace, "rev-parse", "HEAD")
    if proc.returncode != 0:
        head_content = head_path.read_text().strip()
        if head_content.startswith("ref: "):
            return None
        raise GripCorruptError(
            f".grip/HEAD points to invalid ref: {head_content!r}. "
            "The grip repo may be corrupt."
        )
    return proc.stdout.strip() or None


def _repo_tree_entries(workspace: Path, name: str, repo_path: Path) -> str:
    """Build a tree for one repo and return an mktree entry line."""
    from python_cli.gitops import repo_dirty

    blobs: list[str] = []

    head = git(repo_path, "rev-parse", "HEAD")
    if head.returncode == 0 and head.stdout.strip():
        sha = _hash_blob(workspace, head.stdout.strip())
        blobs.append(f"100644 blob {sha}\tcommit")

    branch = git(repo_path, "branch", "--show-current")
    if branch.returncode == 0 and branch.stdout.strip():
        sha = _hash_blob(workspace, branch.stdout.strip())
        blobs.append(f"100644 blob {sha}\tbranch")

    remote = git(repo_path, "config", "--get", "remote.origin.url")
    if remote.returncode == 0 and remote.stdout.strip():
        sha = _hash_blob(workspace, remote.stdout.strip())
        blobs.append(f"100644 blob {sha}\tremote")

    is_dirty = repo_dirty(repo_path)
    dirty_sha = _hash_blob(workspace, "true" if is_dirty else "false")
    blobs.append(f"100644 blob {dirty_sha}\tdirty")

    tree_sha = _mktree(workspace, blobs)
    return f"040000 tree {tree_sha}\t{name}"


def _changeset_tree(
    workspace: Path,
    *,
    changeset_type: str = "",
    sprint: str = "",
) -> str | None:
    """Build the .grip/ changeset metadata subtree. Returns tree SHA or None."""
    blobs: list[str] = []

    if changeset_type:
        sha = _hash_blob(workspace, changeset_type)
        blobs.append(f"100644 blob {sha}\ttype")

    if sprint:
        sha = _hash_blob(workspace, sprint)
        blobs.append(f"100644 blob {sha}\tsprint")

    if not blobs:
        return None
    return _mktree(workspace, blobs)


def _config_overlay_tree(workspace: Path, overlay_dir: Path) -> str | None:
    """Build a config/ tree from overlay JSON files for inclusion in grip commit."""
    entries: list[str] = []

    for f in sorted(overlay_dir.glob("*.json")):
        content = f.read_text()
        sha = _hash_blob(workspace, content)
        entries.append(f"100644 blob {sha}\t{f.name}")

    prompts_dir = overlay_dir / "prompts"
    if prompts_dir.is_dir():
        prompt_entries: list[str] = []
        for pf in sorted(prompts_dir.glob("*.json")):
            content = pf.read_text()
            sha = _hash_blob(workspace, content)
            prompt_entries.append(f"100644 blob {sha}\t{pf.name}")
        if prompt_entries:
            prompts_tree = _mktree(workspace, prompt_entries)
            entries.append(f"040000 tree {prompts_tree}\tprompts")

    if not entries:
        return None
    return _mktree(workspace, entries)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def grip_init(workspace: Path) -> Path:
    """Initialize the .grip/ git repo. Idempotent."""
    grip_dir = workspace / ".grip"
    if not grip_dir.exists():
        grip_dir.mkdir(parents=True)
    git_dir = grip_dir / ".git"
    if git_dir.is_file():
        raise GripInitError(
            f".grip/.git is a file, not a directory (corrupt). "
            f"Remove {git_dir} and run grip_init again."
        )
    if not git_dir.exists():
        git(grip_dir, "init")
        git(grip_dir, "config", "user.email", "grip@synapt.dev")
        git(grip_dir, "config", "user.name", "grip")
    return grip_dir


def grip_snapshot(
    workspace: Path,
    repos: dict[str, Path],
    *,
    changeset_type: str = "",
    sprint: str = "",
    message: str = "",
    overlay_dir: Path | None = None,
) -> str:
    """Create a grip commit from current repo states. Returns commit SHA."""
    _validate_grip_repo(workspace)
    repo_entries: list[str] = []
    for name in sorted(repos):
        entry = _repo_tree_entries(workspace, name, repos[name])
        repo_entries.append(entry)

    repos_tree = _mktree(workspace, repo_entries)
    root_entries = [f"040000 tree {repos_tree}\trepos"]

    cs_tree = _changeset_tree(workspace, changeset_type=changeset_type, sprint=sprint)
    if cs_tree:
        root_entries.append(f"040000 tree {cs_tree}\t.grip")

    if overlay_dir and overlay_dir.is_dir():
        config_tree = _config_overlay_tree(workspace, overlay_dir)
        if config_tree:
            root_entries.append(f"040000 tree {config_tree}\tconfig")

    root_tree = _mktree(workspace, root_entries)

    parent = _current_head(workspace)
    commit_msg = message or f"grip snapshot ({changeset_type})" if changeset_type else message or "grip snapshot"
    commit_sha = _commit_tree(workspace, root_tree, parent=parent, message=commit_msg)

    _grip_git(workspace, "update-ref", "HEAD", commit_sha)

    return commit_sha


def grip_log(workspace: Path, *, max_count: int = 10) -> list[GripCommitInfo]:
    """List grip commit history, most recent first."""
    _validate_grip_repo(workspace)
    head = _current_head(workspace)
    if not head:
        return []

    proc = _grip_git(
        workspace,
        "log",
        f"--max-count={max_count}",
        "--format=%H%n%s%n%aI%n---",
        "HEAD",
    )
    if proc.returncode != 0:
        return []

    entries: list[GripCommitInfo] = []
    chunks = proc.stdout.strip().split("---\n")
    for chunk in chunks:
        chunk = chunk.strip().rstrip("---").strip()
        if not chunk:
            continue
        lines = chunk.splitlines()
        if len(lines) < 2:
            continue
        sha = lines[0].strip()
        msg = lines[1].strip()
        ts = lines[2].strip() if len(lines) > 2 else ""

        repo_names = _read_repo_names(workspace, sha)
        entries.append(GripCommitInfo(sha=sha, message=msg, repos=repo_names, timestamp=ts))

    return entries


def _read_repo_names(workspace: Path, commit_sha: str) -> list[str]:
    proc = _grip_git(workspace, "ls-tree", f"{commit_sha}:repos")
    if proc.returncode != 0:
        return []
    return [
        line.split("\t")[-1]
        for line in proc.stdout.strip().splitlines()
        if line.strip()
    ]


def grip_diff(workspace: Path, ref_a: str, ref_b: str) -> GripDiff:
    """Compare two grip commits and return changed/added/removed repos."""
    _validate_grip_repo(workspace)
    repos_a = _read_repo_state(workspace, ref_a)
    repos_b = _read_repo_state(workspace, ref_b)

    result = GripDiff()

    all_names = set(repos_a.keys()) | set(repos_b.keys())
    for name in sorted(all_names):
        if name in repos_a and name not in repos_b:
            result.removed.append(name)
        elif name not in repos_a and name in repos_b:
            result.added.append(name)
        else:
            old_commit = repos_a[name].get("commit", "")
            new_commit = repos_b[name].get("commit", "")
            if old_commit != new_commit:
                result.changed[name] = {
                    "old_commit": old_commit,
                    "new_commit": new_commit,
                }

    return result


def _read_repo_state(workspace: Path, ref: str) -> dict[str, dict[str, str]]:
    """Read all repo states from a grip commit."""
    proc = _grip_git(workspace, "ls-tree", f"{ref}:repos")
    if proc.returncode != 0:
        return {}

    repos: dict[str, dict[str, str]] = {}
    for line in proc.stdout.strip().splitlines():
        if not line.strip():
            continue
        name = line.split("\t")[-1]
        state: dict[str, str] = {}
        fields = _grip_git(workspace, "ls-tree", f"{ref}:repos/{name}")
        if fields.returncode == 0:
            for fline in fields.stdout.strip().splitlines():
                if not fline.strip():
                    continue
                fname = fline.split("\t")[-1]
                blob = _grip_git(workspace, "show", f"{ref}:repos/{name}/{fname}")
                if blob.returncode == 0:
                    state[fname] = blob.stdout.strip()
        repos[name] = state

    return repos


def grip_checkout(workspace: Path, ref: str) -> dict[str, str]:
    """Read a grip commit and checkout matching commits in workspace repos.

    Returns dict mapping repo name to commit SHA.
    """
    _validate_grip_repo(workspace)

    # Verify the ref resolves to a valid object
    verify = _grip_git(workspace, "cat-file", "-t", ref)
    if verify.returncode != 0:
        raise GripCorruptError(
            f"Ref '{ref}' does not resolve to a valid object in .grip/ repo."
        )

    repo_states = _read_repo_state(workspace, ref)
    result: dict[str, str] = {}

    for name, state in sorted(repo_states.items()):
        commit_sha = state.get("commit", "")
        if not commit_sha:
            continue
        result[name] = commit_sha

        repo_path = workspace / name
        if repo_path.is_dir():
            git(repo_path, "checkout", commit_sha)

    return result
