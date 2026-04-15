"""gr2 PR group orchestration.

Implements multi-repo PR lifecycle from PR-LIFECYCLE.md:
- create_pr_group: Create linked PRs across repos with pr_group_id
- merge_pr_group: Merge all PRs in a group (stops on first failure)
- check_pr_group_status: Poll status/checks and emit change events
- record_pr_review: Record an externally-submitted review event

The PlatformAdapter is group-unaware. This module assigns pr_group_id,
persists group metadata, and emits events per HOOK-EVENT-CONTRACT.md
section 3.2 (PR Lifecycle).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .events import emit, EventType
from .platform import AdapterError, CreatePRRequest, PlatformAdapter


class PRMergeError(RuntimeError):
    """Raised when a PR merge fails."""

    def __init__(self, repo: str, pr_number: int, reason: str) -> None:
        self.repo = repo
        self.pr_number = pr_number
        self.reason = reason
        super().__init__(f"merge failed for {repo}#{pr_number}: {reason}")


def _pr_groups_dir(workspace_root: Path) -> Path:
    return workspace_root / ".grip" / "pr_groups"


def _generate_group_id() -> str:
    return "pg_" + os.urandom(4).hex()


def _load_group(workspace_root: Path, pr_group_id: str) -> dict:
    path = _pr_groups_dir(workspace_root) / f"{pr_group_id}.json"
    return json.loads(path.read_text())


def _save_group(workspace_root: Path, group: dict) -> None:
    d = _pr_groups_dir(workspace_root)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{group['pr_group_id']}.json"
    path.write_text(json.dumps(group, indent=2))


def create_pr_group(
    workspace_root: Path,
    owner_unit: str,
    lane_name: str,
    title: str,
    base_branch: str,
    head_branch: str,
    repos: list[str],
    adapter: PlatformAdapter,
    actor: str,
    *,
    body: str = "",
    draft: bool = False,
) -> dict:
    """Create linked PRs across repos and emit pr.created."""
    pr_group_id = _generate_group_id()
    prs: list[dict] = []

    for repo in repos:
        request = CreatePRRequest(
            repo=repo,
            title=title,
            body=body,
            head_branch=head_branch,
            base_branch=base_branch,
            draft=draft,
        )
        ref = adapter.create_pr(request)
        prs.append({
            "repo": repo,
            "pr_number": ref.number,
            "url": ref.url,
        })

    group = {
        "pr_group_id": pr_group_id,
        "lane_name": lane_name,
        "title": title,
        "base_branch": base_branch,
        "head_branch": head_branch,
        "prs": prs,
        "status": {repo: "OPEN" for repo in repos},
    }
    _save_group(workspace_root, group)

    emit(
        event_type=EventType.PR_CREATED,
        workspace_root=workspace_root,
        actor=actor,
        owner_unit=owner_unit,
        payload={
            "pr_group_id": pr_group_id,
            "lane_name": lane_name,
            "repos": prs,
        },
    )

    return group


def merge_pr_group(
    workspace_root: Path,
    pr_group_id: str,
    adapter: PlatformAdapter,
    actor: str,
) -> dict:
    """Merge all PRs in a group. Stops on first failure."""
    group = _load_group(workspace_root, pr_group_id)
    merged: list[dict] = []

    for pr_info in group["prs"]:
        repo = pr_info["repo"]
        number = pr_info["pr_number"]
        try:
            adapter.merge_pr(repo, number)
        except AdapterError as exc:
            emit(
                event_type=EventType.PR_MERGE_FAILED,
                workspace_root=workspace_root,
                actor=actor,
                owner_unit=group.get("owner_unit", actor),
                payload={
                    "pr_group_id": pr_group_id,
                    "repo": repo,
                    "pr_number": number,
                    "reason": str(exc),
                },
            )
            raise PRMergeError(repo, number, str(exc)) from exc
        merged.append(pr_info)

    emit(
        event_type=EventType.PR_MERGED,
        workspace_root=workspace_root,
        actor=actor,
        owner_unit=group.get("owner_unit", actor),
        payload={
            "pr_group_id": pr_group_id,
            "repos": merged,
        },
    )

    return group


def check_pr_group_status(
    workspace_root: Path,
    pr_group_id: str,
    adapter: PlatformAdapter,
    actor: str,
) -> dict:
    """Poll PR status/checks for all repos in a group. Emit change events."""
    group = _load_group(workspace_root, pr_group_id)
    cached_status = group.get("status", {})

    for pr_info in group["prs"]:
        repo = pr_info["repo"]
        number = pr_info["pr_number"]
        status = adapter.pr_status(repo, number)
        old_state = cached_status.get(repo, "OPEN")

        # Detect state change (OPEN -> MERGED, OPEN -> CLOSED, etc.)
        if status.state != old_state:
            emit(
                event_type=EventType.PR_STATUS_CHANGED,
                workspace_root=workspace_root,
                actor=actor,
                owner_unit=group.get("owner_unit", actor),
                payload={
                    "pr_group_id": pr_group_id,
                    "repo": repo,
                    "pr_number": number,
                    "old_status": old_state,
                    "new_status": status.state,
                },
            )
            cached_status[repo] = status.state

        # Detect check results (only when checks are complete)
        if status.checks:
            completed = [c for c in status.checks if c.status == "COMPLETED"]
            if completed and len(completed) == len(status.checks):
                failed = [c.name for c in completed if c.conclusion != "SUCCESS"]
                if failed:
                    emit(
                        event_type=EventType.PR_CHECKS_FAILED,
                        workspace_root=workspace_root,
                        actor=actor,
                        owner_unit=group.get("owner_unit", actor),
                        payload={
                            "pr_group_id": pr_group_id,
                            "repo": repo,
                            "pr_number": number,
                            "failed_checks": failed,
                        },
                    )
                else:
                    emit(
                        event_type=EventType.PR_CHECKS_PASSED,
                        workspace_root=workspace_root,
                        actor=actor,
                        owner_unit=group.get("owner_unit", actor),
                        payload={
                            "pr_group_id": pr_group_id,
                            "repo": repo,
                            "pr_number": number,
                            "passed_checks": [c.name for c in completed],
                        },
                    )

    group["status"] = cached_status
    _save_group(workspace_root, group)
    return group


def record_pr_review(
    workspace_root: Path,
    pr_group_id: str,
    repo: str,
    pr_number: int,
    reviewer: str,
    state: str,
    actor: str,
) -> None:
    """Record an externally-submitted PR review and emit pr.review_submitted.

    Reviews come from outside gr2 (GitHub webhooks, human action, etc.).
    The adapter doesn't query reviews, so this is a push-model entry point.
    """
    emit(
        event_type=EventType.PR_REVIEW_SUBMITTED,
        workspace_root=workspace_root,
        actor=actor,
        owner_unit=actor,
        payload={
            "pr_group_id": pr_group_id,
            "repo": repo,
            "pr_number": pr_number,
            "reviewer": reviewer,
            "state": state,
        },
    )
