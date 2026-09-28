"""Native single-repository push with explicit remote and arrival evidence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .gitops import git
from .review_records import lane_paths_for_repo, read_review_record, review_record_pointer_path


class PushError(Exception):
    pass


class PushEvidenceError(PushError):
    """The push was acknowledged, but its remote result cannot be verified."""


@dataclass(frozen=True)
class PushReceipt:
    remote: str
    branch: str
    local_sha: str
    remote_sha: str
    set_upstream: bool
    force_with_lease: bool


def _current_branch(repo: Path) -> str:
    try:
        result = git(repo, "branch", "--show-current")
    except OSError as exc:
        raise PushError(f"failed to determine the current branch in {repo}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise PushError(detail or "failed to determine the current branch")
    branch = result.stdout.strip()
    if not branch:
        raise PushError("cannot push from detached HEAD")
    return branch


def _remote_names(repo: Path) -> tuple[str, ...]:
    try:
        result = git(repo, "remote")
    except OSError as exc:
        raise PushError(f"failed to enumerate remotes in {repo}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise PushError(detail or "failed to enumerate git remotes")
    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())


def _select_remote(repo: Path, branch: str, explicit: str | None) -> str:
    remotes = _remote_names(repo)
    if explicit is not None:
        if explicit not in remotes:
            raise PushError(f"remote '{explicit}' is not configured in {repo}")
        return explicit

    for key in (f"branch.{branch}.pushRemote", "remote.pushDefault", f"branch.{branch}.remote"):
        try:
            configured = git(repo, "config", "--get", key)
        except OSError as exc:
            raise PushError(f"failed to inspect configured push destination {key}: {exc}") from exc
        if configured.returncode == 0 and configured.stdout.strip():
            remote = configured.stdout.strip()
            if remote not in remotes:
                raise PushError(f"{key} names unavailable remote '{remote}'")
            return remote
        if configured.returncode not in {0, 1}:
            detail = (configured.stderr or configured.stdout).strip()
            raise PushError(detail or f"failed to inspect configured push destination {key}")

    if len(remotes) == 1:
        return remotes[0]
    if not remotes:
        raise PushError("no git remote is configured; pass --remote after adding one")
    raise PushError(
        f"multiple remotes are configured ({', '.join(remotes)}); pass --remote or configure the branch upstream"
    )


def _head_sha(repo: Path) -> str:
    try:
        result = git(repo, "rev-parse", "--verify", "HEAD")
    except OSError as exc:
        raise PushError(f"failed to resolve HEAD in {repo}: {exc}") from exc
    sha = result.stdout.strip() if result.returncode == 0 else ""
    if not sha:
        raise PushError("cannot push because HEAD is not a commit")
    return sha


def _remote_branch_sha(repo: Path, remote: str, branch: str) -> str:
    ref = f"refs/heads/{branch}"
    try:
        result = git(repo, "ls-remote", "--heads", remote, ref)
    except OSError as exc:
        raise PushEvidenceError(
            f"push was acknowledged but remote receipt evidence was unavailable: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise PushEvidenceError(
            detail or "push was acknowledged but the remote branch could not be queried"
        )
    rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
    matches = [parts[0] for parts in rows if len(parts) == 2 and parts[1] == ref]
    if len(matches) != 1:
        raise PushEvidenceError(
            f"push was acknowledged but remote '{remote}' did not provide exactly one receipt for {ref}"
        )
    return matches[0]


def _refuse_review_ephemeral_repo(repo: Path) -> None:
    """A review-ephemeral lane is read-only and disposable: refuse a push from it,
    naming the kind, so a review lane never becomes a work lane."""
    try:
        paths = lane_paths_for_repo(repo)
        record = read_review_record(paths) if paths else None
    except Exception as exc:
        raise PushError(f"cannot safely resolve review receipt for {repo}: {exc}") from exc
    if review_record_pointer_path(repo).is_file() and record is None:
        raise PushError(f"{repo} has a review pointer but no readable receipt; refusing push")
    kind = record.get("lane_kind") if record else None
    if kind == "review-ephemeral":
        raise PushError(
            f"{repo} is a review-ephemeral review lane (read-only, disposable): it "
            "cannot be pushed. A review lane never becomes a work lane."
        )


def push_current_branch(
    repo: Path,
    *,
    remote: str | None = None,
    set_upstream: bool = False,
    force_with_lease: bool = False,
) -> PushReceipt:
    """Push the current branch and verify that the remote ref equals HEAD."""
    _refuse_review_ephemeral_repo(repo)
    branch = _current_branch(repo)
    selected_remote = _select_remote(repo, branch, remote)
    local_sha = _head_sha(repo)

    args = ["push"]
    if set_upstream:
        args.append("--set-upstream")
    if force_with_lease:
        args.append("--force-with-lease")
    args.extend([selected_remote, branch])
    try:
        pushed = git(repo, *args)
    except OSError as exc:
        raise PushError(f"failed to launch git push in {repo}: {exc}") from exc
    if pushed.returncode != 0:
        detail = (pushed.stderr or pushed.stdout).strip()
        raise PushError(detail or f"git push failed with exit {pushed.returncode}")

    remote_sha = _remote_branch_sha(repo, selected_remote, branch)
    if remote_sha != local_sha:
        raise PushEvidenceError(
            f"push was acknowledged but remote {selected_remote}/{branch} is {remote_sha}, expected {local_sha}"
        )
    return PushReceipt(
        remote=selected_remote,
        branch=branch,
        local_sha=local_sha,
        remote_sha=remote_sha,
        set_upstream=set_upstream,
        force_with_lease=force_with_lease,
    )
