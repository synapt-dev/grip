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
    "PRHeadPinError",
    "PRMergeError",
    "PRMergeGroupError",
    "PRMergeOutcomeUnknownError",
    "PRMergePostconditionError",
    "PRMergeTargetError",
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


class PRHeadPinError(PRMergeError):
    """A pinned member's head moved after the reads, so NOTHING merged.

    `completed` is always empty by construction: the pin pass runs before the
    first merge. That is the difference between this and a per-call check --
    an adapter refusing only the member it has reached has already merged the
    members before it, and a group that lands half of bytes nobody read is the
    defect the pin exists to prevent.
    """

    def __init__(
        self,
        repo: str,
        pr_number: int,
        *,
        expected: str,
        actual: str | None,
        completed: list[CompletedMerge],
    ) -> None:
        self.expected = expected
        self.actual = actual
        shown = actual[:8] if actual else "unreadable"
        reason = (
            f"head is {shown} ({actual or 'the adapter could not report it'}) but the "
            f"reviewed head was {expected[:8]} ({expected}): the branch moved after the "
            "reads, so merging would land bytes nobody reviewed, and an unreadable head "
            "is not the same as an unchanged one. Re-bind the review to the current head "
            "and read it again, or pass the head you actually read."
        )
        super().__init__(repo, pr_number, reason, completed=completed)


class PRMergeTargetError(PRMergeError):
    """A member has no explicit local verification target, so NOTHING merged.

    `completed` is always empty by construction: this is checked before the pin
    pass and before the first merge, so no member has been touched.

    It is a PRMergeError subclass rather than a bare ValueError because the
    merge verb's CLI handler catches PRMergeError and renders it as a sentence.
    As a ValueError this one reached the operator as a traceback -- on the verb
    whose whole point is to be the safe way to merge a group.
    """

    def __init__(
        self,
        repo: str,
        pr_number: int,
        *,
        missing: list[str],
        completed: list[CompletedMerge],
    ) -> None:
        self.missing = list(missing)
        listed = ", ".join(self.missing)
        reason = (
            f"no explicit local verification target for {listed}: merging would record "
            "a completion this run never verified against a local DAG, so nothing "
            "merged. Every member of the group needs a declared workspace repo with a "
            "live clone, and the group's members are the repos its `prs` entries name."
        )
        super().__init__(repo, pr_number, reason, completed=completed)


class PRMergeGroupError(PRMergeError):
    """The group cannot be read as written, so nothing was attempted.

    Raised before any conversion of the group's numbers. Three sites on this path call
    `int(pr_number)` -- the target check, the pin pass and the merge loop -- and an
    unreadable number makes `int()` raise ValueError or TypeError, neither of which the
    merge verb's CLI catches. Driven through the entry point with a member carrying
    "abc", the operator saw NOTHING at all: the exception propagates out of `main()`,
    so it is not a sentence and not even a rendered traceback.

    `pr_number` is carried UNREAD into the sentence, so the refusal can show what was
    actually there rather than a conversion error about it.
    """

    def __init__(
        self,
        repo: str,
        pr_number: object,
        *,
        completed: list[CompletedMerge],
        problem: str = "which is not an integer",
    ) -> None:
        reason = (
            f"the group's entry for {repo!r} carries pr_number {pr_number!r}, {problem}, "
            "so the group cannot be read. Nothing was attempted -- the whole entry is "
            "proven before anything reads it."
        )
        super().__init__(repo, pr_number, reason, completed=completed)  # type: ignore[arg-type]


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


