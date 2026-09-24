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
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .events import EventType, emit, emit_after_outcome
from .merge_verification import (
    CompletedMerge,
    MergeVerificationTarget,
    consume_merge_receipt,
)
from .platform import (
    AdapterError,
    CreatePRRequest,
    MergeEvidenceError,
    MergeMethod,
    MergeReceipt,
    PlatformAdapter,
)

__all__ = [
    "MergeMethod",
    "PRMergeError",
    "PRMergeOutcomeUnknownError",
    "PRMergePostconditionError",
    "UnpermittedMergeMethodError",
    "check_pr_group_status",
    "create_pr_group",
    "merge_pr_group",
    "record_pr_review",
    "resolve_merge_method",
]


class UnpermittedMergeMethodError(RuntimeError):
    """The requested method is known, but workspace policy does not permit it."""

    def __init__(self, requested: MergeMethod, permitted: list[str]) -> None:
        self.requested = requested
        self.permitted = list(permitted)
        allowed = ", ".join(self.permitted) or "none"
        super().__init__(
            f"merge method {requested.value!r} is not permitted here "
            f"(permitted: {allowed}); refusing rather than substituting another method"
        )


def _parse_method(name: str, *, source: str) -> MergeMethod:
    try:
        return MergeMethod(name)
    except ValueError:
        expected = ", ".join(method.value for method in MergeMethod)
        raise ValueError(
            f"unrecognised merge method {name!r} from {source} "
            f"(expected one of: {expected}); refusing rather than falling back"
        ) from None


def resolve_merge_method(
    explicit: str | None = None,
    configured: str | None = None,
    permitted: list[str] | None = None,
) -> MergeMethod:
    """Resolve explicit, then configured, then merge-commit strategy."""

    if explicit is not None:
        chosen = _parse_method(explicit, source="--method")
    elif configured is not None:
        chosen = _parse_method(configured, source="workspace setting")
    else:
        chosen = MergeMethod.MERGE

    if permitted is not None and chosen.value not in permitted:
        raise UnpermittedMergeMethodError(chosen, permitted)
    return chosen


class PRMergeError(RuntimeError):
    """A merge command failed, carrying prior completed host operations."""

    operation_acknowledged = False
    outcome_unknown = False

    def __init__(
        self,
        repo: str,
        pr_number: int,
        reason: str,
        *,
        completed: list[CompletedMerge],
    ) -> None:
        self.repo = repo
        self.pr_number = pr_number
        self.reason = reason
        self.completed = list(completed)
        already = ", ".join(item.receipt.observed.repo for item in self.completed)
        suffix = (
            f" (ALREADY MERGED, do not retry: {already})"
            if self.completed
            else " (nothing had merged yet)"
        )
        super().__init__(f"merge failed for {repo}#{pr_number}: {reason}{suffix}")


class PRMergeOutcomeUnknownError(PRMergeError):
    """The host acknowledged the command but no immutable receipt was available."""

    operation_acknowledged = True
    outcome_unknown = True

    def __init__(
        self,
        repo: str,
        pr_number: int,
        reason: str,
        *,
        completed: list[CompletedMerge],
    ) -> None:
        self.repo = repo
        self.pr_number = pr_number
        self.reason = reason
        self.completed = list(completed)
        already = ", ".join(item.receipt.observed.repo for item in self.completed)
        suffix = f"; earlier completed: {already}" if already else ""
        RuntimeError.__init__(
            self,
            f"merge outcome unknown for {repo}#{pr_number}: {reason}; "
            f"the command was acknowledged, so do not retry{suffix}",
        )


