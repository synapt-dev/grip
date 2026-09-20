"""Repo-tier review primitive.

`gr review open` materializes a PR head into an isolated review lane over the
grip#807 reference-clone seam (``ensure_lane_checkout``), and adds the review
behavior the raw lane materialization does not carry:

* the lane must land at the EXPECTED head sha, and the expected sha is bound
  from the host's own advertisement of the PR head, independently of the fetch
  that brings the bytes down — a mismatch REFUSES, never warns (verdict
  binding);
* the import resolution is PRINTED, so the reviewer sees which tree their run
  imports and cannot mistake a machine-wide install for the reviewed code;
* every dispatched execution first asserts its cwd is CONTAINED by the lane
  (a child-routing defect: a dispatched child must not run against the base tree);
* the review is recorded as the (repo, base, head) triple plus a required
  ``lane_kind`` stamp — where ``repo`` is the canonical, transport-independent
  GitHub source identity — a one-entry pin-delta so the project tier can adopt
  it without a second pin spelling, and ``lane_kind`` tells a reader whether the
  head reconstructs independently (``materialized``) or from the author's
  worktree under the bind-time guard (``bound``).

Two destructive operations are fenced: a REUSED lane is never deleted (cleanup
may only remove what this call created), and ``close`` refuses any path that is
not a review lane this tool opened.

This module is the pure, testable core. The Typer verbs are thin shells that
resolve the source repo from the workspace spec and the PR pins from the host,
then call in here.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import gitops
from .clone_exec import CloneExecutionError, IncompleteRemoval, rmtree_or_refuse
from .gitops import ensure_lane_checkout

_SHA40 = re.compile(r"\A[0-9a-f]{40}\Z")

# A review receipt is stamped with the KIND of lane it came from, so a reader
# never has to infer the reconstruction guarantee. ``materialized``: an isolated
# clone pinned at the recorded head, reconstructible independently of anything
# else on disk. ``bound``: derived from an author's own worktree at bind time,
# honest only under the clean-tree/HEAD-matches guard the bind imposes. The
# field is REQUIRED on every receipt (by design).
LANE_KINDS = ("materialized", "bound", "review-ephemeral")

Echo = Callable[[str], None]


class ReviewError(Exception):
    """A review-lane operation refused: a wrong head, a broken isolation
    invariant, a dispatched execution whose cwd escapes the lane, or a
    destructive operation whose target could not be proven safe. A refusal is
    terminal — the caller must not proceed as if it were a warning."""


# --------------------------------------------------------------------------- #
# repository identity (transport-independent)
# --------------------------------------------------------------------------- #
_GITHUB_SSH_SCP = re.compile(r"\Agit@github\.com:(?P<owner>[^/]+)/(?P<repo>.+?)(?:\.git)?\Z")
_GITHUB_SSH_URL = re.compile(
    r"\Assh://git@github\.com/(?P<owner>[^/]+)/(?P<repo>[^/?#]+?)(?:\.git)?(?:[/?#].*)?\Z"
)
_GITHUB_HTTPS = re.compile(
    r"\Ahttps?://(?:[^@/]+@)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/?#]+?)(?:\.git)?(?:[/?#].*)?\Z"
)


def canonical_source_identity(remote_url: str, *, allow_local: bool = False) -> str:
    """Reduce a clone URL to the repository's portable identity.

    Clone transport (SSH vs HTTPS, credentials, a machine path) answers *how the
    bytes arrive*; repository identity answers *which repository they are*. A
    cross-workspace lock needs one key per repository, so SSH and HTTPS clones of
    the same GitHub repo MUST canonicalize to the same bytes:
    ``https://github.com/<owner>/<repo>`` (lowercase, no ``.git``, no
    credentials, no query or fragment).

    A filesystem origin supports local test materialization but is not a
    portable committed-lock identity; it is refused unless ``allow_local`` is
    set, in which case it is marked ``local:<path>`` so it can never masquerade
    as a projectable identity."""
    url = (remote_url or "").strip()
    for pattern in (_GITHUB_SSH_SCP, _GITHUB_SSH_URL, _GITHUB_HTTPS):
        match = pattern.match(url)
        if match:
            return f"https://github.com/{match.group('owner').lower()}/{match.group('repo').lower()}"
    if allow_local:
        return f"local:{Path(url).resolve()}"
    raise ReviewError(
        f"origin {url!r} is not a canonical GitHub source; a review record's repo "
        "identity must be a portable https://github.com/<owner>/<repo> (no .git). "
        "A filesystem origin is clone transport, not a lock-projectable identity; "
        "pass allow_local only for a non-portable local test lane."
    )


@dataclass(frozen=True)
class ReviewRecord:
    """The repo-tier review IS this triple: a one-entry pin-delta.

    ``repo`` is the canonical, transport-independent source identity (never a
    clone URL or a local working path), ``base`` and ``head`` are lowercase full
    40-hex commit object IDs. Kept minimal on purpose so a project-tier lock can
    project this record without a second pin spelling.

    ``lane_kind`` (``materialized`` | ``bound``) is REQUIRED so a reader never
    infers the reconstruction guarantee: a materialized lane reconstructs from a
    carried range independently of the author's disk; a bound lane's bytes live
    in the author's worktree and are honest only under the bind-time guard."""

    repo: str
    base: str
    head: str
    lane_kind: str

    def __post_init__(self) -> None:
        if self.lane_kind not in LANE_KINDS:
            raise ReviewError(
                f"review record lane_kind must be one of {LANE_KINDS}, got "
                f"{self.lane_kind!r}"
            )

    def to_dict(self) -> dict[str, str]:
        return {"repo": self.repo, "base": self.base, "head": self.head, "lane_kind": self.lane_kind}