def group_members(group: Mapping[str, object]) -> list[tuple[str, int]]:
    """Every member as `(repo, pr_number)`, proven before anything reads one.

    THE ENTRY, NOT ONLY THE VALUE, AND THE ONLY PLACE THIS REFUSAL IS MADE. `_load_group`
    is a bare `json.loads` with no schema validation, so a group file can carry an entry
    that is not an object or that lacks a key, and every read of a member assumes both.

    The CLI's pin parser runs `_parse_head_pins` BEFORE it calls this function, and used to
    read `item["repo"]` itself -- so the missing-`repo` shape left `main()` as a bare
    `KeyError`, printing nothing. It is tolerant now rather than refusing, and that is
    deliberate: this function's refusal is a `PRMergeError`, and the merge verb's
    `PRMergeError` handler prints the offending entry inside a JSON payload, which the pin
    parser's own `except ValueError` handler does not. Moving the refusal earlier would
    keep the sentence and silently drop the entry from the operator's view.

    A QUANTIFIER over the shape rather than an enumeration of the exceptions a conversion
    raises: `int()` accepts `True` and `7.9` in silence, so "unreadable" was narrower than
    "not an integer", and a list of exception types is short the moment a type is missed.
    """
    raw_prs = group.get("prs")
    if not isinstance(raw_prs, list):
        raise PRMergeGroupError(
            "<the group>", raw_prs, completed=[], problem="which is not a list of members"
        )
    members: list[tuple[str, int]] = []
    for entry in raw_prs:
        if not isinstance(entry, dict):
            raise PRMergeGroupError(
                "<an entry>", entry, completed=[], problem="which is not an object"
            )
        repo = entry.get("repo")
        number = entry.get("pr_number")
        if not isinstance(repo, str) or not repo:
            raise PRMergeGroupError(
                repr(repo), number, completed=[], problem="which is not a usable repo name"
            )
        if not isinstance(number, int) or isinstance(number, bool):
            raise PRMergeGroupError(repo, number, completed=[])
        members.append((repo, number))
    return members


def merge_pr_group(
    workspace_root: Path,
    pr_group_id: str,
    adapter: PlatformAdapter,
    actor: str,
    *,
    method: MergeMethod,
    verification_targets: Mapping[str, MergeVerificationTarget],
    report: Callable[[str], Any],
    expected_heads: Mapping[str, str] | None = None,
) -> dict:
    """Merge all PRs, consuming host evidence before recording completion.

    `expected_heads` pins a member's reviewed head COMMIT by repo. Every pinned
    member is checked before the first merge, so a group whose bytes moved under
    the reads lands nothing at all rather than every member whose head happened
    to still match.
    """
    group = _load_group(workspace_root, pr_group_id)
    merged: list[CompletedMerge] = []
    targets = dict(verification_targets)
    pins = dict(expected_heads or {})

    # Members are proven ONCE, before any of the sites below that used to convert a
    # number, so a malformed group is refused here and nowhere else.
    members = group_members(group)
    member_repos = [repo for repo, _ in members]
    unknown_pins = sorted(set(pins) - set(member_repos))
    if unknown_pins:
        # A pin that matches no member is a typo that leaves the real member
        # unpinned while reading as protection. Refuse rather than ignore.
        raise ValueError(
            "expected_heads names repo(s) not in this PR group: "
            + ", ".join(unknown_pins)
            + " (group carries: "
            + ", ".join(member_repos)
            + ")"
        )

    # The numbers come from the validated pairs, so no site on this path converts one
    # again: `int()` accepts `True` and `7.9` in silence, and the three reads that used
    # to convert here, in the pin pass and in the merge loop were three chances to
    # accept a number nobody wrote.
    missing_targets = [(repo, number) for repo, number in members if repo not in targets]
    if missing_targets:
        # A PRMergeError subclass, NOT a ValueError: the merge verb's CLI catches
        # PRMergeError and prints a sentence, so a bare ValueError raised here
        # reached the operator as a traceback. Nothing has merged at this point.
        raise PRMergeTargetError(
            missing_targets[0][0],
            missing_targets[0][1],
            missing=[repo for repo, _ in missing_targets],
            completed=[],
        )

    # THE PIN PASS. It runs over every member BEFORE the first merge, because a
    # per-call check refuses only the member the adapter has reached -- an
    # earlier member merges first, and half of a group that nobody read lands.
    for pr_info in group["prs"]:
        repo = str(pr_info["repo"])
        expected = pins.get(repo)
        if expected is None:
            continue
        number = int(pr_info["pr_number"])
        actual = adapter.pr_status(repo, number).head_oid
        if actual != expected:
            raise PRHeadPinError(
                repo,
                number,
                expected=expected,
                actual=actual,
                completed=[],
            )

    for pr_info in group["prs"]:
        repo = str(pr_info["repo"])
        number = int(pr_info["pr_number"])
        try:
            receipt = adapter.merge_pr(
                repo, number, method=method, expected_head=pins.get(repo)
            )
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
