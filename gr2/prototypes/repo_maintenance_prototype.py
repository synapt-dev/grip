#!/usr/bin/env python3
"""Prototype planner for gr2 repo maintenance policy.

This is intentionally separate from gr2 apply. It answers:
- which repos are missing and need clone/materialization
- which repos are safe to fast-forward
- which repos require explicit human intervention
- where autostash would be required to proceed

The goal is to keep workspace structure convergence (`gr2 apply`) separate
from repo state convergence (`gr2 repo sync` / `gr2 repo pull`).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import stat
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class RepoSpec:
    name: str
    path: str
    url: str


@dataclasses.dataclass(frozen=True)
class UnitSpec:
    name: str
    path: str
    repos: list[str]


@dataclasses.dataclass(frozen=True)
class WorkspaceSpec:
    schema_version: int
    workspace_name: str
    repos: list[RepoSpec]
    units: list[UnitSpec]


@dataclasses.dataclass(frozen=True)
class RepoTarget:
    scope: str
    target_name: str
    repo_name: str
    path: Path
    url: str


@dataclasses.dataclass(frozen=True)
class RepoPolicy:
    sync_mode: str
    dirty_policy: str
    tracked_branch: str | None


@dataclasses.dataclass(frozen=True)
class RepoStatus:
    exists: bool
    is_git_repo: bool
    branch: str | None
    upstream: str | None
    dirty: bool
    ahead: int
    behind: int
    detached: bool
    linked_worktree: bool = False
    git_dir_is_symlink: bool = False


@dataclasses.dataclass(frozen=True)
class PlannedAction:
    target: RepoTarget
    action: str
    reason: str
    status: RepoStatus
    policy: RepoPolicy

    def as_dict(self) -> dict[str, object]:
        return {
            "scope": self.target.scope,
            "target": self.target.target_name,
            "repo": self.target.repo_name,
            "path": str(self.target.path),
            "action": self.action,
            "reason": self.reason,
            "status": dataclasses.asdict(self.status),
            "policy": dataclasses.asdict(self.policy),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prototype gr2 repo maintenance planner")
    parser.add_argument("workspace_root", type=Path, help="gr2 workspace root")
    parser.add_argument(
        "--spec",
        type=Path,
        help="path to workspace_spec.toml (defaults to <workspace>/.grip/workspace_spec.toml)",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        help="optional TOML policy file for branch/sync defaults",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of a table",
    )
    return parser.parse_args()


def read_workspace_spec(spec_path: Path) -> WorkspaceSpec:
    with spec_path.open("rb") as fh:
        raw = tomllib.load(fh)

    repos = [
        RepoSpec(name=item["name"], path=item["path"], url=item["url"])
        for item in raw.get("repos", [])
    ]
    units = [
        UnitSpec(
            name=item["name"],
            path=item["path"],
            repos=list(item.get("repos", [])),
        )
        for item in raw.get("units", [])
    ]
    return WorkspaceSpec(
        # `workspace init` writes no schema_version, and every other reader
        # uses .get; default to 1 (the only schema shipped) rather than raise.
        schema_version=raw.get("schema_version", 1),
        workspace_name=raw["workspace_name"],
        repos=repos,
        units=units,
    )


def read_policy(policy_path: Path | None) -> dict[str, object]:
    if policy_path is None:
        return {}
    with policy_path.open("rb") as fh:
        return tomllib.load(fh)


def derive_targets(workspace_root: Path, spec: WorkspaceSpec) -> list[RepoTarget]:
    shared_targets = [
        RepoTarget(
            scope="shared",
            target_name=repo.name,
            repo_name=repo.name,
            path=workspace_root / repo.path,
            url=repo.url,
        )
        for repo in spec.repos
    ]

    repo_map = {repo.name: repo for repo in spec.repos}
    unit_targets: list[RepoTarget] = []
    for unit in spec.units:
        for repo_name in unit.repos:
            repo = repo_map[repo_name]
            unit_targets.append(
                RepoTarget(
                    scope="unit",
                    target_name=unit.name,
                    repo_name=repo_name,
                    path=workspace_root / unit.path / repo_name,
                    url=repo.url,
                )
            )

    return shared_targets + unit_targets


def policy_for(target: RepoTarget, policy_doc: dict[str, object]) -> RepoPolicy:
    defaults = policy_doc.get("defaults", {})
    repos = policy_doc.get("repos", {})

    if not isinstance(defaults, dict):
        defaults = {}
    if not isinstance(repos, dict):
        repos = {}

    repo_overrides = repos.get(target.repo_name, {})
    if not isinstance(repo_overrides, dict):
        repo_overrides = {}

    if target.scope == "shared":
        sync_mode = str(repo_overrides.get("sync", defaults.get("shared_sync", "ff-only")))
    else:
        sync_mode = str(repo_overrides.get("sync", defaults.get("unit_sync", "explicit")))

    dirty_policy = str(repo_overrides.get("dirty", defaults.get("dirty", "block")))
    tracked_branch = repo_overrides.get("tracked_branch", defaults.get("tracked_branch"))
    tracked_branch = str(tracked_branch) if tracked_branch is not None else None

    return RepoPolicy(
        sync_mode=sync_mode,
        dirty_policy=dirty_policy,
        tracked_branch=tracked_branch,
    )


def run_git(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_path,
        check=False,
        capture_output=True,
        text=True,
    )


ASIDE_KEEP = "KEEP"
ASIDE_DROP = "DROP"
ASIDE_UNREADABLE = "UNREADABLE"
ASIDE_SUSPECT = "SUSPECT"

# The ambient repo-identity variables. With any of these inherited, `git -C <aside>`
# answers about a DIFFERENT tree -- rc 0, empty output -- and the tree it answered for is
# not the one being judged. Same class as a stale index root redirecting a recall read.
_REPO_IDENTITY_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
)


def _git_scrubbed(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """`run_git` with the ambient repo-identity variables removed.

    `run_git` is right for every other caller, where the subject is the caller's own cwd.
    Here the subject is a directory git has to be told about explicitly, and an inherited
    `GIT_DIR`/`GIT_WORK_TREE` pair silently redirects the question somewhere else.
    """
    env = {k: v for k, v in os.environ.items() if k not in _REPO_IDENTITY_ENV}
    return subprocess.run(
        ["git", *args],
        cwd=repo_path,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def aside_disposition(aside: Path) -> tuple[str, int, str]:
    """Disposition of the moved-aside original: KEEP / DROP / UNREADABLE / SUSPECT.

    MUST be called BEFORE the prune. Pruning deletes the registration this copy's `.git`
    pointer names, after which git cannot answer for it at all -- and an empty answer from
    a dead instrument reads exactly like a clean tree.

    Two independent instruments, in this order, because each covers the other's blind spot:

    * the INDEX AUDIT (`ls-files -v`) runs FIRST, and the order is load-bearing. A
      lowercase tag means assume-unchanged and `S` means skip-worktree; either one
      switches off the comparison itself, so no `status` flag can reach that path and its
      silence proves nothing. If the comparison ran first and returned a clean DROP, the
      audit would never run and this bypass would return in full. The audit is also the
      weaker instrument alone -- under a per-worktree `config.worktree` redirect
      `ls-files` goes quiet while `status` fails loudly (rc 128 -> UNREADABLE) -- so each
      one covers the other only in this order.
    * then the COMPARISON, WIDE: `--porcelain --ignored -uall`. Wide on the DELETE side
      only: we delete a tree only when it is provably empty of anything git does not
      already carry. An ignored file is invisible to a narrow `--porcelain` in steady
      state, with no race, and a `status.showUntrackedFiles=no` in shared config hides an
      untracked draft from every check except one that passes `-uall` explicitly -- a
      command-line flag outranks config, which is the durable reason to pass it every time
      rather than inherit a default someone else can set.

    The refusal gates upstream stay narrow on purpose, and the asymmetry is the point: a
    `*.env` must not block a conversion, it must only stop a deletion. The cost is that an
    aside is kept more often, at a NAMED printed path, and a kept directory is recoverable
    where a deleted one is not.
    """
    bits = _git_scrubbed(aside, "ls-files", "-v")
    if bits.returncode != 0:
        lines = (bits.stderr or "").strip().splitlines()
        return (ASIDE_UNREADABLE, 0, lines[-1] if lines else "git refused to read the tree")

    hidden = [
        line for line in bits.stdout.splitlines() if line[:1].islower() or line[:1] == "S"
    ]
    if hidden:
        return (
            ASIDE_SUSPECT,
            len(hidden),
            f"the index marks {len(hidden)} path(s) assume-unchanged or skip-worktree, "
            "so the comparison is switched off for them and its silence proves nothing",
        )

    out = _git_scrubbed(aside, "status", "--porcelain", "--ignored", "-uall")
    if out.returncode != 0:
        lines = (out.stderr or "").strip().splitlines()
        return (ASIDE_UNREADABLE, 0, lines[-1] if lines else "git refused to read the tree")
    n = sum(1 for line in out.stdout.splitlines() if line.strip())
    if n == 0:
        return (ASIDE_DROP, 0, "")
    return (ASIDE_KEEP, n, out.stdout.strip())


def is_git_dir_symlink(path: Path) -> bool:
    """A repo path's ``.git`` IS a symlink into another clone's git state.

    A different isolation violation from a linked worktree -- the repo does
    not own its own git state either way, but the mechanism and the fix are
    different (remove the symlink and re-clone, vs. convert a worktree).
    Checked FIRST in ``is_linked_worktree``'s caller, same order as
    ``clone_exec.py``'s ``verify_clone_isolation``, because a symlink also
    fails the ``not S_ISDIR`` test below and must not double-report as both.
    """
    git_path = path / ".git"
    try:
        mode = git_path.lstat().st_mode
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(mode)


def is_linked_worktree(path: Path) -> bool:
    """A repo path IS a linked worktree (``git worktree add``), not an own
    clone and not a symlinked ``.git`` (see ``is_git_dir_symlink``).

    Deliberately does not consult ``git rev-parse --is-inside-work-tree`` --
    that answers TRUE inside a linked worktree just as it does inside a real
    clone (the same trap ``clone_exec.py``'s ``verify_clone_isolation``
    docstring names for gr2's lane-clone path), so it can't distinguish the
    two. A plain clone's ``.git`` is a directory; ``git worktree add``
    replaces it with a text file (``gitdir: <path>``). lstat, not
    ``Path.is_dir()``, which follows a symlink.
    """
    git_path = path / ".git"
    try:
        mode = git_path.lstat().st_mode
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(mode):
        return False
    return not stat.S_ISDIR(mode)


class ConvertCloneError(Exception):
    """Raised when a linked-worktree-to-clone conversion is refused."""


def _lstat_summary(git_path: Path) -> dict[str, object]:
    try:
        mode = git_path.lstat().st_mode
    except FileNotFoundError:
        return {"exists": False}
    return {
        "exists": True,
        "is_dir": stat.S_ISDIR(mode),
        "is_symlink": stat.S_ISLNK(mode),
        "is_regular_file": stat.S_ISREG(mode),
    }


def _origin_url(path: Path) -> str | None:
    """The repo's ``origin`` URL, or None when it has no origin."""
    proc = run_git(path, "remote", "get-url", "origin")
    if proc.returncode != 0:
        return None
    url = proc.stdout.strip()
    return url or None


def restore_original(target: Path, aside: Path) -> bool:
    """Put the moved-aside original back at ``target``. Destroys nothing.

    PARAMETERISED ON PURPOSE, so the witness runs the same bytes the caller
    does rather than a copy free to drift.

    An occupant at ``target`` is MOVED ASIDE AND NAMED, never deleted: this is
    the recovery path, so by definition it runs once something has already
    gone wrong, and the first version of the hand-built equivalent did an
    unguarded ``rm -rf`` here and then printed "(nothing lost)". A recovery
    path carrying the defect its forward path was fixed for is the worst
    place for it to live.
    """
    target = Path(target)
    aside = Path(aside)
    if target.exists():
        keep = target.with_name(f"{target.name}.interloper.{os.getpid()}")
        try:
            os.rename(target, keep)
        except OSError:
            return False
        print(
            f"  NOTE: something occupied {target} during recovery; moved to {keep} (NOT deleted)",
            file=sys.stderr,
        )
    try:
        os.rename(aside, target)
    except OSError:
        print(
            f"  CANNOT RESTORE. Your original tree is at: {aside}",
            file=sys.stderr,
        )
        return False
    return True


def swap_in_place(staged: Path, target: Path) -> None:
    """Move ``staged`` onto ``target``, refusing to nest.

    ``mv src dst`` with dst PRESENT nests src inside it, leaving the path a
    non-git directory and reporting the failure only after the damage
   . If anything recreated the target inside the
    window -- gr1's materializer, a hook, a peer -- refuse and leave both
    copies findable.
    """
    if Path(target).exists():
        raise ConvertCloneError(
            f"{target} was recreated during the conversion; refusing to move "
            f"onto it (the replacement is at {staged})"
        )
    shutil.move(str(staged), str(target))


def convert_worktree_to_clone(path: Path) -> dict[str, object]:
    """Convert a linked worktree into an own clone, in place.

    Six steps, proven once by hand against a scratch fixture before this
    function existed:

    1. Refuse anything that is not GENUINELY a linked worktree -- a symlinked
       ``.git`` is a different isolation violation (see
       ``is_git_dir_symlink``) with a different fix (remove the symlink and
       re-clone, not convert), and an own clone needs no conversion.
    2. Refuse a dirty tree (tracked or untracked changes) BEFORE any mutation.
    3. Resolve ``--git-common-dir`` to find the canonical repo this worktree
       is linked to.
    4. Clone fresh from the canonical repo over ``file://`` into a staging
       path, and check out the same branch there.
    5. Verify the staging clone's isolation (a real ``.git`` directory, no
       nested ``.git/worktrees``, HEAD matching the original exactly) BEFORE
       touching the original worktree at all.
    6. Only then: ``git worktree remove`` the original (removing it from the
       canonical repo's registry, not merely hiding it), ``git worktree
       prune``, and move the verified staging clone into the original path.

    Returns a receipt: old common-dir, branch, head sha, and .git lstat
    before and after -- the witness that the conversion actually happened,
    at the same path, not just that no error was raised.
    """
    path = path.resolve()
    git_path = path / ".git"

    if is_git_dir_symlink(path):
        raise ConvertCloneError(
            f"{path}: .git is a symlink into another clone's git state, not a "
            "linked worktree -- remove the symlink and re-clone instead"
        )

    if not is_linked_worktree(path):
        raise ConvertCloneError(f"{path}: not a linked worktree -- nothing to convert")

    before = _lstat_summary(git_path)

    status = run_git(path, "status", "--porcelain")
    if status.stdout.strip():
        raise ConvertCloneError(f"{path}: working tree is dirty, refusing to convert")

    common_dir_proc = run_git(path, "rev-parse", "--git-common-dir")
    if common_dir_proc.returncode != 0:
        raise ConvertCloneError(
            f"{path}: could not resolve --git-common-dir: {common_dir_proc.stderr.strip()}"
        )
    common_dir = Path(common_dir_proc.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = (path / common_dir).resolve()
    canonical_repo_root = common_dir.parent

    branch_proc = run_git(path, "rev-parse", "--abbrev-ref", "HEAD")
    branch = branch_proc.stdout.strip()
    if branch_proc.returncode != 0 or branch == "HEAD":
        raise ConvertCloneError(f"{path}: on a detached HEAD, refusing to convert")

    head_sha = run_git(path, "rev-parse", "HEAD").stdout.strip()

    # A worktree's commits live in the OWNER'S object store, which
    # has been acting as an accidental backup nobody designed. Converting to a
    # clone removes that fallback, so this is the last moment at which
    # "unpushed" is cheap to say.
    # HEAD is explicit on purpose: `git log --not --remotes` with no positive
    # ref yields NOTHING -- git has only exclusions to walk -- so the check
    # could never fire. Measured: a desk with one commit on no remote
    # reported 0 until HEAD was named.
    unmerged = run_git(path, "log", "--oneline", "HEAD", "--not", "--remotes")
    unpushed_commits = len([ln for ln in unmerged.stdout.splitlines() if ln.strip()])
    if unpushed_commits:
        print(
            f"  WARNING: {path} carries {unpushed_commits} commit(s) on no "
            "remote. After conversion this clone is their only copy -- push "
            "before relying on it.",
            file=sys.stderr,
        )

    # Read the ORIGINAL's origin before anything is mutated. The
    # staging clone's own origin will point at the owner's directory, because
    # that is what `git clone <path>` sets, so leaving it is a .git coupling
    # traded for an origin coupling -- the lane doing the opposite of its
    # purpose while reporting success.
    original_origin = _origin_url(path)

    staging_path = path.parent / f".convert-staging-{path.name}"
    if staging_path.exists():
        raise ConvertCloneError(f"{staging_path}: staging path already exists")

    clone_proc = subprocess.run(
        ["git", "clone", "-q", f"file://{canonical_repo_root}", str(staging_path)],
        cwd=path.parent,
        capture_output=True,
        text=True,
    )
    if clone_proc.returncode != 0:
        raise ConvertCloneError(
            f"clone from {canonical_repo_root} failed: {clone_proc.stderr.strip()}"
        )

    checkout_proc = run_git(staging_path, "checkout", "-q", branch)
    if checkout_proc.returncode != 0:
        shutil.rmtree(staging_path)
        raise ConvertCloneError(
            f"checkout of {branch} in the staging clone failed: {checkout_proc.stderr.strip()}"
        )

    # The clone's origin is the owner's directory. Restore the
    # original's origin, or REMOVE it when the original had none -- inventing
    # a coupling is worse than having no remote.
    if original_origin is not None:
        set_proc = run_git(staging_path, "remote", "set-url", "origin", original_origin)
        if set_proc.returncode != 0:
            shutil.rmtree(staging_path)
            raise ConvertCloneError(
                f"could not restore origin {original_origin}: {set_proc.stderr.strip()}"
            )
    else:
        run_git(staging_path, "remote", "remove", "origin")

    staging_git = staging_path / ".git"
    staging_summary = _lstat_summary(staging_git)
    if not staging_summary["is_dir"]:
        shutil.rmtree(staging_path)
        raise ConvertCloneError("staging clone's .git is not a directory -- refusing to proceed")
    if (staging_git / "worktrees").exists():
        shutil.rmtree(staging_path)
        raise ConvertCloneError("staging clone hosts linked worktrees -- refusing to proceed")
    staging_head = run_git(staging_path, "rev-parse", "HEAD").stdout.strip()
    if staging_head != head_sha:
        shutil.rmtree(staging_path)
        raise ConvertCloneError(
            f"staging clone HEAD {staging_head} != original {head_sha} -- refusing to proceed"
        )

    # RENAME ASIDE, THEN SWAP -- never destroy before the
    # replacement is in place. The previous order was `worktree remove
    # --force` (which deletes the original working tree) followed by an
    # UNGUARDED move, so any failure between them -- an mv error, a read-only
    # parent, SIGKILL, power loss -- left this path EMPTY with the
    # replacement stranded at a staging path no message named. Witnessed by
    # fault injection before this fix. A rename is instant and the worktree's
    # .git pointer is ABSOLUTE, so relocating it does not break it.
    # RE-CHECK DIRTY, immediately before the irreversible step. The check far
    # above runs BEFORE the clone, and the clone is the long part -- minutes on
    # a real repo. Anything written into the worktree during that window is
    # invisible to the earlier gate, captured by the rename below, and then
    # deleted by the success-path cleanup, with "converted" reported. Same
    # verify-then-act-over-a-mutating-world family as the nest guard, opposite
    # victim: not an interloper, the user, on the happy path.
    #
    # This gate stays NARROW on purpose. It decides whether to PROCEED, not what to
    # DELETE, and nothing is dropped on the strength of a narrow reading: an ignored file
    # that slips past here lands in the aside and is caught by aside_disposition's wide
    # delete-side read, which keeps the whole aside. Refuse-side narrow, delete-side wide
    # -- a `*.env` must not block a conversion, it must only stop a deletion.
    status_now = run_git(path, "status", "--porcelain")
    if status_now.stdout.strip():
        shutil.rmtree(staging_path, ignore_errors=True)
        raise ConvertCloneError(
            f"{path}: working tree became dirty during the conversion, "
            f"refusing to continue:\n{status_now.stdout.strip()}"
        )

    aside_path = path.with_name(f".convert-aside-{path.name}.{os.getpid()}")
    if aside_path.exists():
        shutil.rmtree(staging_path)
        raise ConvertCloneError(f"{aside_path}: aside path already exists")
    try:
        os.rename(path, aside_path)
    except OSError as exc:
        shutil.rmtree(staging_path)
        raise ConvertCloneError(f"could not move the original aside: {exc}") from exc

    try:
        swap_in_place(staging_path, path)
    except ConvertCloneError:
        restore_original(path, aside_path)
        raise
    except Exception as exc:
        # Every branch that leaves a copy behind NAMES it. The first version
        # let a raw OSError propagate, so the operator was told the swap
        # failed and never told where the replacement went.
        restore_original(path, aside_path)
        raise ConvertCloneError(
            f"the swap onto {path} failed: {exc}. Your original has been "
            f"restored at {path}; the replacement clone is at {staging_path} "
            "(neither deleted)."
        ) from exc

    after = _lstat_summary(path / ".git")
    if not after["is_dir"]:
        shutil.rmtree(path, ignore_errors=True)
        restore_original(path, aside_path)
        raise ConvertCloneError(
            f"{path}: verification failed after the swap -- the original has been restored"
        )

    # ASK ABOUT THE ASIDE NOW, WHILE THE ANSWERER IS STILL ALIVE -- AND ONLY ASK.
    #
    # The prune below deletes the registration that this copy's `.git` pointer names, so
    # any git question asked of aside_path AFTER it returns rc 128 with an empty stdout.
    # The narrow form of this check (`status --porcelain`, stdout only, rc discarded) read
    # that as a clean tree and deleted it on the strength of an error -- and because it sat
    # two lines after the prune, `shutil.rmtree` ran on EVERY conversion and the `.keep`
    # branch below could not fire at all. Same family as the nest guard, opposite victim:
    # the user's work, on the happy path, judged by an instrument we had just killed.
    #
    # Asking early is only half of it. The other half is that the instrument is wide
    # (see aside_disposition): this read is the DELETE side, so an ignored file or an index
    # bit that mutes the comparison has to be visible here, where the refusal gates
    # upstream may stay narrow.
    verdict, _residual_count, residual_detail = aside_disposition(aside_path)

    # DEREGISTER, in porcelain only, and only now that the conversion is
    # verified. `git worktree prune` reaps a registration whose path is
    # MISSING, and the path is now occupied by the new clone, so prune alone
    # is a no-op here -- the existing witness (the canonical repo's own
    # `worktree list`) is what caught that. `git worktree remove` refuses a
    # path that is no longer a worktree, and `git worktree repair` cannot
    # undo a prune (measured: the admin dir is gone and the tree is left
    # unusable), so pruning earlier -- while the original sits aside -- would
    # make the restore path return a tree that is no longer a git repository.
    #
    # So: move the new clone aside for the length of one prune and move it
    # back. Both steps are instant renames, and every failure leaves both
    # copies at named paths, with the original's files still at aside_path.
    # Note what that is worth after the prune has run: the FILE PILE survives
    # and git can no longer answer for it, which is exactly why the
    # disposition above was taken before this block.
    dereg_path = path.with_name(f".convert-dereg-{path.name}.{os.getpid()}")
    pruned = False
    try:
        os.rename(path, dereg_path)
        run_git(canonical_repo_root, "worktree", "prune")
        pruned = True
        os.rename(dereg_path, path)
    except OSError as exc:
        # A message that implies recoverability the copy does not have is its own defect:
        # if the prune ran, the aside is a file pile no git command will answer for.
        pointer_note = (
            " The prune has already run, so the copy at that path is a file pile git can "
            "no longer answer for: its registration is gone and `git worktree repair` "
            "cannot undo a prune. Copy it by hand; do not expect git to read it."
            if pruned
            else ""
        )
        raise ConvertCloneError(
            f"deregistration failed: {exc}. The converted clone is at "
            f"{dereg_path if dereg_path.exists() else path}; your original is "
            f"at {aside_path}. Neither has been deleted.{pointer_note}"
        ) from exc

    still_registered = run_git(canonical_repo_root, "worktree", "list", "--porcelain")
    if f"worktree {path}\n" in still_registered.stdout + "\n":
        raise ConvertCloneError(
            f"{path} is still registered as a worktree of {canonical_repo_root} "
            f"after conversion; original preserved at {aside_path}"
        )

    # APPLY the disposition taken before the prune. Both non-DROP verdicts keep the tree,
    # and the UNREADABLE one is the important half: a tree git cannot answer for is exactly
    # the one we must not destroy, because its silence is indistinguishable from a clean
    # tree and only one of those two readings is safe to act on.
    keep = aside_path.with_name(f"{aside_path.name}.keep")
    if verdict == ASIDE_DROP:
        shutil.rmtree(aside_path, ignore_errors=True)
    else:
        os.rename(aside_path, keep)
        if verdict == ASIDE_KEEP:
            print(
                f"  NOTE: the original carried uncommitted work at cleanup time; "
                f"kept at {keep} (NOT deleted):\n{residual_detail}",
                file=sys.stderr,
            )
        else:
            print(
                f"  NOTE: the original could not be shown to be empty of work "
                f"({verdict}: {residual_detail}); kept at {keep} (NOT deleted).",
                file=sys.stderr,
            )

    return {
        "converted_at": datetime.now(UTC).isoformat(),
        "path": str(path),
        "old_common_dir": str(common_dir),
        "branch": branch,
        "head_sha": head_sha,
        "before_git_lstat": before,
        "after_git_lstat": after,
        "unpushed_commits": unpushed_commits,
        "origin_before": original_origin,
        "origin_after": _origin_url(path),
    }


def inspect_repo(path: Path) -> RepoStatus:
    if not path.exists():
        return RepoStatus(
            exists=False,
            is_git_repo=False,
            branch=None,
            upstream=None,
            dirty=False,
            ahead=0,
            behind=0,
            detached=False,
        )

    git_dir_is_symlink = is_git_dir_symlink(path)
    linked_worktree = is_linked_worktree(path)

    git_check = run_git(path, "rev-parse", "--is-inside-work-tree")
    if git_check.returncode != 0 or git_check.stdout.strip() != "true":
        return RepoStatus(
            exists=True,
            is_git_repo=False,
            branch=None,
            upstream=None,
            dirty=False,
            ahead=0,
            behind=0,
            detached=False,
            linked_worktree=linked_worktree,
            git_dir_is_symlink=git_dir_is_symlink,
        )

    branch_proc = run_git(path, "symbolic-ref", "--quiet", "--short", "HEAD")
    detached = branch_proc.returncode != 0
    branch = None if detached else branch_proc.stdout.strip()

    upstream_proc = run_git(
        path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
    )
    upstream = upstream_proc.stdout.strip() if upstream_proc.returncode == 0 else None

    dirty_proc = run_git(path, "status", "--porcelain")
    dirty = bool(dirty_proc.stdout.strip())

    ahead = 0
    behind = 0
    if upstream:
        counts_proc = run_git(path, "rev-list", "--left-right", "--count", f"HEAD...{upstream}")
        if counts_proc.returncode == 0:
            left, right = counts_proc.stdout.strip().split()
            ahead = int(left)
            behind = int(right)

    return RepoStatus(
        exists=True,
        is_git_repo=True,
        branch=branch,
        upstream=upstream,
        dirty=dirty,
        ahead=ahead,
        behind=behind,
        detached=detached,
        linked_worktree=linked_worktree,
        git_dir_is_symlink=git_dir_is_symlink,
    )


def classify(target: RepoTarget, status: RepoStatus, policy: RepoPolicy) -> PlannedAction:
    if not status.exists:
        return PlannedAction(target, "clone_missing", "repo path is absent", status, policy)

    if not status.is_git_repo:
        return PlannedAction(
            target,
            "block_path_conflict",
            "target path exists but is not a git repo",
            status,
            policy,
        )

    if status.git_dir_is_symlink:
        return PlannedAction(
            target,
            "block_git_dir_symlink",
            "repo's .git is a symlink into another clone's git state -- "
            "it does not own its own git state; remove the symlink and "
            "re-clone",
            status,
            policy,
        )

    if status.linked_worktree:
        return PlannedAction(
            target,
            "block_linked_worktree",
            "repo path is a linked git worktree, not an own clone -- "
            "gr does not support worktree-backed repos; convert it to a "
            "standalone clone",
            status,
            policy,
        )

    if status.detached:
        return PlannedAction(
            target,
            "manual_sync",
            "repo is on a detached HEAD; do not move automatically",
            status,
            policy,
        )

    if policy.tracked_branch and status.branch != policy.tracked_branch:
        if target.scope == "shared" and not status.dirty and status.ahead == 0:
            return PlannedAction(
                target,
                "checkout_branch",
                f"shared repo should be on {policy.tracked_branch}, found {status.branch}",
                status,
                policy,
            )
        return PlannedAction(
            target,
            "manual_sync",
            f"branch mismatch: expected {policy.tracked_branch}, found {status.branch}",
            status,
            policy,
        )

    if status.dirty:
        if policy.dirty_policy == "autostash":
            return PlannedAction(
                target,
                "autostash_then_sync",
                "working tree is dirty and policy allows preservation",
                status,
                policy,
            )
        return PlannedAction(
            target,
            "block_dirty",
            "working tree is dirty; stop by default",
            status,
            policy,
        )

    if not status.upstream:
        return PlannedAction(
            target,
            "manual_sync",
            "repo has no upstream tracking branch",
            status,
            policy,
        )

    if status.behind == 0 and status.ahead == 0:
        return PlannedAction(
            target,
            "no_change",
            "repo is already aligned with upstream",
            status,
            policy,
        )

    if status.behind > 0 and status.ahead == 0:
        if policy.sync_mode == "ff-only":
            return PlannedAction(
                target,
                "fast_forward",
                f"repo is behind upstream by {status.behind} commit(s) and can fast-forward",
                status,
                policy,
            )
        return PlannedAction(
            target,
            "manual_sync",
            f"repo is behind upstream by {status.behind} commit(s), but policy requires explicit sync",
            status,
            policy,
        )

    if status.behind > 0 and status.ahead > 0:
        return PlannedAction(
            target,
            "manual_sync",
            f"repo diverged from upstream (ahead {status.ahead}, behind {status.behind})",
            status,
            policy,
        )

    if status.ahead > 0:
        return PlannedAction(
            target,
            "manual_sync",
            f"repo has {status.ahead} local commit(s) ahead of upstream",
            status,
            policy,
        )

    return PlannedAction(target, "manual_sync", "unclassified repo state", status, policy)


def render_table(actions: list[PlannedAction]) -> str:
    lines = [
        "gr2 repo-maintenance prototype",
        "SCOPE\tTARGET\tREPO\tACTION\tBRANCH\tUPSTREAM\tSTATE\tREASON",
    ]
    for item in actions:
        state_bits = []
        if item.status.dirty:
            state_bits.append("dirty")
        if item.status.ahead:
            state_bits.append(f"ahead={item.status.ahead}")
        if item.status.behind:
            state_bits.append(f"behind={item.status.behind}")
        if item.status.detached:
            state_bits.append("detached")
        if item.status.linked_worktree:
            state_bits.append("linked_worktree")
        if item.status.git_dir_is_symlink:
            state_bits.append("git_dir_is_symlink")
        if not state_bits:
            state_bits.append("clean")

        lines.append(
            "\t".join(
                [
                    item.target.scope,
                    item.target.target_name,
                    item.target.repo_name,
                    item.action,
                    item.status.branch or "-",
                    item.status.upstream or "-",
                    ",".join(state_bits),
                    item.reason,
                ]
            )
        )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    workspace_root = args.workspace_root.resolve()
    spec_path = (args.spec or workspace_root / ".grip" / "workspace_spec.toml").resolve()
    spec = read_workspace_spec(spec_path)
    policy_doc = read_policy(args.policy)

    actions = []
    for target in derive_targets(workspace_root, spec):
        status = inspect_repo(target.path)
        policy = policy_for(target, policy_doc)
        actions.append(classify(target, status, policy))

    if args.json:
        print(json.dumps([item.as_dict() for item in actions], indent=2))
    else:
        print(render_table(actions))

    return 0


if __name__ == "__main__":
    sys.exit(main())
