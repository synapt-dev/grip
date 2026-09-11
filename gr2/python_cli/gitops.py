from __future__ import annotations

import os
import subprocess
from pathlib import Path


_SSH_GITHUB_PREFIX = "git@github.com:"
_HTTPS_GITHUB_PREFIX = "https://github.com/"


def rewrite_ssh_github_url(url: str) -> str:
    """``git@github.com:OWNER/REPO(.git)`` -> ``https://github.com/OWNER/REPO.git``.

    Any other URL (a different host, a different scheme, an already-HTTPS
    GitHub URL) is returned unchanged. The host's SSH is
    1Password-managed and off-limits to agents, so a spec-declared SSH GitHub
    URL is otherwise permanently unreachable for gr2's own network operations.
    """
    if not url.startswith(_SSH_GITHUB_PREFIX):
        return url
    rest = url[len(_SSH_GITHUB_PREFIX):]
    if not rest.endswith(".git"):
        rest = f"{rest}.git"
    return f"{_HTTPS_GITHUB_PREFIX}{rest}"


def _ssh_rewrite_enabled() -> bool:
    value = os.environ.get("GR2_SYNC_REWRITE_SSH_URLS", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _effective_remote_url(url: str) -> str:
    """The URL gr2 should actually dial for ``url``: rewritten when it is an
    SSH GitHub URL and the rewrite is enabled (default on), else unchanged."""
    if _ssh_rewrite_enabled():
        return rewrite_ssh_github_url(url)
    return url


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def is_git_repo(path: Path) -> bool:
    if not path.exists():
        # subprocess.run(cwd=...) raises FileNotFoundError uncontrolled for an
        # absent cwd rather than returning a failed CompletedProcess; a missing
        # path is not a git repo, so this is the answer, not an exception.
        return False
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


def current_head_sha(path: Path) -> str | None:
    proc = git(path, "rev-parse", "HEAD")
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def commits_between(path: Path, old_sha: str | None, new_sha: str | None) -> int:
    if not new_sha:
        return 0
    if not old_sha:
        proc = git(path, "rev-list", "--count", new_sha)
    else:
        proc = git(path, "rev-list", "--count", f"{old_sha}..{new_sha}")
    if proc.returncode != 0:
        return 0
    try:
        return int(proc.stdout.strip() or "0")
    except ValueError:
        return 0


def conflicting_files(path: Path) -> list[str]:
    proc = git(path, "diff", "--name-only", "--diff-filter=U")
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def ensure_repo_cache(url: str, cache_repo_root: Path, *, local_source: Path | None = None) -> bool:
    """Ensure a local bare mirror exists for a repo URL.

    When the cache does not yet exist and ``local_source`` names an existing git
    checkout (a repo already materialized as a shared checkout next
    to the missing cache), the mirror is seeded from that LOCAL checkout -- no
    network involved -- and the mirror's ``origin`` remote is then repointed to
    the EFFECTIVE url (rewritten when it is an SSH GitHub url, see step 2 below)
    so it still tracks a remote that will actually work on the next refresh.
    Falls back to a network clone from the effective url when ``local_source``
    is absent or is not a git repo.

    An EXISTING cache self-heals the same way before refreshing: its origin is
    repointed to the effective url first (a cache seeded before this fix
    existed, or by a human whose own SSH works, must not keep failing on every
    later refresh), then ``remote update --prune`` runs.

    Returns True if a cache was created, False if it already existed and was refreshed.
    """
    effective_url = _effective_remote_url(url)

    if cache_repo_root.exists():
        if not is_git_dir(cache_repo_root):
            raise SystemExit(f"repo cache path exists but is not a git dir: {cache_repo_root}")

        if effective_url != url:
            remote_proc = subprocess.run(
                ["git", "--git-dir", str(cache_repo_root), "remote", "set-url", "origin", effective_url],
                capture_output=True,
                text=True,
                check=False,
            )
            if remote_proc.returncode != 0:
                raise SystemExit(
                    f"failed to repoint repo cache origin to {effective_url}:\n"
                    f"{remote_proc.stderr or remote_proc.stdout}"
                )

        proc = subprocess.run(
            ["git", "--git-dir", str(cache_repo_root), "remote", "update", "--prune"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise SystemExit(f"failed to refresh repo cache {cache_repo_root}:\n{proc.stderr or proc.stdout}")
        return False

    seed_from_local = local_source is not None and is_git_repo(local_source)
    seed_source = str(local_source) if seed_from_local else effective_url

    cache_repo_root.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "clone", "--mirror", seed_source, str(cache_repo_root)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"failed to seed repo cache {seed_source} -> {cache_repo_root}:\n{proc.stderr or proc.stdout}")

    if seed_from_local:
        # The mirror's origin must be the URL that will actually work for a
        # later refresh (`remote update`), not necessarily the raw spec url --
        # an unreachable SSH url here would just move today's seed failure
        # to every future refresh instead.
        remote_proc = subprocess.run(
            ["git", "--git-dir", str(cache_repo_root), "remote", "set-url", "origin", effective_url],
            capture_output=True,
            text=True,
            check=False,
        )
        if remote_proc.returncode != 0:
            raise SystemExit(
                f"seeded repo cache from local checkout {local_source} but failed to repoint "
                f"origin to {effective_url}:\n{remote_proc.stderr or remote_proc.stdout}"
            )
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


def fetch_repo(repo_root: Path, remote: str = "origin") -> None:
    """Fetch new refs for ``remote`` into ``repo_root``.

    When the remote's CONFIGURED url is an SSH GitHub url (unreachable to an
    agent), the fetch is routed through the rewritten HTTPS url
    with an explicit full refspec instead -- so the standard
    ``<remote>/<branch>`` tracking refs still land where every caller expects
    them. The checkout's OWN ``git config`` is never written to: no
    ``remote set-url`` runs here, ever.
    """
    origin_url = remote_origin_url(repo_root) if remote == "origin" else None
    effective_url = _effective_remote_url(origin_url) if origin_url is not None else None

    if effective_url is not None and effective_url != origin_url:
        proc = git(repo_root, "fetch", effective_url, f"+refs/heads/*:refs/remotes/{remote}/*")
    else:
        proc = git(repo_root, "fetch", remote)
    if proc.returncode != 0:
        raise SystemExit(
            f"failed to fetch from {remote} in {repo_root}:\n{proc.stderr or proc.stdout}"
        )


def ahead_behind(repo_root: Path, tracking_ref: str) -> tuple[int, int]:
    """(ahead, behind) of HEAD relative to ``tracking_ref`` -- commits only HEAD
    has, commits only the tracking ref has. ``(0, 0)`` when the comparison
    cannot be made (e.g. the tracking ref does not exist yet)."""
    proc = git(repo_root, "rev-list", "--left-right", "--count", f"HEAD...{tracking_ref}")
    if proc.returncode != 0:
        return (0, 0)
    parts = proc.stdout.split()
    if len(parts) != 2:
        return (0, 0)
    try:
        return (int(parts[0]), int(parts[1]))
    except ValueError:
        return (0, 0)


def fast_forward_to(repo_root: Path, tracking_ref: str) -> bool:
    """Fast-forward the checked-out branch to ``tracking_ref``. Refuses (git's
    own refusal, not ours) if the branch is not a strict ancestor of the
    tracking ref -- only a CLEAN, non-diverged checkout may move;
    a diverged one must be reported, never force-moved. Returns True if HEAD
    actually moved."""
    before = current_head_sha(repo_root)
    proc = git(repo_root, "merge", "--ff-only", tracking_ref)
    if proc.returncode != 0:
        raise SystemExit(
            f"failed to fast-forward {repo_root} to {tracking_ref}:\n{proc.stderr or proc.stdout}"
        )
    return current_head_sha(repo_root) != before


def probe_remote(url: str) -> tuple[bool, str]:
    """One-shot reachability probe for ``url`` (rewritten per
    ``_effective_remote_url`` first, same rewrite as step 2 above). Returns
    ``(reachable, detail)`` -- detail is empty on success, the git
    stderr/stdout on failure. Used by ``sync status`` (never ``sync run``'s
    own internal plan-building) to surface a refused remote as an ISSUE
    before ``sync run`` would hit it mid-operation."""
    effective_url = _effective_remote_url(url)
    proc = subprocess.run(
        ["git", "ls-remote", effective_url],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0:
        return True, ""
    return False, (proc.stderr or proc.stdout).strip()


def is_git_dir(path: Path) -> bool:
    proc = subprocess.run(
        ["git", "--git-dir", str(path), "rev-parse", "--is-bare-repository"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _derive_workspace_root(source_repo_root: Path) -> Path:
    """The nearest ancestor holding a ``.grip`` directory is the workspace root;
    used to locate the object cache when a caller does not pass it. Falls back to
    the source's parent so the reference-clone path still runs (with no cache)
    outside a materialized workspace."""
    for candidate in (source_repo_root, *source_repo_root.parents):
        if (candidate / ".grip").is_dir():
            return candidate
    return source_repo_root.parent


def ensure_lane_checkout(
    *,
    source_repo_root: Path,
    target_repo_root: Path,
    branch: str,
    workspace_root: Path | None = None,
    seed_commit: str | None = None,
) -> bool:
    """Ensure a real, state-isolated lane checkout exists (grip#807).

    The lane is materialized as an INDEPENDENT reference clone, never a
    ``git worktree add``: two lanes checking out the same branch must not share
    refs, HEAD, reflogs, index, locks, config, or working-tree state. The clone
    contract lives in the single clone_exec seam; this is the thin lane entry to
    it. Returns True on first materialization, False when a valid lane is reused.
    """
    # Lazy import: clone_exec imports gitops, so a module-level import here would
    # be a cycle.
    from . import clone_exec

    if workspace_root is None:
        workspace_root = _derive_workspace_root(source_repo_root)

    return clone_exec.materialize_lane_clone(
        source_repo_root=source_repo_root,
        dest=target_repo_root,
        branch=branch,
        seed_commit=seed_commit,
        workspace_root=workspace_root,
    )


def checkout_branch(repo_root: Path, branch: str) -> None:
    proc = git(repo_root, "checkout", branch)
    if proc.returncode != 0:
        raise SystemExit(f"failed to checkout {branch} in {repo_root}:\n{proc.stderr or proc.stdout}")


def current_branch(repo_root: Path) -> str:
    proc = git(repo_root, "branch", "--show-current")
    if proc.returncode != 0:
        raise SystemExit(f"failed to determine current branch in {repo_root}:\n{proc.stderr or proc.stdout}")
    return proc.stdout.strip()


def stash_if_dirty(repo_root: Path, message: str) -> bool:
    if not repo_dirty(repo_root):
        return False
    proc = git(repo_root, "stash", "push", "-u", "-m", message)
    if proc.returncode != 0:
        raise SystemExit(f"failed to stash dirty work in {repo_root}:\n{proc.stderr or proc.stdout}")
    return True


def discard_if_dirty(repo_root: Path) -> bool:
    if not repo_dirty(repo_root):
        return False
    proc = git(repo_root, "reset", "--hard", "HEAD")
    if proc.returncode != 0:
        raise SystemExit(f"failed to discard tracked changes in {repo_root}:\n{proc.stderr or proc.stdout}")
    proc = git(repo_root, "clean", "-fd")
    if proc.returncode != 0:
        raise SystemExit(f"failed to discard untracked changes in {repo_root}:\n{proc.stderr or proc.stdout}")
    return True
