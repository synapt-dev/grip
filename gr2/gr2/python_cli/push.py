"""Native single-repository push with explicit remote and arrival evidence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .gitops import LsRemoteMalformed, git, is_ancestor, ls_remote_oid, ls_remote_rows
from .review_records import lane_paths_for_repo, read_review_records_for_guard, review_record_pointer_path


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
    # git sent nothing: the remote already had the head (porcelain flag '=' on every ref)
    up_to_date: bool = False


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
        result = git(repo, "ls-remote", "--heads", "--", remote, ref)
    except OSError as exc:
        raise PushEvidenceError(
            f"push was acknowledged but remote receipt evidence was unavailable: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise PushEvidenceError(
            detail or "push was acknowledged but the remote branch could not be queried"
        )
    try:
        matches = [sha for sha, name in ls_remote_rows(result.stdout) if name == ref]
    except LsRemoteMalformed as exc:
        raise PushEvidenceError(f"push was acknowledged but remote receipt evidence was unreadable: {exc}") from None
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
        records = read_review_records_for_guard(paths) if paths else ()
    except Exception as exc:
        raise PushError(f"cannot safely resolve review receipt for {repo}: {exc}") from exc
    if review_record_pointer_path(repo).is_file() and not records:
        raise PushError(f"{repo} has a review pointer but no readable receipt; refusing push")
    if any(record.get("lane_kind") == "review-ephemeral" for record in records):
        raise PushError(
            f"{repo} is a review-ephemeral review lane (read-only, disposable): it "
            "cannot be pushed. A review lane never becomes a work lane."
        )


def _porcelain_up_to_date(stdout: str) -> bool:
    """True when `git push --porcelain` reports every ref as already on the remote ('=' flag)."""
    flags = [line.split("\t", 1)[0] for line in stdout.splitlines() if "\t" in line]
    return bool(flags) and all(flag == "=" for flag in flags)


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

    # --porcelain: the per-ref flag ('=' for a ref the remote already had) is fixed text, unlike
    # the "Everything up-to-date" sentence, which git translates under a non-English locale.
    args = ["push", "--porcelain"]
    if set_upstream:
        args.append("--set-upstream")
    if force_with_lease:
        args.append("--force-with-lease")
    # "--": git takes a remote named "--receive-pack=..." (`git remote add -- <name>`); unfenced it is an option.
    args.extend(["--", selected_remote, branch])
    try:
        pushed = git(repo, *args)
    except OSError as exc:
        raise PushError(f"failed to launch git push in {repo}: {exc}") from exc
    if pushed.returncode != 0:
        # --porcelain puts the per-ref reason ("[rejected] (fetch first)") on stdout and git's
        # error and hints on stderr; the refusal carries both.
        detail = "\n".join(part for part in (pushed.stdout.strip(), pushed.stderr.strip()) if part)
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
        up_to_date=_porcelain_up_to_date(pushed.stdout),
    )


@dataclass(frozen=True)
class LaneRepoPush:
    """One repo's row in a lane push: `pushed`, `up_to_date` or `failed`. `uncovered` carries the
    sentence when the push landed but the head is not on the lane's branch at its remote."""

    repo: str
    status: str
    remote: str | None = None
    branch: str | None = None
    sha: str | None = None
    error: str | None = None
    uncovered: str | None = None


def _lane_branch_uncovered(repo: Path, receipt: PushReceipt, lane_branch: str) -> str | None:
    """Why the pushed head is not on the lane's branch at the remote it went to, or None when it is.

    When the repo pushed the lane's branch itself, the push's own `ls-remote` already proved the
    remote ref equals the head, so the answer costs nothing more. Only a repo on ANOTHER branch asks
    the remote once about the lane's branch. When that tip is not in this clone (a teammate pushed
    past the head and this clone has not fetched), the ancestry cannot be read locally, so exactly
    that ref is fetched and the question asked again; a fetch that fails says it cannot tell, never
    that the head must be pushed."""
    if receipt.branch == lane_branch:
        return None
    where = f"{receipt.remote}/{lane_branch}"
    not_on = f"head {receipt.local_sha} is not on {where}; push it first"
    listed = git(repo, "ls-remote", "--heads", "--", receipt.remote, f"refs/heads/{lane_branch}")
    if listed.returncode != 0:
        return f"cannot tell whether head {receipt.local_sha} is on {where}: ls-remote failed"
    try:
        tip = ls_remote_oid(listed.stdout, f"refs/heads/{lane_branch}")
    except ValueError:
        return f"cannot tell whether head {receipt.local_sha} is on {where}: ls-remote answered a malformed listing"
    if tip is None:
        return not_on
    ancestry = is_ancestor(repo, receipt.local_sha, tip, via_cwd=True)
    if ancestry is None:
        fetched = git(repo, "fetch", "--no-tags", "--refmap=", "--no-write-fetch-head", "--", receipt.remote, f"refs/heads/{lane_branch}")
        if fetched.returncode != 0:
            return f"cannot tell whether head {receipt.local_sha} is on {where}: its tip could not be fetched"
        ancestry = is_ancestor(repo, receipt.local_sha, tip, via_cwd=True)
        if ancestry is None:
            return f"cannot tell whether head {receipt.local_sha} is on {where}: ancestry could not be read"
    return None if ancestry else not_on


def push_lane(
    workspace_root: Path,
    owner_unit: str,
    lane_name: str,
    *,
    remote: str | None = None,
    set_upstream: bool = False,
    force_with_lease: bool = False,
) -> list[LaneRepoPush]:
    """Push every repo of a lane through `push_current_branch`, one row per repo.

    Pushes to N remotes cannot be atomic: a repo that fails is recorded and the repos already
    pushed stay pushed; the rows say which landed. After each successful push, the coverage question
    the store's root push will ask is asked here, so a lane push cannot report success that the
    root push later refuses."""
    from gr2.prototypes import lane_workspace_prototype as lane_proto

    from .commit import _lane_repo_targets

    doc = lane_proto.load_lane_doc(workspace_root, owner_unit, lane_name)
    if doc.get("lane_kind") == "review-ephemeral":
        raise PushError(
            f"lane {owner_unit}/{lane_name} is a review-ephemeral lane (read-only, disposable): "
            "it cannot be pushed. A review lane never becomes a work lane."
        )
    branch_map = doc.get("branch_map", {}) or {}
    rows: list[LaneRepoPush] = []
    for key, repo_root in _lane_repo_targets(workspace_root, owner_unit, lane_name, doc):
        try:
            receipt = push_current_branch(
                repo_root,
                remote=remote,
                set_upstream=set_upstream,
                force_with_lease=force_with_lease,
            )
        except PushError as exc:
            rows.append(LaneRepoPush(key, "failed", error=str(exc)))
            continue
        lane_branch = branch_map.get(key) or receipt.branch
        why = _lane_branch_uncovered(repo_root, receipt, lane_branch)
        uncovered = f"{key} {why}" if why else None
        rows.append(
            LaneRepoPush(
                key,
                "up_to_date" if receipt.up_to_date else "pushed",
                remote=receipt.remote,
                branch=receipt.branch,
                sha=receipt.local_sha,
                uncovered=uncovered,
            )
        )
    return rows