def review_record_path(lane_repo_root: Path | str) -> Path:
    """Where the triple is stored: inside the lane clone's own ``.git`` (a real
    directory per the #807 isolation contract), so it is untracked by the lane's
    working tree, removed when the lane is dropped, and serves as the identity
    marker that a directory is a review lane this tool opened."""
    return Path(lane_repo_root) / ".git" / "grip-review.json"


def assert_cwd_contained(cwd: Path | str, lane_root: Path | str) -> Path:
    """Refuse any execution whose working directory is not the lane or a
    descendant of it (a dispatched child must not route its execution out of the
    isolated lane). Returns the resolved cwd on success."""
    cwd_r = Path(cwd).resolve()
    lane_r = Path(lane_root).resolve()
    if cwd_r != lane_r and lane_r not in cwd_r.parents:
        raise ReviewError(
            f"dispatched execution cwd {cwd_r} escapes the review lane {lane_r}; "
            "refusing to run — a review command must execute inside its lane"
        )
    return cwd_r


def _require_sha(label: str, value: str) -> str:
    if not isinstance(value, str) or not _SHA40.match(value):
        raise ReviewError(f"{label} must be a lowercase full 40-hex commit sha, got {value!r}")
    return value


def host_pr_head_oid(source_repo_root: Path, pr_number: int, *, remote: str = "origin") -> str:
    """The host's own OID for a PR head, read straight from the remote's ref
    advertisement (``ls-remote``) — independent of any local fetch. This is the
    authority ``expected_head_sha`` must be bound from, so that a wrong or
    tampered fetch (whose local ref would otherwise be compared against itself)
    is detectable."""
    ref = f"refs/pull/{pr_number}/head"
    result = gitops.git(Path(source_repo_root), "ls-remote", remote, ref)
    line = result.stdout.strip()
    if result.returncode != 0 or not line:
        raise ReviewError(
            f"cannot read the host OID for PR {pr_number} ({ref}) on {remote!r}: "
            f"{result.stderr.strip() or 'ref not advertised'}"
        )
    oid = line.split()[0].strip().lower()
    if not _SHA40.match(oid):
        raise ReviewError(f"host advertised a non-sha OID for PR {pr_number}: {oid!r}")
    return oid


