"""Executor for the neutral MaterializationPlan `clone` operation (S4-B).

S4-A landed the plan CONTRACT: validate -> un-forgeable ValidatedPlan
capability -> durable receipt. This is the first thing that consumes it.

Contract: MaterializationPlan v1 clone contract and acceptance fruit 6/7/8/9.

Why this is its own module rather than more of `gitops.py`: gitops is a thin
subprocess layer over git. The substance here is POLICY -- which object sharing
is permitted, what makes a clone state-isolated, when an existing clone may be
reused. Policy living in the primitive layer is policy that later callers
bypass by reaching for the primitive directly.

Design note carried from the S4-A review cycle, applied while designing rather
than while testing: for every guard below, the question was what ELSE would
reject this input first, and whether the guard is therefore untested at its own
level. Five masking pairs came out of that and are recorded in the test module;
the two that shaped this code most:

  - `_read_alternate_entries` returns a SET and callers compare with `==`, never
    `all(...)`. An empty alternates file makes "every entry is the declared
    cache" vacuously true, which is exactly how a clone with no object sharing
    at all passes a guard written to require object sharing.
  - `verify_clone_isolation` re-runs `verify_cache_provenance` on the declared
    reference instead of trusting it. Set-equality alone proves the clone points
    where the PLAN said; it says nothing about whether the plan pointed at a
    legitimate cache, and another unit's bare mirror of the same repository
    satisfies every other check.

And one property of git that the whole verification posture rests on:
`--reference-if-able` SILENTLY DEGRADES. Against a missing cache it exits 0 and
writes no alternates file at all. Section 8.2 chooses that flag deliberately so
a cold cache cannot fail the timed path, which means a declared reference is a
CLAIM the executor must verify positively afterwards. Passing the flag is not
evidence the alternate exists.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from . import gitops
from .spec_apply import (
    MaterializationPlanError,
    ValidatedPlan,
    _read_canonical_workspace_spec_bytes,
    canonicalize_workspace_path,
    workspace_cache_root,
)


def clone_and_pin(
    repo_url: str,
    dest: Path,
    *,
    pin: str = "",
    member: str = "",
    reference_repo_root: Path | None = None,
) -> bool:
    """Clone a member into a STAGING sibling, put it on its declared pin, then
    rename it into place. Returns whether this is the first materialization.

    Cloning straight into ``dest`` and then refusing left a real repository
    behind at the remote's tip, and the refusal was only half the damage: the
    NEXT run found a clone already present, printed success and converged
    nothing, so the member stayed on a commit its root never declared and
    nothing said so. Measured by a probe built on a remote whose pinned commit
    was pushed and then its branch deleted, the reflog expired and the objects
    gc'd -- the strongest form of that case, and it is what caught this.

    Publication is INSIDE the cleanup boundary, the same shape as the S4 clone
    path below: nothing follows the rename, so on success there is no staging
    left for the handler to remove.
    """
    # Same distinction as clone_repo's guard, and it matters for the same
    # reason: dest sits under a unit root that is itself inside the workspace
    # superproject, so `is_git_repo(dest)` is True for a directory that merely
    # exists -- which would report a first materialization as a re-run.
    # Already a clone: nothing to do. Staging here would clone, pin, and then
    # fail at the rename with a raw OSError (ENOTEMPTY) instead of answering the
    # caller's actual question, which is whether this was the first
    # materialization.
    if gitops.is_repo_root(dest):
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=dest.parent, prefix=f".{dest.name}.staging-"))
    try:
        gitops.clone_repo(repo_url, staging, reference_repo_root=reference_repo_root)
        if pin:
            gitops.checkout_declared_pin(staging, pin, member=member or dest.name)
        os.replace(staging, dest)
    except BaseException as exc:
        try:
            rmtree_or_refuse(staging)
        except IncompleteRemoval as cleanup_exc:
            raise CloneExecutionError(
                f"materializing '{member or dest.name}' failed ({exc}) and staging cleanup "
                f"also left it behind: {cleanup_exc}"
            ) from exc
        raise
    return True


@dataclasses.dataclass(frozen=True)
class _CloneBinding:
    """One immutable reading of a clone operation, taken from A's frozen plan
    snapshot before any caller-reachable code runs.

    Mirrors A's _ConsumptionBinding for the same reason: verification and use
    must not be separated by anything that can run caller code. Holding the
    fields instead of re-reading the capability makes a post-verification swap
    irrelevant rather than merely detectable."""

    repo_url: str
    branch: str
    dest_path: str
    reference_base: str | None


class CloneExecutionError(MaterializationPlanError):
    """A validated plan's clone operation could not be executed safely.

    Subclasses the contract's error so callers catching the family keep
    working, while the type still names which layer refused."""


class IncompleteRemoval(CloneExecutionError):
    """A directory a caller asked to be removed is still (partially) present."""


def rmtree_or_refuse(path: Path) -> None:
    """Remove a directory tree, then verify it is actually gone.

    ``shutil.rmtree(path, ignore_errors=True)`` silently drops any failure --
    a locked file, a permission-denied entry, a busy mount point -- and every
    call site that used it alone had no way to distinguish "cleaned" from
    "partially cleaned," several of them going on to report success (a
    "lane discarded" message, a silent fall-through to reuse the winner's
    lane) regardless. `shutil.rmtree` only removes a directory's OWN entry as
    its LAST step, so if anything beneath it survives, the directory itself
    still exists -- checking `path.exists()` after the attempt is therefore a
    complete detector for "did the whole tree go," not a sample of it. Raises,
    naming one surviving entry, if it did not; a caller that wants a softer
    outcome (log and continue) catches `IncompleteRemoval` explicitly rather
    than getting silence by default."""
    shutil.rmtree(path, ignore_errors=True)
    if not path.exists():
        return
    try:
        remaining = sorted(str(p) for p in path.rglob("*"))
    except OSError:
        remaining = []
    example = remaining[0] if remaining else str(path)
    raise IncompleteRemoval(
        f"{path} could not be fully removed; {len(remaining)} entr"
        f"{'y' if len(remaining) == 1 else 'ies'} beneath it remain, "
        f"including {example}"
    )


# Section 8.1 names these as the mutable state a clone must OWN. The
# --git-common-dir check proves only that the .git ROOT is local; every entry
# beneath it can be redirected individually, which is how a clone with a
# perfectly local common dir still reads another unit's refs or object store
# (Sentinel, #803 review at bd7afe5).
#
# `objects` is on this list for a reason worth stating: object sharing has TWO
# routes, and the alternates file is only one of them. Symlinking the objects
# directory itself shares the store with no alternates file to inspect -- so a
# plan declaring no reference_base passes the alternates check vacuously while
# the clone is fully joined to another unit's objects.
_CLONE_LOCAL_GIT_ENTRIES = (
    "objects",
    "refs",
    "index",
    "HEAD",
    "config",
    "logs",
    "packed-refs",
)

_SHA40 = re.compile(r"\A[0-9a-f]{40}\Z")


def _alternates_file(clone_root: Path) -> Path:
    return clone_root / ".git" / "objects" / "info" / "alternates"


def _require_local_git_internals(clone_root: Path, git_dir: Path) -> None:
    """Every entry section 8.1 requires the clone to own must be its own, not a
    redirection. lstat, never stat: stat() follows the link and reports the
    target, which is precisely the thing being hidden."""
    for name in _CLONE_LOCAL_GIT_ENTRIES:
        entry = git_dir / name
        try:
            mode = os.lstat(entry).st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise CloneExecutionError(
                f"clone at {clone_root} redirects .git/{name} through a symlink to "
                f"{os.readlink(entry)!r} -- section 8.1 requires clone-local refs, "
                "index, locks, config and objects, and a local .git root does not "
                "make the state beneath it local"
            )


def _require_complete_history(clone_root: Path, git_dir: Path) -> None:
    """Section 8.1: the clone must hold the complete reachable history required
    by the profile, and the v1 plan has no shallow or partial profile to opt
    into. A depth-1 clone is otherwise indistinguishable from a healthy one --
    correct origin, local git dir, clean tree, valid alternate -- so nothing
    else in this verifier can see it (Sentinel, #803 review at bd7afe5)."""
    if (git_dir / "shallow").exists():
        raise CloneExecutionError(
            f"clone at {clone_root} is shallow (.git/shallow present) -- section 8.1 "
            "requires the complete reachable history and the plan declares no shallow "
            "profile"
        )
    proc = gitops.git(clone_root, "rev-parse", "--is-shallow-repository")
    if proc.returncode == 0 and proc.stdout.strip() == "true":
        raise CloneExecutionError(
            f"clone at {clone_root} reports itself shallow -- section 8.1 requires the "
            "complete reachable history"
        )
    for key in ("remote.origin.promisor", "remote.origin.partialclonefilter"):
        probe = gitops.git(clone_root, "config", "--get", key)
        if probe.returncode == 0 and probe.stdout.strip():
            raise CloneExecutionError(
                f"clone at {clone_root} is a partial clone ({key}="
                f"{probe.stdout.strip()!r}) -- its history is fetched lazily from the "
                "network, which section 8.1's complete-history requirement excludes"
            )


def _working_tree_state(clone_root: Path) -> str:
    """clean | dirty | unreadable.

    Three outcomes, not two. gitops.repo_dirty() maps EVERY nonzero exit to
    False, so a corrupt .git/index reads as 'clean' and a damaged clone is
    reused as healthy (Sentinel, #803 review at bd7afe5). Collapsing the
    unreadable case into 'clean' is a fail-open: the one state we understand
    least becomes the one we treat most permissively.

    Deliberately local rather than a fix to gitops.repo_dirty(): that helper is
    live in app.py and syncops.py, and changing shared dirtiness semantics
    underneath two other call paths does not belong in this PR. Filed
    separately."""
    proc = gitops.git(clone_root, "status", "--porcelain")
    if proc.returncode != 0:
        return "unreadable"
    return "dirty" if proc.stdout.strip() else "clean"


def _read_alternates_of(object_dir: Path) -> set[Path]:
    """Alternate object directories declared by an arbitrary object database.

    Relative entries resolve against the OBJECT DIRECTORY, which is git's rule.
    Resolving them against the process working directory -- the obvious
    Path(line).resolve() -- makes this verifier and git disagree about what the
    clone is actually reading from, and a verifier that disagrees with git is
    worse than no verifier (Atlas, #803 review at bd7afe5)."""
    path = object_dir / "info" / "alternates"
    if not path.exists():
        return set()
    entries = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        candidate = Path(line)
        if not candidate.is_absolute():
            candidate = object_dir / candidate
        entries.add(candidate.resolve())
    return entries


def _read_alternate_entries(clone_root: Path) -> set[Path]:
    """Resolved alternate object directories declared by this clone.

    Blank lines are dropped, so a whitespace-only file reads as empty rather
    than as one weird entry pointing at the process working directory.

    A SET, not a list: the same permitted cache listed twice is the same object
    sharing, and git tolerates the duplicate. Deduplicating here means the
    permitted-set comparison judges WHICH object stores are reachable, not how
    many times the file happens to name them.

    Emptiness is deliberately NOT encoded as "a set that fails an equality" --
    callers claim it in an explicit branch first. Relying on the equality to
    reject empty is the vacuous-truth trap: it holds for `==` and silently
    fails for the `all(entry in permitted)` spelling that any later reader may
    think is the same check."""
    return _read_alternates_of(clone_root / ".git" / "objects")


def verify_cache_provenance(cache_root: Path, *, workspace_root: Path, repo_url: str) -> None:
    """Section 8.2: object sharing is permitted ONLY against the workspace-managed
    cache, seeded from the declared upstream.

    The three checks are orthogonal and each one is the only thing that can see
    its own failure:

      - containment rejects a bare mirror of the RIGHT repository sitting at the
        wrong place (another unit's directory, the team clone). Provenance
        cannot see it, because its origin matches.
      - bare-repository rejects a working clone parked under the cache root.
      - provenance rejects a cache under the right path, bare, seeded from a
        different upstream.

    Containment is what keeps object bytes from crossing a unit boundary, which
    is the isolation seam the whole design refuses to open."""
    cache_resolved = cache_root.resolve()
    permitted_root = workspace_cache_root(workspace_root).resolve()
    if cache_resolved == permitted_root or not cache_resolved.is_relative_to(permitted_root):
        raise CloneExecutionError(
            f"reference {cache_root} is not inside the workspace object cache "
            f"({permitted_root}) -- section 8.2 permits object sharing only with the "
            "workspace-managed cache, never with another unit or the team clone"
        )
    if not cache_resolved.exists():
        raise CloneExecutionError(
            f"declared object cache {cache_root} does not exist -- it must be seeded "
            "before any clone references it"
        )
    if not gitops.is_git_dir(cache_resolved):
        raise CloneExecutionError(
            f"declared object cache {cache_root} is not a bare git repository"
        )
    actual_url = gitops.remote_origin_url(cache_resolved)
    if actual_url != repo_url:
        raise CloneExecutionError(
            f"declared object cache {cache_root} was seeded from {actual_url!r}, "
            f"not from the declared upstream {repo_url!r}"
        )

    # Containment is only ONE HOP without this (Atlas, #803 review at bd7afe5).
    # A cache that sits at the right path, is bare, and carries the right origin
    # can still reach outside the workspace two ways, and the unit clone inherits
    # whatever it reaches: the cache's own objects dir may be a symlink, or the
    # cache may declare its own alternate pointing at a machine-global store.
    # Either one re-opens exactly the isolation seam section 8.2 refuses to open,
    # one level down where the clone-side check cannot see it.
    cache_objects = cache_resolved / "objects"
    if stat.S_ISLNK(os.lstat(cache_objects).st_mode):
        raise CloneExecutionError(
            f"declared object cache {cache_root} redirects its objects directory "
            f"through a symlink to {os.readlink(cache_objects)!r} -- the clone would "
            "share objects with an undeclared store one hop out"
        )
    transitive = _read_alternates_of(cache_objects)
    if transitive:
        raise CloneExecutionError(
            f"declared object cache {cache_root} declares its own alternate(s) "
            f"{sorted(str(e) for e in transitive)} -- object sharing must terminate at "
            "the workspace cache, and a cache that alternates onward makes every clone "
            "reachable into an undeclared store (section 8.2)"
        )


def verify_clone_isolation(
    clone_root: Path,
    *,
    workspace_root: Path,
    repo_url: str,
    reference_base: Path | None,
) -> None:
    """Section 8.1 (independent mutable state) + 8.2 (allowed object sharing).

    Runs on STAGING before publication and again on any existing clone before
    reuse, because the two paths admit the same failures by different routes:
    a fresh clone can silently lose its alternate, an existing directory can be
    a worktree somebody parked there.

    Deliberately does not consult `gitops.is_git_repo()`: that asks
    "is-inside-work-tree", which answers TRUE inside a linked worktree. The
    forbidden shape looks healthy to the obvious helper, so the checks here are
    positive statements about this clone's OWN state."""
    git_dir = clone_root / ".git"
    # lstat FIRST, and never Path.is_dir(). is_dir() follows the link, so a .git
    # that is itself a symlink into another same-origin clone answers True --
    # and then --git-common-dir resolves THROUGH that same link, so both sides of
    # the common-dir comparison land on the foreign directory and agree. Two
    # guards, one symlink, both satisfied (Atlas, #803 review at bd7afe5).
    try:
        git_mode = os.lstat(git_dir).st_mode
    except FileNotFoundError:
        raise CloneExecutionError(
            f"clone at {clone_root} has no .git -- it is not a git repository"
        ) from None
    if stat.S_ISLNK(git_mode):
        raise CloneExecutionError(
            f"clone at {clone_root} redirects .git through a symlink to "
            f"{os.readlink(git_dir)!r} -- .git must be a directory the clone owns "
            "(section 8.1)"
        )
    if not stat.S_ISDIR(git_mode):
        raise CloneExecutionError(
            f"clone at {clone_root} is not state-isolated: .git must be a directory, "
            "not a worktree pointer file (section 8.1)"
        )
    _require_local_git_internals(clone_root, git_dir)
    _require_complete_history(clone_root, git_dir)
    if (git_dir / "worktrees").exists():
        raise CloneExecutionError(
            f"clone at {clone_root} hosts linked worktrees (.git/worktrees), which share "
            "its refs and locks -- section 8.1 forbids the shape in both directions"
        )

    proc = gitops.git(clone_root, "rev-parse", "--git-common-dir")
    if proc.returncode != 0:
        raise CloneExecutionError(
            f"clone at {clone_root} is not a readable git repository: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    # Relative (".git") in a normal clone, absolute inside a worktree -- so it
    # has to be resolved against the clone root before it means anything.
    common_dir = Path(os.path.join(clone_root, proc.stdout.strip())).resolve()
    if common_dir != git_dir.resolve():
        raise CloneExecutionError(
            f"clone at {clone_root} resolves its git common directory to {common_dir}, "
            "outside its own .git -- its refs, index, and locks are shared (section 8.1)"
        )

    actual_url = gitops.remote_origin_url(clone_root)
    if actual_url != repo_url:
        raise CloneExecutionError(
            f"clone at {clone_root} has origin {actual_url!r}, not the declared {repo_url!r}"
        )

    entries = _read_alternate_entries(clone_root)
    if reference_base is None:
        if entries:
            raise CloneExecutionError(
                f"clone at {clone_root} declares alternate(s) "
                f"{sorted(str(e) for e in entries)} but its plan operation declares no "
                "reference_base -- undeclared object sharing is forbidden (section 8.2)"
            )
        return

    verify_cache_provenance(reference_base, workspace_root=workspace_root, repo_url=repo_url)
    expected = {(reference_base / "objects").resolve()}
    if not entries:
        raise CloneExecutionError(
            f"clone at {clone_root} declares no alternate, but its plan operation "
            f"declares reference_base {reference_base}. git's --reference-if-able "
            "degrades silently, so an absent or empty alternates file is the expected "
            "shape of a reference that never took -- not evidence that one did"
        )
    if entries != expected:
        raise CloneExecutionError(
            f"clone at {clone_root} declares alternate(s) "
            f"{sorted(str(e) for e in entries)}, which is not exactly the declared "
            f"workspace cache {sorted(str(e) for e in expected)} (section 8.2)"
        )


def _require_workspace_binding(validated: ValidatedPlan, workspace_root: Path) -> None:
    """The plan is bound to a WorkspaceSpec at VALIDATION; the executor takes
    workspace_root as a separate argument. Nothing structurally stops a caller
    from validating against one workspace and executing against another, and
    every relative path in the plan would then resolve somewhere else.

    Re-checking at USE rather than trusting the capability's field is the same
    lesson the S4-A TOCTOU round ended on: verification belongs at the moment of
    use, not only at the moment of construction."""
    try:
        spec_bytes = _read_canonical_workspace_spec_bytes(workspace_root)
    except MaterializationPlanError as exc:
        raise CloneExecutionError(
            f"cannot execute against {workspace_root}: its canonical WorkspaceSpec is "
            f"unreadable ({exc})"
        ) from exc
    actual = hashlib.sha256(spec_bytes).hexdigest()
    if actual != validated.workspace_spec_sha256:
        raise CloneExecutionError(
            f"plan is bound to WorkspaceSpec {validated.workspace_spec_sha256} but "
            f"{workspace_root} has {actual} -- executing a plan against a workspace it "
            "was not validated for would resolve every declared path elsewhere"
        )


def _git_clone(repo_url: str, target: Path, *, branch: str, reference: Path | None) -> None:
    command = ["git", "clone", "--quiet"]
    if reference is not None:
        # section 8.2: --dissociate is intentionally omitted on the timed path.
        # State isolation, not object duplication, is the invariant.
        command.extend(["--reference-if-able", str(reference)])
    command.extend(["--branch", branch, repo_url, str(target)])
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise CloneExecutionError(
            f"failed to clone {repo_url} at branch {branch!r}:\n"
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )


def _reuse_existing_clone(
    dest: Path, *, workspace_root: Path, repo_url: str, reference_base: Path | None
) -> None:
    """Section 8.3: a healthy clone is reused; a dirty one is NEVER reset or
    replaced; a damaged or mismatched one blocks.

    Isolation runs before the dirty check because "is this working tree dirty"
    is not a meaningful question about a worktree pointer or a clone of some
    other repository -- those are answered by refusing, not by inspecting.

    The declared branch is deliberately NOT enforced on reuse. Section 8.1
    requires a clone-local HEAD; the unit owns its branch state, and forcing it
    back to the plan's branch would be exactly the destructive repair 8.3 puts
    behind an explicit separate command."""
    verify_clone_isolation(
        dest,
        workspace_root=workspace_root,
        repo_url=repo_url,
        reference_base=reference_base,
    )
    state = _working_tree_state(dest)
    if state == "unreadable":
        raise CloneExecutionError(
            f"existing clone at {dest} cannot report its working-tree state -- it is "
            "damaged (an unreadable index or object database). Section 8.3 blocks a "
            "damaged clone rather than repairing it, and blocking here changes none of "
            "its bytes"
        )
    if state == "dirty":
        raise CloneExecutionError(
            f"existing clone at {dest} is dirty -- section 8.3 never resets or replaces "
            "a dirty clone; destructive repair requires an explicit separate command"
        )
    if gitops.current_head_sha(dest) is None:
        raise CloneExecutionError(
            f"existing clone at {dest} has no resolvable HEAD -- reuse requires a clone "
            "that can actually be worked in, not merely one whose status command exits 0"
        )


def execute_clone_operation(
    validated: ValidatedPlan, index: int, *, workspace_root: Path
) -> dict[str, object]:
    """Execute the `clone` operation at `index` of a validated plan.

    Returns neutral, receipt-shaped evidence for that operation. Addressing by
    index rather than filtering for clone operations keeps evidence[i] aligned
    with operation i, which is what S4-A's receipt screen requires: an executor
    that silently skipped the kinds it does not handle would shift every later
    result by one and still screen clean."""
    # A plain Path, always. workspace_root is caller-supplied, and a Path
    # SUBCLASS can run arbitrary code from __truediv__/resolve during the path
    # work below -- which is the callback that reopens the window this binding
    # exists to close. Normalising here removes the callback surface itself
    # rather than trying to be safe around it.
    workspace_root = Path(os.fspath(workspace_root))

    validated.verify(require_provenance=True)
    _require_workspace_binding(validated, workspace_root)

    operations = validated.plan["operations"]
    if not 0 <= index < len(operations):
        raise CloneExecutionError(
            f"operation index {index} is out of range for a plan with "
            f"{len(operations)} operation(s)"
        )
    op = operations[index]
    kind = op.get("kind")
    if kind != "clone":
        raise CloneExecutionError(
            f"operations[{index}] is kind {kind!r}, not 'clone' -- its handler lands "
            "with a later slice (venv/editable_install with S4-D, project_file with S4-C)"
        )

    # ONE immutable binding taken from A's frozen snapshot, before any path work
    # runs, and no live capability read after it (Atlas, #803 review at bd7afe5).
    # The previous shape verified, then did callback-capable path work, then
    # re-read validated.plan -- so a capability swapped in between was cloned
    # from while the sealed one was never touched. This is A's own consume()
    # lesson: the fix is not to check again, it is to stop re-reading.
    binding = _CloneBinding(
        repo_url=str(op["repo_url"]),
        branch=str(op["branch"]),
        dest_path=str(op["dest_path"]),
        reference_base=(
            str(op["reference_base"]) if op.get("reference_base") is not None else None
        ),
    )

    repo_url = binding.repo_url
    branch = binding.branch
    dest = canonicalize_workspace_path(
        workspace_root, binding.dest_path, field_name=f"operations[{index}].dest_path"
    )
    declared_reference = binding.reference_base
    reference_base = (
        canonicalize_workspace_path(
            workspace_root,
            declared_reference,
            field_name=f"operations[{index}].reference_base",
        )
        if declared_reference is not None
        else None
    )

    if reference_base is not None:
        # Before any work: cloning against a cache we would refuse afterwards
        # only wastes the timed path and leaves staging to clean up.
        verify_cache_provenance(reference_base, workspace_root=workspace_root, repo_url=repo_url)

    reused = dest.exists()
    if reused:
        _reuse_existing_clone(
            dest,
            workspace_root=workspace_root,
            repo_url=repo_url,
            reference_base=reference_base,
        )
    else:
        _stage_and_publish(
            dest,
            workspace_root=workspace_root,
            repo_url=repo_url,
            branch=branch,
            reference_base=reference_base,
        )

    # Section 12.1 requires repo URL, destination, HEAD, clone-state evidence,
    # and cache path plus APPROVED-ALTERNATE evidence. The previous shape echoed
    # the declaration back (reference_base as planned) and called it evidence --
    # hollow-green, because a receipt that repeats the plan proves nothing about
    # what is on disk (Atlas, #803 review at bd7afe5). What is recorded here is
    # what was OBSERVED and proven: the alternates git will actually read, and
    # the isolation facts the verifier established.
    observed_alternates = sorted(
        _relativize(entry, workspace_root) for entry in _read_alternate_entries(dest)
    )
    return {
        "kind": "clone",
        "repo_url": binding.repo_url,
        "dest_path": binding.dest_path,
        "branch": gitops.current_branch(dest),
        "head_sha": gitops.current_head_sha(dest) or "",
        "cache_path": declared_reference,
        "approved_alternates": observed_alternates,
        "clone_state": {
            "git_dir_local": True,
            "complete_history": True,
            "hosts_worktrees": False,
            "working_tree": _working_tree_state(dest),
        },
        "reused": reused,
    }


def _relativize(path: Path, workspace_root: Path) -> str:
    """Workspace-relative when possible. An absolute path inside a receipt is
    not wrong, but it makes two differently-rooted clean runs produce different
    receipts for identical work -- which acceptance fruit 16 forbids."""
    try:
        return str(path.relative_to(workspace_root.resolve()))
    except ValueError:
        return str(path)


def _stage_and_publish(
    dest: Path,
    *,
    workspace_root: Path,
    repo_url: str,
    branch: str,
    reference_base: Path | None,
) -> None:
    """Section 8.3: create at a sibling staging path, verify, then rename.

    A half-verified clone must never be visible at dest_path, because the reuse
    path would later find it and call it healthy -- publication is the only
    moment at which "this clone passed its guards" becomes a fact other code
    can rely on.

    Staging uses mkdtemp rather than a pid-suffixed name: two concurrent unit
    materializations of the same repository are the normal case under section
    9.1's parallel clone fan-out, and a name that has to be ARGUED unique is a
    name that eventually is not. git clones happily into an existing empty
    directory, so mkdtemp costs nothing."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=dest.parent, prefix=f".{dest.name}.staging-"))
    try:
        _git_clone(repo_url, staging, branch=branch, reference=reference_base)
        verify_clone_isolation(
            staging,
            workspace_root=workspace_root,
            repo_url=repo_url,
            reference_base=reference_base,
        )
        actual_branch = gitops.current_branch(staging)
        if actual_branch != branch:
            raise CloneExecutionError(
                f"clone of {repo_url} is on branch {actual_branch!r}, not the declared {branch!r}"
            )
        # Publication is INSIDE the cleanup boundary. It was outside, so an
        # OSError from the rename kept dest correctly absent but left a fully
        # populated staging sibling behind -- the no-residue guarantee held for
        # every failure except the one that happens at the publication seam
        # itself (Sentinel, #803 review at bd7afe5). Nothing follows the rename,
        # so on success there is no staging left for the handler to remove.
        os.replace(staging, dest)
    except BaseException as exc:
        try:
            rmtree_or_refuse(staging)
        except IncompleteRemoval as cleanup_exc:
            raise CloneExecutionError(
                f"clone failed ({exc}) and staging cleanup also left it behind: "
                f"{cleanup_exc}"
            ) from exc
        raise


# ---------------------------------------------------------------------------
# grip#807: lane materialization as an independent reference clone.
#
# The S4 plan path above clones a declared URL at a declared branch. A LANE has
# neither a plan nor a URL in hand: it has a source checkout, a destination, and
# a branch that may exist ONLY in the source (an unpushed seed). It reuses this
# file's verifier and staging shape rather than growing a second, weaker clone
# contract inside gitops.py (grip#807 step 7). The one semantic that differs is
# the publication race: two agents materialize the SAME destination, so the
# loser must reuse the winner rather than overwrite it. A per-destination O_EXCL
# lock is the no-replace primitive; the rename verb is not, because os.rename and
# os.replace both replace an empty target directory on POSIX, so neither refuses
# a concurrently created dest on its own. The lock plus an absence check taken
# while it is held is what makes publication no-replace.
# ---------------------------------------------------------------------------


# Publish-lock waiting bounds. A lane clone publishes in seconds; a loser waits
# for the winner's dest to appear. Generous enough to cover a slow clone, bounded
# so a creator that died mid-publish surfaces as an error rather than a hang.
_LANE_PUBLISH_LOCK_TIMEOUT_S = 120.0
_LANE_PUBLISH_POLL_S = 0.05


def _lane_clone(repo_url: str, target: Path, *, reference: Path | None) -> None:
    """Clone the canonical URL at its default branch. No ``--branch``: the lane's
    branch (which may be unpushed) is seeded by a one-shot fetch from the source
    afterward, so cloning it from the URL would fail for a branch the URL has
    never seen."""
    command = ["git", "clone", "--quiet"]
    if reference is not None:
        command.extend(["--reference-if-able", str(reference)])
    command.extend([repo_url, str(target)])
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise CloneExecutionError(
            f"failed to clone lane source {repo_url!r}:\n"
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )


def _resolve_origin_url(source_repo_root: Path, url: str) -> str:
    """A relative FILESYSTEM origin resolves against the source repo's location,
    which is where git resolves it -- but our clone runs from a staging directory
    in the lane tree, so the same relative string would resolve against the wrong
    cwd and fail (Sentinel, grip#807 v1). A scheme URL (https, ssh, git@) or an
    absolute path is already unambiguous and passes through untouched; a relative
    path is made absolute against the source before it ever reaches a clone with a
    different cwd."""
    if "://" in url or url.startswith("git@") or os.path.isabs(url):
        return url
    # A local relative path (e.g. "../foo.git"): resolve it where git would, at
    # the source repo, not at whatever cwd the clone later runs from.
    return os.path.abspath(os.path.join(source_repo_root, url))


def _publish_lane_atomically(
    staging: Path,
    dest: Path,
    *,
    workspace_root: Path,
    repo_url: str,
    reference_base: Path | None,
    expected_branch: str,
    expected_seed: str | None = None,
) -> bool:
    """Publish the staged clone under a per-destination lock (grip#807 step 5).

    The rename verb is NOT the no-replace primitive: os.rename and os.replace both
    replace an empty target directory on POSIX, so neither refuses a concurrently
    created dest on its own. A per-destination O_EXCL lockfile is the primitive --
    the lock, not the directory, is the claim. The winner holds the lock across
    "dest is absent (checked while holding the lock) -> move staging in"; because
    only a lock holder ever creates dest, no empty dest can appear in that window
    from another lane creator. A loser fails to acquire, waits for the winner's
    dest to appear, discards only its own staging, and reuses the winner. Returns
    True if this creator published, False on reuse.
    """
    lock = dest.parent / f".{dest.name}.publish.lock"
    deadline = time.monotonic() + _LANE_PUBLISH_LOCK_TIMEOUT_S
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # Another creator holds the publish lock. Wait for its dest to appear.
            if dest.exists():
                rmtree_or_refuse(staging)
                _reuse_existing_lane(
                    dest,
                    workspace_root=workspace_root,
                    repo_url=repo_url,
                reference_base=reference_base,
                expected_branch=expected_branch,
                expected_seed=expected_seed,
                )
                return False
            if time.monotonic() > deadline:
                raise CloneExecutionError(
                    f"timed out after {_LANE_PUBLISH_LOCK_TIMEOUT_S:g}s waiting for the "
                    f"publish lock {lock} to release -- a previous creator may have died "
                    "mid-publish. Remove the lock file if no publish is in progress"
                )
            time.sleep(_LANE_PUBLISH_POLL_S)
            continue
        try:
            # Lock held. A dest present now is either a prior winner (reuse) or an
            # externally-created directory (reuse validates and refuses it) -- the
            # absence check below is what refuses it, since the rename itself would
            # happily replace an empty directory.
            if dest.exists():
                rmtree_or_refuse(staging)
                _reuse_existing_lane(
                    dest,
                    workspace_root=workspace_root,
                    repo_url=repo_url,
                reference_base=reference_base,
                expected_branch=expected_branch,
                expected_seed=expected_seed,
                )
                return False
            # dest is absent and we hold the lock: no other lane creator can make
            # it, so this move lands on a target proven absent under mutual
            # exclusion. os.rename and os.replace are equivalent here (both replace
            # an empty dir, both fail on a non-empty one) -- the absence check
            # above, not the verb, is the guarantee; os.rename is used only because
            # no overwrite is intended.
            os.rename(staging, dest)
            return True
        finally:
            os.close(fd)
            try:
                os.unlink(lock)
            except FileNotFoundError:
                pass


def _reuse_existing_lane(
    dest: Path,
    *,
    workspace_root: Path,
    repo_url: str,
    reference_base: Path | None,
    expected_branch: str,
    expected_seed: str | None = None,
) -> None:
    """grip#807 step 6: a healthy lane on the expected branch is reused untouched.

    Isolation, origin, and cache are checked first (the same verifier the staging
    path runs), because "is this dirty / on which branch" is not a meaningful
    question about a worktree pointer or a clone of some other repository -- those
    are answered by refusing. A dirty or locally-committed but otherwise valid
    lane on the expected branch is left byte-for-byte: materialization never
    resets, stashes, fetches, or switches."""
    verify_clone_isolation(
        dest,
        workspace_root=workspace_root,
        repo_url=repo_url,
        reference_base=reference_base,
    )
    state = _working_tree_state(dest)
    if state == "unreadable":
        raise CloneExecutionError(
            f"existing lane at {dest} cannot report its working-tree state -- it is "
            "damaged. grip#807 blocks a damaged lane rather than repairing it; blocking "
            "changes none of its bytes"
        )
    actual_head = gitops.current_head_sha(dest)
    if actual_head is None:
        raise CloneExecutionError(
            f"existing lane at {dest} has no resolvable HEAD -- reuse requires a lane "
            "that can be worked in, not merely one whose status command exits 0"
        )
    actual_branch = gitops.current_branch(dest)
    if actual_branch != expected_branch:
        raise CloneExecutionError(
            f"existing lane at {dest} is on branch {actual_branch!r}, not the expected "
            f"{expected_branch!r}. Materialization never switches a lane's branch; move "
            f"it back with `git -C {dest} checkout {expected_branch}` or remove the lane "
            "to re-materialize (grip#807 step 6)"
        )
    if expected_seed is not None and actual_head != expected_seed:
        raise CloneExecutionError(
            f"existing lane at {dest} is at {actual_head!r}, not the requested immutable "
            f"seed {expected_seed!r}. Materialization never resets, fetches, or switches a "
            "reused lane; remove it before reopening at a different review head"
        )


def materialize_lane_clone(
    *,
    source_repo_root: Path,
    dest: Path,
    branch: str,
    seed_commit: str | None = None,
    workspace_root: Path,
    cache_root: Path | None = None,
) -> bool:
    """grip#807: materialize a lane repository as an independent reference clone.

    Returns True on first materialization, False when an existing valid lane is
    reused. Never runs ``git worktree add`` and never accepts a linked worktree
    as a lane checkout -- two lanes on the same branch must not share refs, HEAD,
    reflogs, index, locks, config, or working tree.
    """
    source_repo_root = Path(source_repo_root)
    dest = Path(dest)
    workspace_root = Path(os.fspath(workspace_root))

    # Step 1: bind the canonical URL from the source's origin and validate it. A
    # relative filesystem origin is resolved against the SOURCE here, because the
    # clone below runs from a staging cwd where the same relative string points
    # elsewhere (Sentinel, grip#807 v1).
    raw_url = gitops.remote_origin_url(source_repo_root)
    if not raw_url:
        raise CloneExecutionError(
            f"lane source {source_repo_root} has no origin remote -- its canonical URL "
            "cannot be derived (grip#807 step 1)"
        )
    repo_url = _resolve_origin_url(source_repo_root, raw_url)

    # An explicit review pin is an immutable object ID, not a source ref or a
    # local branch spelling. Resolve it before clone/reuse so every following
    # path carries one bound commit. The branch-only lane API keeps its legacy
    # selected-branch-or-HEAD behavior when no pin was supplied.
    if seed_commit is not None:
        if not _SHA40.match(seed_commit):
            raise CloneExecutionError(
                f"explicit lane seed must be a lowercase full 40-hex commit sha, got {seed_commit!r}"
            )
        seed = gitops.git(source_repo_root, "rev-parse", "--verify", f"{seed_commit}^{{commit}}")
        if seed.returncode != 0:
            raise CloneExecutionError(
                f"cannot resolve explicit lane seed {seed_commit!r} in {source_repo_root} to a commit:\n"
                f"{seed.stderr.strip() or seed.stdout.strip()}"
            )
        seed_sha = seed.stdout.strip()
        seed_ref: str | None = None
    else:
        seed_sha = None
        seed_ref = None

    # The workspace-managed bare cache shares immutable object bytes when present.
    # --reference-if-able degrades silently, so a reference is declared to the
    # verifier ONLY when the cache actually exists; otherwise the verifier would
    # (correctly) reject a clone that declares a reference no alternate records.
    if cache_root is None:
        cache_root = workspace_root / ".grip" / "cache" / "repos" / f"{source_repo_root.name}.git"
    reference_base = cache_root if cache_root.exists() else None

    if dest.exists():
        _reuse_existing_lane(
            dest,
            workspace_root=workspace_root,
            repo_url=repo_url,
            reference_base=reference_base,
            expected_branch=branch,
            expected_seed=seed_sha,
        )
        return False

    # Step 2: seed selection binds an IMMUTABLE COMMIT, not a ref name (Atlas,
    # grip#807 v1). An existing source branch seeds at its exact commit; otherwise
    # the source's HEAD commit seeds a new branch. Resolving to a SHA in the source
    # now closes the window in which the source ref could move between selection
    # and fetch, and makes "which commit did this lane start at" answerable.
    if seed_sha is None:
        branch_in_source = (
            gitops.git(source_repo_root, "show-ref", "--verify", f"refs/heads/{branch}").returncode == 0
        )
        seed_ref = f"refs/heads/{branch}" if branch_in_source else "HEAD"
        seed = gitops.git(source_repo_root, "rev-parse", "--verify", f"{seed_ref}^{{commit}}")
        if seed.returncode != 0:
            raise CloneExecutionError(
                f"cannot resolve lane seed {seed_ref!r} in {source_repo_root} to a commit:\n"
                f"{seed.stderr.strip() or seed.stdout.strip()}"
            )
        seed_sha = seed.stdout.strip()

    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=dest.parent, prefix=f".{dest.name}.staging-"))
    try:
        _lane_clone(repo_url, staging, reference=reference_base)

        # Step 3: transfer the bound seed COMMIT by a one-shot fetch from the source
        # path (the commit may be unpushed and absent from the origin URL). A path
        # fetch adds no persistent remote. Fetch the exact object; local transport
        # advertises ref tips and the seed is one, so a by-SHA fetch resolves it.
        fetch = gitops.git(staging, "fetch", "--no-tags", str(source_repo_root), seed_sha)
        if fetch.returncode != 0:
            # Some server configs refuse a by-SHA want; fall back to the containing
            # ref, then still check out the bound SHA below.
            if seed_commit is None:
                assert seed_ref is not None
                fetch = gitops.git(staging, "fetch", "--no-tags", str(source_repo_root), seed_ref)
                if fetch.returncode != 0:
                    raise CloneExecutionError(
                        f"failed to fetch seed {seed_sha} from lane source {source_repo_root}:\n"
                        f"{fetch.stderr.strip() or fetch.stdout.strip()}"
                    )
            else:
                raise CloneExecutionError(
                    f"failed to fetch explicit immutable seed {seed_sha} from lane source {source_repo_root}:\n"
                    f"{fetch.stderr.strip() or fetch.stdout.strip()}"
                )
        checkout = gitops.git(staging, "checkout", "-B", branch, seed_sha)
        if checkout.returncode != 0:
            raise CloneExecutionError(
                f"failed to seed lane branch {branch!r} at {seed_sha}:\n"
                f"{checkout.stderr.strip() or checkout.stdout.strip()}"
            )
        # Bind check: HEAD is exactly the commit resolved in the source.
        head_sha = gitops.current_head_sha(staging)
        if head_sha != seed_sha:
            raise CloneExecutionError(
                f"lane seed check failed: staged HEAD {head_sha} is not the bound seed {seed_sha}"
            )

        # Step 4: prove the isolation invariants on the staged clone before it is
        # ever visible at dest -- .git is a local directory, common-dir resolves
        # inside it, no .git/worktrees, origin is the declared URL, and the only
        # alternate (if any) is the declared cache.
        verify_clone_isolation(
            staging,
            workspace_root=workspace_root,
            repo_url=repo_url,
            reference_base=reference_base,
        )

        # Step 5: publish under a per-destination lock (no-replace primitive).
        return _publish_lane_atomically(
            staging,
            dest,
            workspace_root=workspace_root,
            repo_url=repo_url,
            reference_base=reference_base,
            expected_branch=branch,
            expected_seed=seed_sha,
        )
    except BaseException as exc:
        try:
            rmtree_or_refuse(staging)
        except IncompleteRemoval as cleanup_exc:
            raise CloneExecutionError(
                f"clone failed ({exc}) and staging cleanup also left it behind: "
                f"{cleanup_exc}"
            ) from exc
        raise
