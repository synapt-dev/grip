from __future__ import annotations

import os
import subprocess
from pathlib import Path


_SSH_GITHUB_PREFIX = "git@github.com:"
_HTTPS_GITHUB_PREFIX = "https://github.com/"


class GitMissingError(Exception):
    """The git executable is not on PATH (a fresh container without git). One
    sentence naming git replaces a traceback — or worse, a False answer that
    reads as 'this is not a git repository' on a machine that simply has no
    git."""


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


def git(
    cwd: Path, *args: str, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    """Run git. `timeout` bounds the call; an expiry is a FAILED result, not a raise.

    A timeout is a way of not finding out, and callers here already have a shape for
    that: a non-zero return code. Returning `returncode=124` (the conventional
    timeout code) with an empty stdout keeps them total, and keeps an unanswered
    remote on the same channel as a refused one instead of adding an exception every
    caller must remember to catch. Measured need: `ls-remote` against a peer that
    accepts and never replies blocks indefinitely, which is how a bound became
    necessary rather than nice.
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            ["git", *args],
            124,
            stdout="",
            stderr=f"git timed out after {timeout}s",
        )
    except FileNotFoundError as exc:
        # The spawn itself failed because the git EXECUTABLE is absent
        # (exc.filename == "git"). A nonexistent cwd raises FileNotFoundError
        # naming the cwd, which is a different state and stays untouched.
        if exc.filename == "git":
            raise GitMissingError(
                "git is not on PATH; install git, then run the verb again"
            ) from None
        raise


def is_git_repo(path: Path) -> bool:
    if not path.is_dir():
        # A FILE as cwd raises NotADirectoryError out of subprocess.run rather
        # than returning a failed CompletedProcess (measured: a unit home
        # holding a plain `unit.toml` crashed every single-repo verb through
        # repos_under). A non-directory is not a git repo, so this is the
        # answer, not an exception.
        return False
    try:
        proc = git(path, "rev-parse", "--is-inside-work-tree")
    except PermissionError:
        # A directory the caller cannot enter (chmod 000) raises
        # PermissionError out of the subprocess cwd; a directory that cannot
        # be probed is not answerable as a repository, so False is the
        # answer, not a traceback. OSError would be wider: it would swallow
        # the FileNotFoundError for a missing git EXECUTABLE and turn a
        # no-git machine into a false 'not a git repository' sentence
        # (Stromus, m_9341d8d1); GitMissingError propagates instead.
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def is_bare_git_repo(path: Path) -> bool:
    """A bare repository (HEAD + a valid git dir, no work tree). The shape a
    stranger uses as a local upstream; the repo scanner skipped it before."""
    if not path.is_dir():
        # Same non-directory guard as is_git_repo (B1 of the alpha-2 gate).
        return False
    try:
        proc = git(path, "rev-parse", "--is-bare-repository")
    except PermissionError:
        # Same unenterable-directory answer as is_git_repo.
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def is_repo_root(path: Path) -> bool:
    """This path ITSELF is the top of a work tree -- not merely inside one.

    `is_git_repo` above answers the second question, and answers it
    truthfully, from the ENCLOSING repository. The two diverge for any plain
    directory that happens to sit inside a checkout, which is exactly what a
    staging directory or a unit member path is, so a caller meaning "is there
    already a clone HERE" got True for an empty directory and skipped its work.
    `--show-toplevel` is the discriminator: it names the root the path resolves
    into, and comparing it to the path is the question this answers. Both sides
    are resolved so a symlinked temp root (/var -> /private/var) does not read
    as a mismatch.
    """
    if not path.is_dir():
        return False
    try:
        proc = git(path, "rev-parse", "--show-toplevel")
    except PermissionError:
        # Same unenterable-directory answer as is_git_repo: a directory that
        # cannot be probed is not answerable as a repository.
        return False
    if proc.returncode != 0:
        return False
    try:
        return Path(proc.stdout.strip()).resolve() == path.resolve()
    except OSError:
        return False


def is_git_repository(path: Path) -> bool:
    """A repository of either shape: a work tree or a bare repo."""
    return is_git_repo(path) or is_bare_git_repo(path)


def repo_path_state(path: Path) -> str:
    """What a member path actually holds: ``repo_root``, ``empty_placeholder``, or ``neither``.

    Three answers, because callers decide differently on each one, and collapsing
    them into a boolean is how the read-through survived four sites: the
    validator skips a placeholder and reports ``neither``; the sync planner
    plans a clone into a placeholder and reports ``neither``; the cache seed
    treats both non-repo answers as "no local source"; merge verification
    refuses both, because the DAG is unavailable for either. Asking
    `is_git_repo` here was the defect: it answers --is-inside-work-tree, which
    is true for any directory inside a checkout, so a plain directory at a
    member path read as the enclosing repository and every site above skipped
    its work.

    ``empty_placeholder`` is a directory that can be READ and holds nothing:
    the ordinary state of a freshly cloned superproject's member paths, where
    `git clone` creates the submodule mount points and leaves them empty until
    `submodule update --init`. An UNREADABLE directory is ``neither``, not an
    empty one: we cannot know what it holds, and the code this helper replaces
    (`is_git_repo`, which catches PermissionError and answers False) reported a
    conflict there, so the conflict is what must keep firing. A path that does
    not exist at all is ``neither`` too; callers that must distinguish missing
    from present-and-wrong ask `exists()` first, exactly as they do today --
    this helper answers the question about what a PRESENT path holds.
    """
    if not path.is_dir():
        # A file at a member path, or a path that does not exist at all:
        # neither is a repository nor an unfilled placeholder. The sites keep
        # their own exists() branches for the plan-the-work decisions.
        return "neither"
    try:
        empty = not any(path.iterdir())
    except OSError:
        # Unreadable: not an empty one, and not answerable as a repository.
        return "neither"
    if is_repo_root(path):
        return "repo_root"
    return "empty_placeholder" if empty else "neither"


class OutsideRepoError(Exception):
    """A single-repo verb was run from a path that is not a git work tree."""


def repos_under(path: Path, limit: int = 5) -> list[Path]:
    """Git WORK TREE repositories directly under `path` (and `path` itself),
    nearest name order, capped so a refusal line stays a line. Only
    directories are probed: a regular file as cwd raises NotADirectoryError
    out of the git probes. Work trees only: the consumers of this listing
    (the single-repo refusals) name repos the verb can run inside, and those
    verbs need a work tree — a bare repository would be an unusable
    suggestion. An unenterable child (chmod 000) is skipped: the helpers
    answer False for it."""
    found: list[Path] = []
    if is_git_repo(path):
        found.append(path)
    if not path.is_dir():
        return found
    for child in sorted(path.iterdir()):
        if len(found) >= limit:
            break
        if not child.is_dir():
            continue
        if child.name.startswith("."):
            continue
        if is_git_repo(child):
            found.append(child)
    return found


def require_git_repo(path: Path, verb: str) -> None:
    """One-sentence refusal for a single-repo verb run from outside a git work
    tree, naming what the verb needs and listing the repos found under the
    cwd. Measured failures being replaced: git's own raw text ("fatal: not a
    git repository"), git's full usage dump from the commit path, and, inside
    a bare repository, git's "this operation must be run in a work tree".
    These verbs need a WORK TREE, not any repository."""
    if is_git_repo(path):
        return
    if is_bare_git_repo(path):
        found = repos_under(path)
        listing = f" Repositories found under it: {', '.join(item.name for item in found)}." if found else ""
        raise OutsideRepoError(
            f"gr2 {verb} runs inside one repository of the workspace; "
            f"{path} is a bare repository, and this verb needs a work tree."
            f"{listing} Run inside a work tree, or pass --repo-path"
        )
    found = repos_under(path)
    if found:
        listing = ", ".join(item.name for item in found)
        raise OutsideRepoError(
            f"gr2 {verb} runs inside one repository of the workspace; "
            f"{path} is not a git repository (repositories found under it: "
            f"{listing}; run inside one, or pass --repo-path)"
        )
    raise OutsideRepoError(
        f"gr2 {verb} runs inside one repository of the workspace; "
        f"{path} is not a git repository (no repositories found under it)"
    )


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

    # SEAM DEFENSE, not just the callers' fix: the state helper decides
    # whether a caller-supplied source is a repository, because this function
    # is the one that clones from it. The read-through let a plain directory
    # be passed as ``local_source`` (is_git_repo answered for the ENCLOSING
    # checkout) and the seed then failed with git's misleading
    # "repository '<plain dir>' does not exist". A plain directory and an
    # empty placeholder are both "no local source": the seed falls back to
    # the effective url, which is the one thing here that can actually
    # resolve.
    seed_from_local = local_source is not None and repo_path_state(local_source) == "repo_root"
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
    # "Is there already a clone AT this path", not "is this path inside a
    # repository". `is_git_repo` answers the second question -- truthfully, from
    # the ENCLOSING repository -- so a directory merely inside a workspace root
    # read as an existing clone and this function returned False WITHOUT
    # cloning. Measured: a staging directory created inside a superproject came
    # back "already a repo", the clone was skipped, and the pin check then ran
    # against a repository that was never created, refusing over an empty
    # directory.
    if target_repo_root.exists() and is_repo_root(target_repo_root):
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


def checkout_declared_pin(repo_root: Path, pin: str, *, member: str) -> None:
    """Put a member at the commit the root declares, after a clone.

    A clone lands on the REMOTE'S DEFAULT BRANCH TIP, which for a superproject
    member is not where the root says it is: the gitlink pins a commit, and that
    pin is the declaration the whole feature exists to record. Measured on
    ``git-training-open/submodule-example``: after ``workspace init
    --from-superproject`` and ``materialize``, ``example1`` sat on ``master`` at
    ``ceed35b970e2`` while the root pinned ``065be099e95a`` -- and
    ``jabberwocky`` looked correct only because that repo's master tip happens
    to equal its pin. So a fixture built on the second member passes while doing
    the wrong thing, which is why the test uses the pair.

    TWO EDGES, decided rather than assumed:

    - **A pin the clone cannot reach** (a shallow or depth-limited clone). Fetch
      that one object by sha. If the server refuses, REFUSE with an error naming
      the pin, the member and the command to try by hand. **Never fall back to
      the default tip**: a silent fallback is exactly the defect being fixed, so
      the failure mode here is loud on purpose.
    - **An empty pin** is not this function's business; the caller skips it. A
      member with no pin stays on its default branch and the status calls it
      UNPINNED rather than at-pin.
    """
    # A caller SHOULD skip an unpinned member, and this no-op is what makes the
    # contract safe when one does not: without it an empty pin falls into the
    # unreachable-pin path below and refuses with a message naming an empty pin,
    # which is a worse answer than doing nothing. The test that pins the contract
    # is what found this -- the docstring promised a no-op the code did not do.
    if not pin:
        return

    have = lambda: git(repo_root, "cat-file", "-e", f"{pin}^{{commit}}").returncode == 0
    if not have():
        # `--depth 1` ONLY when the clone is already shallow. Passing it
        # unconditionally re-shallowed a FULL clone, which throws away history
        # the caller never asked to lose and is a worse outcome than the missing
        # object it was meant to fetch.
        fetch_args = ["fetch"]
        shallow = git(repo_root, "rev-parse", "--is-shallow-repository").stdout.strip() == "true"
        if shallow:
            fetch_args.extend(["--depth", "1"])
        fetch_args.extend(["origin", pin])
        git(repo_root, *fetch_args)
        if not have():
            raise SystemExit(
                f"member '{member}' declares pin {pin}, the clone cannot reach that commit, and "
                f"fetching it by sha from origin did not provide it. Try "
                f"`git -C {repo_root} fetch --depth 1 origin {pin}` by hand to see why. This verb "
                "will not leave the member on its default branch instead: that is a commit the "
                "root never declared."
            )
    checked = git(repo_root, "checkout", "--detach", pin)
    if checked.returncode != 0:
        raise SystemExit(
            f"member '{member}' declares pin {pin} but checking it out failed:\n"
            f"{checked.stderr or checked.stdout}"
        )


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
