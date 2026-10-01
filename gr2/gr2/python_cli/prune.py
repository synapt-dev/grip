"""Native `prune` command for gr2.

gr2 registers 44 verbs and `prune` was absent from all of them (measured
2026-08-30): gr1 was the only surface with a branch-prune, so every lane gr2
opens left a branch that only gr1 or a hand could remove. This is that verb.

It is deliberately MORE correct than gr1's prune. gr1's `is_branch_merged`
(src/git/branch.rs) uses `git branch --merged <target>`, which is CONTAINMENT
alone -- it reports a branch merged only when its tip is an ancestor of the
target. In our squash- and rebase-heavy history that misses most landed work
(measured on Apollo's own desk 2026-09-06: feat/gr2-workspace-spec-regeneration
and feat/pr-create-base each read NOT contained but ARE present in origin/dev by
patch-id). Per claude.md's standing rule -- "containment answers 'is this commit
OBJECT present'; nobody ever wants that answer" -- merged here is decided by
PATCH-ID (`git cherry`, patch-id equivalence, catches plain/ff/rebase/cherry-pick
merges) OR by the SQUASH TREE check (the branch's aggregate diff matching a single
target commit's patch-id), never by containment alone.

Single-repo by convention, like gr2's other top-level verbs (cwd or
--repo-path). Dry-run by default; deletes only with --execute; never touches a
remote ref; never deletes the current branch, the target, or `main`/`dev`.
"""

from __future__ import annotations

import subprocess

from dataclasses import dataclass

from pathlib import Path

from .gitops import git


class PruneError(Exception):
    pass


# Never delete these, by name, in addition to the current branch and the target.
_NAMED_PROTECTED = ("main", "dev")


@dataclass(frozen=True)
class MergedBranch:
    """A local branch whose work is already in the target, with why."""

    name: str
    reason: str  # "patch-id" | "squash"
    detail: str


@dataclass
class PruneReport:
    target: str
    target_source: str  # how the target was resolved, for the printout
    merged: list[MergedBranch]
    protected_skipped: list[str]
    deleted: list[str]
    failed: list[tuple[str, str]]  # (branch, error)
    executed: bool


def _short(ref: str) -> str:
    """The short branch name for a possibly-qualified ref (origin/dev -> dev)."""
    return ref.rsplit("/", 1)[-1]


def _current_branch(repo: Path) -> str | None:
    proc = git(repo, "branch", "--show-current")
    if proc.returncode != 0:
        raise PruneError(f"cannot read current branch in {repo}: {proc.stderr.strip()}")
    name = proc.stdout.strip()
    return name or None  # empty == detached HEAD


def _local_branches(repo: Path) -> list[str]:
    proc = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    if proc.returncode != 0:
        raise PruneError(f"cannot list local branches in {repo}: {proc.stderr.strip()}")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _ref_exists(repo: Path, ref: str) -> bool:
    return git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0


def resolve_target(
    repo: Path,
    target: str | None,
    remote: str,
    stored_target: str | None = None,
) -> tuple[str, str]:
    """Resolve the ref merged-ness is measured against.

    Order: explicit ``--target``, then the gripspace's stored ``[settings].target``
    (``stored_target``), then the integration branch ``<remote>/dev``, then the
    remote's own default branch (``refs/remotes/<remote>/HEAD``), then
    ``<remote>/main``. Returns ``(target_ref, how_it_was_resolved)``.

    The stored target is the branch work in THIS gripspace actually merges into
    (an epic branch, say, where ``dev`` is not the base). It is READ here, never
    written -- ``gr target set`` is the only writer. A bare name (``epic/x``) is
    mapped to ``<remote>/<name>`` because merged-ness is measured against a
    remote-tracking ref. A stored target whose remote ref is ABSENT (a stale or
    typo'd setting) is SKIPPED, not fatal: resolution falls through to ``dev`` and
    the returned source names the skipped stored value, so a stranger reading the
    report sees why ``dev`` was chosen. Skipping (rather than raising) keeps
    deletion on the safe side -- a rotted setting never leaves branches
    under-protected.

    ``dev`` is tried BEFORE ``origin/HEAD`` on purpose: our integration branch is
    ``dev``, but ``origin/HEAD`` points at ``main`` on a fresh clone, so resolving
    HEAD first would silently measure prune against ``main`` and report dev-merged
    branches as unmerged. An explicit ``--target`` that does not exist still
    raises, because a wrong operator-named target makes every verdict wrong.
    """
    if target is not None:
        if not _ref_exists(repo, target):
            raise PruneError(f"target ref '{target}' does not exist in {repo}")
        return target, "explicit --target"

    stored_note: str | None = None
    if stored_target:
        stored_ref = (
            stored_target
            if stored_target.startswith(f"{remote}/")
            else f"{remote}/{stored_target}"
        )
        if _ref_exists(repo, stored_ref):
            return stored_ref, f"stored target ({stored_ref})"
        stored_note = f"stored target '{stored_target}' skipped ({stored_ref} not found)"

    def _with_note(source: str) -> str:
        return f"{source}; {stored_note}" if stored_note else source

    dev = f"{remote}/dev"
    if _ref_exists(repo, dev):
        return dev, _with_note(f"integration branch ({dev})")
    head = git(repo, "symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD")
    if head.returncode == 0 and head.stdout.strip():
        ref = _short_remote(head.stdout.strip(), remote)
        if _ref_exists(repo, ref):
            return ref, _with_note(f"{remote}/HEAD")
    main = f"{remote}/main"
    if _ref_exists(repo, main):
        return main, _with_note(f"fallback ({main})")
    msg = (
        f"could not resolve a target in {repo}: no {remote}/dev, {remote}/HEAD, or "
        f"{remote}/main. Pass --target explicitly."
    )
    if stored_note:
        msg += f" ({stored_note})"
    raise PruneError(msg)