class PRMergePostconditionError(PRMergeError):
    """The merge is known to have happened but its evidence was not consumed."""

    operation_acknowledged = True
    outcome_unknown = False

    def __init__(
        self,
        receipt: MergeReceipt,
        reason: str,
        *,
        completed: list[CompletedMerge],
    ) -> None:
        observed = receipt.observed
        self.receipt = receipt
        self.repo = str(observed.repo)
        self.pr_number = int(observed.number)
        self.reason = reason
        self.completed = list(completed)
        already = ", ".join(item.receipt.observed.repo for item in self.completed)
        suffix = f"; earlier completed: {already}" if already else ""
        RuntimeError.__init__(
            self,
            f"merge postcondition failed for {self.repo}#{self.pr_number}: {reason}; "
            f"the host merge is acknowledged, so do not retry{suffix}",
        )


def _pr_groups_dir(workspace_root: Path) -> Path:
    return workspace_root / ".grip" / "pr_groups"


def _generate_group_id() -> str:
    return "pg_" + os.urandom(4).hex()


def _load_group(workspace_root: Path, pr_group_id: str) -> dict:
    path = _pr_groups_dir(workspace_root) / f"{pr_group_id}.json"
    return json.loads(path.read_text())


def _save_group(workspace_root: Path, group: dict) -> Path:
    d = _pr_groups_dir(workspace_root)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{group['pr_group_id']}.json"
    path.write_text(json.dumps(group, indent=2))
    return path


class SiblingLinkError(RuntimeError):
    """One or more PRs in a set could not be linked to their siblings.

    The group is attached because it is already persisted by the time this is raised:
    the record carries per-repo ``sibling_edits`` status, so a caller can print the
    set it created AND fail. Anything less reports success for a set a reader cannot
    navigate.
    """

    def __init__(self, group: dict, unlinked: list[str]) -> None:
        self.group = group
        self.unlinked = list(unlinked)
        super().__init__(
            "unlinked PR(s) after the sibling pass: "
            + ", ".join(self.unlinked)
            + " (each named in sibling_edits with its failure)"
        )


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
        prs.append({"repo": repo, "pr_number": ref.number, "url": ref.url})

    # THE SIBLING BLOCK. A reviewer who lands on one PR of a set has no way to reach
    # the others from it: measured on a4, both bodies read `gr2 PR group for
    # default/set-lane` and neither named the other. The links can only be added AFTER
    # every PR exists, because a number does not exist until its PR is created, so
    # this is a second pass over the set rather than part of the create call.
    #
    # Every failure is REPORTED and any failure is FATAL to the command: a half-linked
    # set that prints success is worse than no links, because the reader believes the
    # set is navigable.
    sibling_edits: dict[str, str] = {}
    unlinked: list[str] = []
    if len(prs) > 1:
        members = "\n".join(f"- {item['repo']}: {item['url']}" for item in prs)
        block = f"\n\n---\nPart of a set of {len(prs)} PRs for this slice:\n{members}\n"
        for item in prs:
            repo_name = str(item["repo"])
            number = item.get("pr_number")
            try:
                if number is None:
                    raise AdapterError(f"{repo_name} carries no PR number, so it cannot be linked")
                adapter.edit_pr_body(repo_name, int(number), body + block)
            except Exception as exc:  # every failure must be reported, none swallowed
                sibling_edits[repo_name] = f"FAILED: {exc}"
                unlinked.append(f"{repo_name}#{number}")
            else:
                sibling_edits[repo_name] = "linked"

    group = {
        "pr_group_id": pr_group_id,
        "owner_unit": owner_unit,
        "lane_name": lane_name,
        "title": title,
        "base_branch": base_branch,
        "head_branch": head_branch,
        "platform": getattr(adapter, "name", "github"),
        "prs": prs,
        "status": {repo: "OPEN" for repo in repos},
        "sibling_edits": sibling_edits,
    }
    path = _save_group(workspace_root, group)

    emit_after_outcome(
        event_type=EventType.PR_CREATED,
        workspace_root=workspace_root,
        actor=actor,
        owner_unit=owner_unit,
        payload={"pr_group_id": pr_group_id, "lane_name": lane_name, "repos": prs},
    )

    group["state_path"] = str(path)
    if unlinked:
        raise SiblingLinkError(group, unlinked)
    return group