def open_review_lane(
    *,
    source_repo_root: Path,
    review_branch: str,
    expected_head_sha: str,
    base_sha: str,
    lane_repo_root: Path,
    workspace_root: Path,
    allow_local: bool = False,
    ephemeral: bool = False,
    repo_name: str | None = None,
    echo: Echo = print,
) -> ReviewRecord:
    """Materialize the review lane at the expected head and record the triple.

    ``expected_head_sha`` is the host-observed PR head (see ``host_pr_head_oid``);
    ``review_branch`` must exist in the source and hold the fetched head. A
    source ref that does not match ``expected_head_sha`` is a raced or tampered
    head and REFUSES before anything is materialized. A REUSED lane is never
    deleted: cleanup removes only a lane this call created."""
    source_repo_root = Path(source_repo_root)
    lane_repo_root = Path(lane_repo_root)
    _require_sha("expected head", expected_head_sha)
    _require_sha("base pin", base_sha)

    # 1. Verdict binding, BEFORE materializing: the fetched source ref must equal
    #    the host-observed head. A mismatch refuses; it never warps the lane into
    #    a different commit and calls it the PR, and it never reaches the seam.
    actual = gitops.git(source_repo_root, "rev-parse", "--verify", f"{review_branch}^{{commit}}")
    if actual.returncode != 0:
        raise ReviewError(
            f"review branch {review_branch!r} does not resolve in {source_repo_root}: "
            f"{actual.stderr.strip() or actual.stdout.strip()}"
        )
    if actual.stdout.strip() != expected_head_sha:
        raise ReviewError(
            f"review head mismatch: {review_branch} fetched {actual.stdout.strip()} but the "
            f"host advertises {expected_head_sha} for this PR; refusing to open a review on a "
            "head that is not the one the host is serving"
        )

    # 2. Canonical, transport-independent repo identity — never the clone URL or
    #    a local working path.
    repo_identity = canonical_source_identity(
        gitops.remote_origin_url(source_repo_root) or "", allow_local=allow_local
    )

    # 3. Materialize over the #807 seam. Capture created-vs-reused: it decides
    #    whether cleanup below is permitted to delete anything.
    # ``review_branch`` selects the source object. The local lane branch is a
    # separate safe name, so a caller that supplies an immutable SHA never turns
    # that SHA into a mutable branch selector at the clone seam.
    # The destination is one review lane, so its local branch is intentionally
    # stable across a later reopen. The immutable seed, not this branch spelling,
    # decides which commit is reviewed.
    lane_branch = "grip-review/open"
    if ephemeral:
        # A review-ephemeral lane is a blobless+sparse clone from the persistent
        # mirror -- NEVER the work-lane clone seam (materialize_lane_clone), whose
        # 8.1/8.2 invariants guard MUTABLE lanes. It is read-only and rm -rf'd on
        # close, so those invariants exceed its needs; blobless keeps the whole
        # commit+tree graph the review uses.
        from . import review_ephemeral
        if lane_repo_root.exists():
            existing = gitops.current_head_sha(lane_repo_root)
            if existing != expected_head_sha:
                raise ReviewError(
                    f"an existing review lane at {lane_repo_root} is at {existing!r}, not the "
                    f"requested head {expected_head_sha}; run `review close` to drop it, then reopen."
                )
            first_materialize = False
        else:
            review_ephemeral.materialize_review_ephemeral(
                mirror=source_repo_root, dest=lane_repo_root,
                head=expected_head_sha, base=base_sha,
                repo_name=(repo_name or source_repo_root.name.removesuffix(".git")),
            )
            first_materialize = True
        lane_head = gitops.git(lane_repo_root, "rev-parse", "HEAD")
        if lane_head.returncode != 0 or lane_head.stdout.strip() != expected_head_sha:
            if first_materialize:
                try:
                    rmtree_or_refuse(lane_repo_root)
                except IncompleteRemoval as cleanup_exc:
                    raise ReviewError(
                        f"review-ephemeral lane is at {lane_head.stdout.strip()!r}, not the "
                        f"expected head {expected_head_sha}, AND it could not be fully "
                        f"discarded: {cleanup_exc}"
                    ) from cleanup_exc
            raise ReviewError(
                f"review-ephemeral lane is at {lane_head.stdout.strip()!r}, not the expected "
                f"head {expected_head_sha}; lane discarded"
            )
        echo(f"review lane (ephemeral): {lane_repo_root.resolve()}")
        record = ReviewRecord(repo=repo_identity, base=base_sha, head=expected_head_sha,
                              lane_kind=review_ephemeral.REVIEW_EPHEMERAL_KIND)
        path = review_record_path(lane_repo_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record.to_dict(), indent=2) + "\n")
        return record
    try:
        first_materialize = ensure_lane_checkout(
            source_repo_root=source_repo_root,
            target_repo_root=lane_repo_root,
            branch=lane_branch,
            seed_commit=expected_head_sha,
            workspace_root=workspace_root,
        )
    except CloneExecutionError as exc:
        # The clone seam checks the requested seed during reuse, before it can
        # mutate a reviewer-owned lane. Translate that refusal into the review
        # contract's existing preservation wording.
        if lane_repo_root.exists() and gitops.current_head_sha(lane_repo_root) != expected_head_sha:
            raise ReviewError(
                f"an existing review lane at {lane_repo_root} is not the requested head "
                f"{expected_head_sha}. It is preserved unchanged (it may hold uncommitted "
                "review notes); run `review close` to drop it, then reopen."
            ) from exc
        raise ReviewError(str(exc)) from exc

    # 4. The lane must be at the expected head. On a FRESH lane, materialize
    #    binds HEAD to the seed, so a mismatch is a genuine fault and the lane we
    #    just made is discarded. On a REUSED lane, materialize leaves it
    #    byte-for-byte (grip#807 never resets/fetches/switches), so a mismatch
    #    means the reviewer already has an open lane at another head — REFUSE and
    #    PRESERVE it (their uncommitted notes are exactly what this primitive
    #    exists to protect); only an explicit `review close` may remove it.
    lane_head = gitops.git(lane_repo_root, "rev-parse", "HEAD")
    if lane_head.returncode != 0 or lane_head.stdout.strip() != expected_head_sha:
        if first_materialize:
            try:
                rmtree_or_refuse(lane_repo_root)
            except IncompleteRemoval as cleanup_exc:
                raise ReviewError(
                    f"freshly materialized lane is at {lane_head.stdout.strip()!r}, not "
                    f"the expected head {expected_head_sha}, AND it could not be fully "
                    f"discarded: {cleanup_exc}"
                ) from cleanup_exc
            raise ReviewError(
                f"freshly materialized lane is at {lane_head.stdout.strip()!r}, not the "
                f"expected head {expected_head_sha}; lane discarded"
            )
        raise ReviewError(
            f"an existing review lane at {lane_repo_root} is at "
            f"{lane_head.stdout.strip()!r}, not the requested head {expected_head_sha}. "
            "It is preserved unchanged (it may hold uncommitted review notes); run "
            "`review close` to drop it, then reopen."
        )

    # 5. Print import resolution: name the lane so a reviewer cannot mistake a
    #    machine-wide install for the reviewed tree (the PYTHONPATH trap).
    src_dir = lane_repo_root / "src"
    pythonpath = src_dir if src_dir.is_dir() else lane_repo_root
    echo(f"review lane: {lane_repo_root.resolve()}")
    echo(
        f"import resolution: PYTHONPATH={pythonpath.resolve()} — run the review "
        "from the lane so imports resolve to the reviewed tree, not a "
        "machine-wide install"
    )

    # 6. Record the triple plus the lane kind. This path always materializes an
    #    isolated clone pinned at the expected head, so the receipt is stamped
    #    ``materialized`` — reconstructible independently of any author worktree.
    record = ReviewRecord(repo=repo_identity, base=base_sha, head=expected_head_sha, lane_kind="materialized")
    path = review_record_path(lane_repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.to_dict(), indent=2) + "\n")
    return record