def _short_remote(symref: str, remote: str) -> str:
    # refs/remotes/origin/dev -> origin/dev
    prefix = "refs/remotes/"
    return symref[len(prefix):] if symref.startswith(prefix) else symref


def _patch_id(repo: Path, *diff_args: str) -> str | None:
    """Stable patch-id of `git diff <diff_args>`; None if the diff is empty."""
    diff = git(repo, "diff", *diff_args)
    if diff.returncode != 0:
        raise PruneError(f"git diff {' '.join(diff_args)} failed in {repo}: {diff.stderr.strip()}")
    if not diff.stdout.strip():
        return None
    pid = subprocess.run(
        ["git", "patch-id", "--stable"],
        cwd=repo,
        input=diff.stdout,
        capture_output=True,
        text=True,
        check=False,
    )
    if pid.returncode != 0 or not pid.stdout.strip():
        return None
    return pid.stdout.split()[0]


def _cherry_merged(repo: Path, target: str, branch: str) -> tuple[bool, int]:
    """Patch-id equivalence via `git cherry`: no '+' line means every commit
    unique to `branch` is already present in `target` by patch-id (covers
    plain/fast-forward/rebase/cherry-pick merges, and the empty-ahead case)."""
    proc = git(repo, "cherry", target, branch)
    if proc.returncode != 0:
        raise PruneError(
            f"git cherry {target} {branch} failed in {repo}: {proc.stderr.strip()}"
        )
    plus = [ln for ln in proc.stdout.splitlines() if ln.startswith("+")]
    return (len(plus) == 0, len(plus))


# How many of target's first-parent commits (newest first) to scan for a squash
# match before giving up. A squash-merge lands as ONE commit on the target's
# first-parent mainline, so the scan is bounded to that line (not every commit
# ever merged in). The cap is a safety valve against a pathologically old
# merge-base: hitting it returns "not squash-merged" (the SAFE direction for a
# deletion -- we never delete on an inconclusive check), and the caller can pass
# --target closer if a branch diverged very long ago.
_SQUASH_SCAN_CAP = 400


def _target_squash_patch_ids(repo: Path, target: str) -> dict[str, str]:
    """Map {commit patch-id -> sha} for target's newest first-parent commits.

    Built ONCE per prune and shared across every branch, so squash detection is
    O(cap) here plus O(1) per branch, not O(branches x cap). Squash-merge commits
    land on the target's first-parent mainline, so that is the only line worth
    scanning; the cap bounds a pathologically deep history and its miss is the
    SAFE direction for a deletion (a branch squashed older than the window reads
    as not-merged and is left alone)."""
    rev = git(
        repo,
        "rev-list",
        "--first-parent",
        f"--max-count={_SQUASH_SCAN_CAP}",
        target,
    )
    if rev.returncode != 0:
        raise PruneError(
            f"git rev-list --first-parent {target} failed in {repo}: {rev.stderr.strip()}"
        )
    ids: dict[str, str] = {}
    for sha in (ln.strip() for ln in rev.stdout.splitlines() if ln.strip()):
        # A root commit (no parent) or a shallow-clone boundary has no `<sha>^`;
        # such a commit cannot be the squash of a feature branch, so skip it
        # rather than error. This also keeps the verb usable inside a shallow
        # review clone, where the oldest commits are grafted.
        if not _ref_exists(repo, f"{sha}^"):
            continue
        pid = _patch_id(repo, f"{sha}^", sha)
        if pid is not None:
            ids.setdefault(pid, sha)
    return ids