def merge_pr_group(
    workspace_root: Path,
    pr_group_id: str,
    adapter: PlatformAdapter,
    actor: str,
    *,
    method: MergeMethod,
    verification_targets: Mapping[str, MergeVerificationTarget],
    report: Callable[[str], Any],
) -> dict:
    """Merge all PRs, consuming host evidence before recording completion."""
    group = _load_group(workspace_root, pr_group_id)
    merged: list[CompletedMerge] = []
    targets = dict(verification_targets)

    missing_targets = [
        str(item["repo"]) for item in group["prs"] if str(item["repo"]) not in targets
    ]
    if missing_targets:
        raise ValueError(
            "merge verification has no explicit local target for: "
            + ", ".join(missing_targets)
        )

    for pr_info in group["prs"]:
        repo = str(pr_info["repo"])
        number = int(pr_info["pr_number"])
        try:
            receipt = adapter.merge_pr(repo, number, method=method)
        except MergeEvidenceError as exc:
            _record_merge_failure(
                workspace_root=workspace_root,
                group=group,
                actor=actor,
                repo=repo,
                number=number,
                reason=str(exc),
                completed=merged,
                operation_acknowledged=True,
            )
            raise PRMergeOutcomeUnknownError(
                repo,
                number,
                str(exc),
                completed=merged,
            ) from exc
        except AdapterError as exc:
            _record_merge_failure(
                workspace_root=workspace_root,
                group=group,
                actor=actor,
                repo=repo,
                number=number,
                reason=str(exc),
                completed=merged,
                operation_acknowledged=False,
            )
            raise PRMergeError(repo, number, str(exc), completed=merged) from exc

        target = targets[repo]
        try:
            completed = consume_merge_receipt(
                repo_root=target.repo_root,
                receipt=receipt,
                remote=target.remote,
                report=report,
            )
        except Exception as exc:  # noqa: BLE001 - host operation already happened
            reason = f"could not consume merge evidence: {exc}"
            _record_merge_failure(
                workspace_root=workspace_root,
                group=group,
                actor=actor,
                repo=repo,
                number=number,
                reason=reason,
                completed=merged,
                operation_acknowledged=True,
            )
            raise PRMergePostconditionError(
                receipt,
                reason,
                completed=merged,
            ) from exc
        merged.append(completed)

    records = _completed_records(merged)

    group["completed"] = records
    group["group_state"] = "merged"
    _save_group(workspace_root, group)

    emit_after_outcome(
        event_type=EventType.PR_MERGED,
        workspace_root=workspace_root,
        actor=actor,
        owner_unit=group.get("owner_unit", actor),
        payload={"pr_group_id": pr_group_id, "repos": records},
    )

    return group


def _record_merge_failure(
    *,
    workspace_root: Path,
    group: dict[str, object],
    actor: str,
    repo: str,
    number: int,
    reason: str,
    completed: list[CompletedMerge],
    operation_acknowledged: bool,
) -> None:
    """Best-effort durable layer; never replaces the in-process error."""

    try:
        emit(
            event_type=EventType.PR_MERGE_FAILED,
            workspace_root=workspace_root,
            actor=actor,
            owner_unit=str(group.get("owner_unit", actor)),
            payload={
                "pr_group_id": group["pr_group_id"],
                "repo": repo,
                "pr_number": number,
                "reason": reason,
                "completed": _completed_records(completed),
                "operation_acknowledged": operation_acknowledged,
            },
        )
    except Exception as emit_exc:  # noqa: BLE001 - event logging is best effort
        print(
            f"gr2: could not record partial merge ({emit_exc}); "
            "the completed list remains on the raised error",
            file=sys.stderr,
        )


def _completed_records(completed: list[CompletedMerge]) -> list[dict[str, object]]:
    """Flatten earned evidence only at a JSON serialization boundary."""

    return [item.as_dict() for item in completed]


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
    """Record an externally-submitted PR review and emit pr.review_submitted."""
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