def run_in_review_lane(
    lane_repo_root: Path,
    command: Sequence[str],
    *,
    cwd: Path | str | None = None,
    echo: Echo = print,
) -> subprocess.CompletedProcess:
    """Dispatch a review command inside the lane, refusing BEFORE spawning if
    its cwd escapes the lane.

    This is the one place the review primitive spawns a child, so it is where
    the cwd-containment gate must fire (a dispatched child routing its execution
    out of the isolated lane is the defect). The assertion runs before
    ``subprocess.run``, never after — a child that has already started outside
    the lane has already read the wrong tree."""
    lane_repo_root = Path(lane_repo_root)
    run_cwd = assert_cwd_contained(cwd if cwd is not None else lane_repo_root, lane_repo_root)
    echo(f"review dispatch (cwd={run_cwd}): {' '.join(command)}")
    return subprocess.run(list(command), cwd=run_cwd)


def close_review_lane(
    *, lane_repo_root: Path, review_lane_root: Path, echo: Echo = print
) -> None:
    """Drop a review lane, refusing anything the tool does not own.

    ``close`` is a recursive delete, and **provenance cannot come from the
    record**: every field of the record is attacker-suppliable, so a same-origin
    clone with a fully self-consistent planted record (matching repo, its own
    HEAD) could otherwise route a delete at an arbitrary directory. Repo identity
    is not lane provenance.

    So the PRIMARY gate is a property the tool owns and a foreign clone cannot
    forge: the target must resolve STRICTLY BENEATH ``review_lane_root`` — the
    lane tree this tool manages under the workspace. A path outside that tree, or
    the root ITSELF, is refused before anything it contains is read, whatever
    record it carries. The root itself is refused deliberately: a recursive delete
    of the managed root would take every sibling lane and the lane index with it,
    so a self-consistent record planted AT the root must not license it — only a
    strict descendant (an individual lane's own clone) is a legitimate target.

    The record checks — owned ``.git`` directory, a well-formed
    ``(repo, base, head, lane_kind)`` receipt, repo matching the lane's own
    origin, and HEAD matching the recorded head — are SECONDARY consistency checks INSIDE that
    boundary: they catch a corrupted or MOVED lane (a reviewer commit or reset
    that means the lane may hold work), not a foreign one. The base workspace is
    never touched — only the lane clone is removed."""
    lane = Path(lane_repo_root).resolve()
    root = Path(review_lane_root).resolve()

    # PRIMARY, forgery-proof: STRICTLY beneath the tool-managed review-lane root.
    # `root in lane.parents` is true only when root is a proper ancestor of lane,
    # so this refuses both a path outside the tree AND the root itself (aiming the
    # delete at the root would take every sibling lane and the index with it).
    if root not in lane.parents:
        raise ReviewError(
            f"{lane} is not strictly beneath the review-lane root {root} this tool manages "
            "(a path outside the tree, or the root itself, is refused); refusing to delete, "
            "whatever record it carries."
        )

    if not lane.exists():
        echo(f"no review lane at {lane}")
        return

    # SECONDARY consistency, inside the owned boundary.
    git_dir = lane / ".git"
    record_path = review_record_path(lane)
    if not (git_dir.is_dir() and not git_dir.is_symlink()) or not record_path.is_file():
        raise ReviewError(
            f"{lane} lacks an owned .git directory or the review record; refusing to "
            "delete even inside the lane tree."
        )
    try:
        record = json.loads(record_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewError(
            f"{lane} review record is unreadable or malformed ({exc}); refusing to delete."
        ) from exc
    if set(record) != {"repo", "base", "head", "lane_kind"} or not (
        isinstance(record.get("repo"), str)
        and record["repo"]
        and _SHA40.match(str(record.get("base", "")))
        and _SHA40.match(str(record.get("head", "")))
        and record.get("lane_kind") in LANE_KINDS
    ):
        raise ReviewError(
            f"{lane} review record is not a well-formed (repo, base, head, lane_kind) receipt; "
            "refusing to delete."
        )
    lane_identity = canonical_source_identity(
        gitops.remote_origin_url(lane) or "", allow_local=True
    )
    if record["repo"] != lane_identity:
        raise ReviewError(
            f"{lane} review record repo {record['repo']!r} does not match the lane's own "
            f"origin identity {lane_identity!r}; refusing to delete."
        )
    lane_head = gitops.git(lane, "rev-parse", "HEAD")
    if lane_head.returncode != 0 or lane_head.stdout.strip() != record["head"]:
        raise ReviewError(
            f"{lane} is at HEAD {lane_head.stdout.strip()!r}, not the recorded review head "
            f"{record['head']}; the lane has moved (a commit, reset, or repointed clone) and "
            "may hold work — refusing to delete. Reset it to the recorded head, or remove it "
            "by hand once you have saved anything you need."
        )

    shutil.rmtree(lane)
    echo(f"review lane dropped: {lane}")