def _squash_merged(
    repo: Path, target: str, branch: str, target_ids: dict[str, str]
) -> tuple[bool, str | None]:
    """The branch's aggregate diff (merge-base..branch) has the same stable
    patch-id as a single first-parent commit on `target` -- the squash case
    `git cherry` cannot see, because a squash gives the combined change a new
    commit whose per-commit patch-ids never match the branch's originals."""
    mb = git(repo, "merge-base", target, branch)
    if mb.returncode != 0 or not mb.stdout.strip():
        return (False, None)
    base = mb.stdout.strip()
    aggregate = _patch_id(repo, base, branch)
    if aggregate is None:
        # No net contribution over the merge-base (empty-ahead branch is already
        # handled by _cherry_merged).
        return (False, None)
    sha = target_ids.get(aggregate)
    return (sha is not None, sha)


def list_merged_branches(repo: Path, target: str) -> tuple[list[MergedBranch], list[str]]:
    """Return (merged, protected_skipped) for `repo` measured against `target`.

    Merged is decided by patch-id first, then the squash tree check -- never by
    containment alone. Protected branches (current, the target's own short name,
    `main`, `dev`) are never candidates and are returned so the caller can show
    what was skipped and why.
    """
    repo = Path(repo).resolve()
    protected = set(_NAMED_PROTECTED)
    protected.add(_short(target))
    current = _current_branch(repo)
    if current is not None:
        protected.add(current)

    local = _local_branches(repo)
    candidates = [b for b in local if b not in protected]
    protected_skipped = [b for b in local if b in protected]
    # Build target's first-parent squash-map ONCE, and only if a candidate that
    # patch-id did not already resolve needs it -- most prunes never do.
    target_ids: dict[str, str] | None = None

    merged: list[MergedBranch] = []
    for branch in candidates:
        is_cherry, plus = _cherry_merged(repo, target, branch)
        if is_cherry:
            merged.append(
                MergedBranch(branch, "patch-id", f"all commits present in {target} by patch-id (git cherry)")
            )
            continue
        if target_ids is None:
            target_ids = _target_squash_patch_ids(repo, target)
        is_squash, sha = _squash_merged(repo, target, branch, target_ids)
        if is_squash:
            merged.append(
                MergedBranch(
                    branch,
                    "squash",
                    f"aggregate diff matches {target} commit {sha[:12]} by patch-id (squash-merged)",
                )
            )
    return merged, protected_skipped


def _delete_local_branch(repo: Path, branch: str) -> None:
    # -D, not -d: we have already established the work is in the target by
    # patch-id/tree, and git's own -d uses the same containment test we reject.
    proc = git(repo, "branch", "-D", branch)
    if proc.returncode != 0:
        raise PruneError(proc.stderr.strip() or f"failed to delete {branch}")


def prune(
    repo: Path,
    *,
    target: str | None = None,
    remote: str = "origin",
    stored_target: str | None = None,
    execute: bool = False,
) -> PruneReport:
    """List (and, with execute=True, delete) merged local branches in one repo."""
    repo = Path(repo).resolve()
    target_ref, source = resolve_target(repo, target, remote, stored_target)
    merged, protected_skipped = list_merged_branches(repo, target_ref)

    deleted: list[str] = []
    failed: list[tuple[str, str]] = []
    if execute:
        for mb in merged:
            try:
                _delete_local_branch(repo, mb.name)
                deleted.append(mb.name)
            except PruneError as exc:
                failed.append((mb.name, str(exc)))

    return PruneReport(
        target=target_ref,
        target_source=source,
        merged=merged,
        protected_skipped=protected_skipped,
        deleted=deleted,
        failed=failed,
        executed=execute,
    )


def render_report(report: PruneReport) -> str:
    lines = [f"Target: {report.target}  ({report.target_source})"]
    if not report.merged:
        lines.append("No merged branches to prune.")
        return "\n".join(lines)
    verb = "Deleted" if report.executed else "Would delete"
    for mb in report.merged:
        marker = "  "
        if report.executed:
            if mb.name in report.deleted:
                marker = "  ✓ "
            elif any(mb.name == f for f, _ in report.failed):
                marker = "  ✗ "
        lines.append(f"{marker}{verb}: {mb.name}  [{mb.reason}] {mb.detail}")
    for name, err in report.failed:
        lines.append(f"  FAILED to delete {name}: {err}")
    if not report.executed:
        lines.append(f"\n{len(report.merged)} merged branch(es). Run with --execute to delete them.")
    else:
        lines.append(f"\nDeleted {len(report.deleted)} of {len(report.merged)} merged branch(es).")
    return "\n".join(lines)
