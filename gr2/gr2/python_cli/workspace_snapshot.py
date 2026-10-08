"""Capture a materialized lane's resolved repository state as one kind=workspace
gr commit (the section-5 semantic tree for the workspace kind).

The workspace gr commit is a reproduction coordinate: it records, per repo, the
exact head a checkout must materialize plus the base it builds on. A repo with
uncommitted changes is REFUSED, not recorded — snapshotting a dirty tree would
record a coordinate that does not reproduce the author's actual state. Commit
the lane first (``gr2 commit`` lane-aware), then snapshot.
"""

from __future__ import annotations

from pathlib import Path

from gr2.prototypes import lane_workspace_prototype as lane_proto

from . import grip
from .gitops import git, repo_dirty


class WorkspaceSnapshotError(Exception):
    pass


def _resolve_head(repo_root: Path, key: str, fork_base_sha: str | None) -> tuple[str, str]:
    """(commit, base) for one lane repo.

    `commit` is HEAD. `base` is the RECORDED fork base, never derived
    from HEAD^: a lane with N commits must record where its work began, and HEAD^
    is only the (N-1)th commit. A lane with no recorded fork base reads base as
    unknown and is REFUSED here — the reproduction path never substitutes HEAD^ or
    a live merge-base for a coordinate it does not actually know.
    """
    head = git(repo_root, "rev-parse", "--verify", "HEAD")
    if head.returncode != 0 or not head.stdout.strip():
        raise WorkspaceSnapshotError(f"repo {key} has no HEAD to snapshot")
    commit = head.stdout.strip()
    if not fork_base_sha:
        raise WorkspaceSnapshotError(
            f"repo {key} has no recorded fork base; base is unknown. Recreate the lane "
            "with a recorded fork base; the snapshot will not substitute HEAD^."
        )
    # The recorded fork base must be a real commit in this repo and an ancestor of
    # HEAD — otherwise it is not a coordinate HEAD actually builds on.
    if git(repo_root, "rev-parse", "--verify", "--quiet", f"{fork_base_sha}^{{commit}}").returncode != 0:
        raise WorkspaceSnapshotError(
            f"repo {key} recorded fork base {fork_base_sha} is not a commit in this repo"
        )
    if git(repo_root, "merge-base", "--is-ancestor", fork_base_sha, commit).returncode != 0:
        raise WorkspaceSnapshotError(
            f"repo {key} recorded fork base {fork_base_sha} is not an ancestor of HEAD {commit}"
        )
    return commit, fork_base_sha


def resolve_lane_repos(workspace_root: Path, owner_unit: str, lane_name: str) -> list[dict[str, str]]:
    """Resolve every repo of a materialized lane into the section-5 fields.

    Refuses a repo with uncommitted changes (a snapshot must reproduce).
    """
    workspace_root = Path(workspace_root).resolve()
    doc = lane_proto.load_lane_doc(workspace_root, owner_unit, lane_name)
    fork_base = doc.get("fork_base", {})
    native_members = lane_proto.native_lane_members(workspace_root, doc)
    if native_members is not None:
        spec_by_name = {m["name"]: {"path": m["path"], "url": m["remote"]} for m in native_members}
    else:
        spec = lane_proto.load_workspace_spec(workspace_root)
        spec_by_name = {r.get("name"): r for r in spec.get("repos", [])}

    resolved: list[dict[str, str]] = []
    for key in doc.get("repos", []):
        repo_root = lane_proto.lane_repo_root(workspace_root, owner_unit, lane_name, key)
        if not (repo_root / ".git").exists():
            raise WorkspaceSnapshotError(f"repo {key} is not materialized at {repo_root}")
        if repo_dirty(repo_root):
            raise WorkspaceSnapshotError(
                f"repo {key} has uncommitted changes; commit the lane before snapshotting"
            )
        repo_spec = spec_by_name.get(key)
        if not repo_spec:
            raise WorkspaceSnapshotError(f"repo {key} is not in the workspace spec")
        commit, base = _resolve_head(repo_root, key, fork_base.get(key, {}).get("sha"))
        resolved.append({
            "key": key,
            "remote": str(repo_spec.get("url", "")),
            "path": str(repo_spec.get("path", "")),
            "commit": commit,
            "base": base,
        })
    return resolved


def snapshot_lane(workspace_root: Path, owner_unit: str, lane_name: str) -> str:
    """Resolve a materialized lane and write one kind=workspace gr commit.

    Returns the gr commit id (the reproduction coordinate).
    """
    workspace_root = Path(workspace_root).resolve()
    repos = resolve_lane_repos(workspace_root, owner_unit, lane_name)
    if not repos:
        raise WorkspaceSnapshotError(f"lane {owner_unit}/{lane_name} has no repos to snapshot")
    return grip.create_workspace_commit(workspace_root, repos)


def read_snapshot(workspace_root: Path, commit: str) -> list[dict[str, str]]:
    """Decode a kind=workspace gr commit's resolved repository fields."""
    return grip.read_workspace_commit(Path(workspace_root).resolve(), commit)
