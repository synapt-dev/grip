"""Tests for PR lifecycle event emission.

Verifies that pr.py emits pr.created, pr.merged, pr.merge_failed,
pr.status_changed, pr.checks_passed, pr.checks_failed, and
pr.review_submitted events per HOOK-EVENT-CONTRACT.md section 3.2
(PR Lifecycle) and PR-LIFECYCLE.md.

Uses a FakeAdapter to avoid real GitHub calls.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gr2.python_cli.platform import (
    AdapterError,
    CreatePRRequest,
    PRCheck,
    PRRef,
    PRStatus,
)


class FakeAdapter:
    """Test double for PlatformAdapter. Records calls, returns canned data."""

    name = "fake"

    def __init__(self) -> None:
        self.created: list[CreatePRRequest] = []
        self.merged: list[tuple[str, int]] = []
        self.statuses: dict[tuple[str, int], PRStatus] = {}
        self._fail_merge: set[tuple[str, int]] = set()

    def create_pr(self, request: CreatePRRequest) -> PRRef:
        self.created.append(request)
        n = len(self.created) + 100
        return PRRef(
            repo=request.repo,
            number=n,
            url=f"https://github.com/test/{request.repo}/pull/{n}",
            head_branch=request.head_branch,
            base_branch=request.base_branch,
            title=request.title,
        )

    def merge_pr(self, repo: str, number: int) -> PRRef:
        if (repo, number) in self._fail_merge:
            raise AdapterError(f"merge conflict in {repo}#{number}")
        self.merged.append((repo, number))
        return PRRef(repo=repo, number=number)

    def pr_status(self, repo: str, number: int) -> PRStatus:
        key = (repo, number)
        if key in self.statuses:
            return self.statuses[key]
        return PRStatus(
            ref=PRRef(repo=repo, number=number),
            state="OPEN",
            checks=[],
        )

    def list_prs(self, repo: str, *, head_branch: str | None = None) -> list[PRRef]:
        return []

    def pr_checks(self, repo: str, number: int) -> list[PRCheck]:
        return self.pr_status(repo, number).checks

    def set_fail_merge(self, repo: str, number: int) -> None:
        self._fail_merge.add((repo, number))

    def set_status(self, repo: str, number: int, status: PRStatus) -> None:
        self.statuses[(repo, number)] = status


def _read_outbox(workspace: Path) -> list[dict]:
    outbox = workspace / ".grip" / "events" / "outbox.jsonl"
    if not outbox.exists():
        return []
    lines = outbox.read_text().strip().split("\n")
    return [json.loads(line) for line in lines if line.strip()]


def _events_of_type(workspace: Path, event_type: str) -> list[dict]:
    return [e for e in _read_outbox(workspace) if e["type"] == event_type]


# ---------------------------------------------------------------------------
# 1. pr.created (section 3.2, PR-LIFECYCLE.md section 3.1)
# ---------------------------------------------------------------------------

class TestPRCreated:

    def test_emits_pr_created(self, workspace: Path):
        from gr2.python_cli.pr import create_pr_group
        adapter = FakeAdapter()
        result = create_pr_group(
            workspace_root=workspace,
            owner_unit="apollo",
            lane_name="feat/hook-events",
            title="feat: hook events",
            base_branch="sprint-21",
            head_branch="test/event-system-runtime",
            repos=["grip", "synapt"],
            adapter=adapter,
            actor="agent:apollo",
        )
        events = _events_of_type(workspace, "pr.created")
        assert len(events) == 1

    def test_pr_created_payload(self, workspace: Path):
        from gr2.python_cli.pr import create_pr_group
        adapter = FakeAdapter()
        result = create_pr_group(
            workspace_root=workspace,
            owner_unit="apollo",
            lane_name="feat/hook-events",
            title="feat: hook events",
            base_branch="sprint-21",
            head_branch="test/event-system-runtime",
            repos=["grip", "synapt"],
            adapter=adapter,
            actor="agent:apollo",
        )
        event = _events_of_type(workspace, "pr.created")[0]
        assert "pr_group_id" in event
        assert isinstance(event["repos"], list)
        assert len(event["repos"]) == 2
        for pr in event["repos"]:
            assert "repo" in pr
            assert "pr_number" in pr
            assert "url" in pr

    def test_pr_group_id_format(self, workspace: Path):
        from gr2.python_cli.pr import create_pr_group
        adapter = FakeAdapter()
        result = create_pr_group(
            workspace_root=workspace,
            owner_unit="apollo",
            lane_name="feat/hook-events",
            title="feat: hook events",
            base_branch="sprint-21",
            head_branch="test/event-system-runtime",
            repos=["grip"],
            adapter=adapter,
            actor="agent:apollo",
        )
        event = _events_of_type(workspace, "pr.created")[0]
        gid = event["pr_group_id"]
        assert gid.startswith("pg_")
        assert len(gid) == 11  # pg_ + 8 hex chars
        assert all(c in "0123456789abcdef" for c in gid[3:])

    def test_pr_group_metadata_stored(self, workspace: Path):
        from gr2.python_cli.pr import create_pr_group
        adapter = FakeAdapter()
        result = create_pr_group(
            workspace_root=workspace,
            owner_unit="apollo",
            lane_name="feat/hook-events",
            title="feat: hook events",
            base_branch="sprint-21",
            head_branch="test/event-system-runtime",
            repos=["grip"],
            adapter=adapter,
            actor="agent:apollo",
        )
        gid = result["pr_group_id"]
        meta_path = workspace / ".grip" / "pr_groups" / f"{gid}.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["pr_group_id"] == gid
        assert meta["lane_name"] == "feat/hook-events"

    def test_calls_adapter_per_repo(self, workspace: Path):
        from gr2.python_cli.pr import create_pr_group
        adapter = FakeAdapter()
        create_pr_group(
            workspace_root=workspace,
            owner_unit="apollo",
            lane_name="feat/hook-events",
            title="feat: hook events",
            base_branch="sprint-21",
            head_branch="test/event-system-runtime",
            repos=["grip", "synapt", "synapt-private"],
            adapter=adapter,
            actor="agent:apollo",
        )
        assert len(adapter.created) == 3
        assert [r.repo for r in adapter.created] == ["grip", "synapt", "synapt-private"]


# ---------------------------------------------------------------------------
# 2. pr.merged (section 3.2, PR-LIFECYCLE.md section 3.3)
# ---------------------------------------------------------------------------

class TestPRMerged:

    def _create_group(self, workspace: Path, adapter: FakeAdapter, repos: list[str] | None = None) -> dict:
        from gr2.python_cli.pr import create_pr_group
        return create_pr_group(
            workspace_root=workspace,
            owner_unit="apollo",
            lane_name="feat/test",
            title="feat: test",
            base_branch="sprint-21",
            head_branch="feat/test",
            repos=repos or ["grip", "synapt"],
            adapter=adapter,
            actor="agent:apollo",
        )

    def test_emits_pr_merged(self, workspace: Path):
        from gr2.python_cli.pr import merge_pr_group
        adapter = FakeAdapter()
        group = self._create_group(workspace, adapter)
        merge_pr_group(
            workspace_root=workspace,
            pr_group_id=group["pr_group_id"],
            adapter=adapter,
            actor="agent:apollo",
        )
        events = _events_of_type(workspace, "pr.merged")
        assert len(events) == 1

    def test_pr_merged_payload(self, workspace: Path):
        from gr2.python_cli.pr import merge_pr_group
        adapter = FakeAdapter()
        group = self._create_group(workspace, adapter)
        merge_pr_group(
            workspace_root=workspace,
            pr_group_id=group["pr_group_id"],
            adapter=adapter,
            actor="agent:apollo",
        )
        event = _events_of_type(workspace, "pr.merged")[0]
        assert event["pr_group_id"] == group["pr_group_id"]
        assert isinstance(event["repos"], list)
        assert len(event["repos"]) == 2

    def test_merges_in_repo_order(self, workspace: Path):
        from gr2.python_cli.pr import merge_pr_group
        adapter = FakeAdapter()
        group = self._create_group(workspace, adapter, repos=["grip", "synapt", "synapt-private"])
        merge_pr_group(
            workspace_root=workspace,
            pr_group_id=group["pr_group_id"],
            adapter=adapter,
            actor="agent:apollo",
        )
        assert [r for r, _ in adapter.merged] == ["grip", "synapt", "synapt-private"]


# ---------------------------------------------------------------------------
# 3. pr.merge_failed (section 3.2, PR-LIFECYCLE.md section 4.4)
# ---------------------------------------------------------------------------

class TestPRMergeFailed:

    def _create_group(self, workspace: Path, adapter: FakeAdapter) -> dict:
        from gr2.python_cli.pr import create_pr_group
        return create_pr_group(
            workspace_root=workspace,
            owner_unit="apollo",
            lane_name="feat/test",
            title="feat: test",
            base_branch="sprint-21",
            head_branch="feat/test",
            repos=["grip", "synapt"],
            adapter=adapter,
            actor="agent:apollo",
        )

    def test_emits_merge_failed(self, workspace: Path):
        from gr2.python_cli.pr import merge_pr_group, PRMergeError
        adapter = FakeAdapter()
        group = self._create_group(workspace, adapter)
        # Make synapt fail
        synapt_pr = [p for p in group["prs"] if p["repo"] == "synapt"][0]
        adapter.set_fail_merge("synapt", synapt_pr["pr_number"])
        with pytest.raises(PRMergeError):
            merge_pr_group(
                workspace_root=workspace,
                pr_group_id=group["pr_group_id"],
                adapter=adapter,
                actor="agent:apollo",
            )
        events = _events_of_type(workspace, "pr.merge_failed")
        assert len(events) == 1

    def test_merge_failed_payload(self, workspace: Path):
        from gr2.python_cli.pr import merge_pr_group, PRMergeError
        adapter = FakeAdapter()
        group = self._create_group(workspace, adapter)
        synapt_pr = [p for p in group["prs"] if p["repo"] == "synapt"][0]
        adapter.set_fail_merge("synapt", synapt_pr["pr_number"])
        with pytest.raises(PRMergeError):
            merge_pr_group(
                workspace_root=workspace,
                pr_group_id=group["pr_group_id"],
                adapter=adapter,
                actor="agent:apollo",
            )
        event = _events_of_type(workspace, "pr.merge_failed")[0]
        assert event["pr_group_id"] == group["pr_group_id"]
        assert event["repo"] == "synapt"
        assert "reason" in event

    def test_stops_after_first_failure(self, workspace: Path):
        """Merge stops at first failure; remaining repos are not attempted."""
        from gr2.python_cli.pr import create_pr_group, merge_pr_group, PRMergeError
        adapter = FakeAdapter()
        group = create_pr_group(
            workspace_root=workspace,
            owner_unit="apollo",
            lane_name="feat/test",
            title="feat: test",
            base_branch="sprint-21",
            head_branch="feat/test",
            repos=["grip", "synapt", "synapt-private"],
            adapter=adapter,
            actor="agent:apollo",
        )
        # Make grip (first repo) fail
        grip_pr = [p for p in group["prs"] if p["repo"] == "grip"][0]
        adapter.set_fail_merge("grip", grip_pr["pr_number"])
        with pytest.raises(PRMergeError):
            merge_pr_group(
                workspace_root=workspace,
                pr_group_id=group["pr_group_id"],
                adapter=adapter,
                actor="agent:apollo",
            )
        # Only grip was attempted; synapt and synapt-private were not
        assert len(adapter.merged) == 0  # grip failed, not in merged list
        assert len(_events_of_type(workspace, "pr.merged")) == 0


# ---------------------------------------------------------------------------
# 4. pr.status_changed, pr.checks_passed, pr.checks_failed
# ---------------------------------------------------------------------------

class TestPRStatusEvents:

    def _create_group(self, workspace: Path, adapter: FakeAdapter) -> dict:
        from gr2.python_cli.pr import create_pr_group
        return create_pr_group(
            workspace_root=workspace,
            owner_unit="apollo",
            lane_name="feat/test",
            title="feat: test",
            base_branch="sprint-21",
            head_branch="feat/test",
            repos=["grip"],
            adapter=adapter,
            actor="agent:apollo",
        )

    def test_checks_passed_emitted(self, workspace: Path):
        from gr2.python_cli.pr import create_pr_group, check_pr_group_status
        adapter = FakeAdapter()
        group = self._create_group(workspace, adapter)
        grip_pr = group["prs"][0]
        # Set checks to all passing
        adapter.set_status("grip", grip_pr["pr_number"], PRStatus(
            ref=PRRef(repo="grip", number=grip_pr["pr_number"]),
            state="OPEN",
            checks=[
                PRCheck(name="ci/test", status="COMPLETED", conclusion="SUCCESS"),
                PRCheck(name="ci/lint", status="COMPLETED", conclusion="SUCCESS"),
            ],
        ))
        check_pr_group_status(
            workspace_root=workspace,
            pr_group_id=group["pr_group_id"],
            adapter=adapter,
            actor="agent:apollo",
        )
        events = _events_of_type(workspace, "pr.checks_passed")
        assert len(events) == 1
        assert events[0]["repo"] == "grip"
        assert events[0]["pr_group_id"] == group["pr_group_id"]

    def test_checks_failed_emitted(self, workspace: Path):
        from gr2.python_cli.pr import create_pr_group, check_pr_group_status
        adapter = FakeAdapter()
        group = self._create_group(workspace, adapter)
        grip_pr = group["prs"][0]
        adapter.set_status("grip", grip_pr["pr_number"], PRStatus(
            ref=PRRef(repo="grip", number=grip_pr["pr_number"]),
            state="OPEN",
            checks=[
                PRCheck(name="ci/test", status="COMPLETED", conclusion="FAILURE"),
                PRCheck(name="ci/lint", status="COMPLETED", conclusion="SUCCESS"),
            ],
        ))
        check_pr_group_status(
            workspace_root=workspace,
            pr_group_id=group["pr_group_id"],
            adapter=adapter,
            actor="agent:apollo",
        )
        events = _events_of_type(workspace, "pr.checks_failed")
        assert len(events) == 1
        assert events[0]["repo"] == "grip"
        assert "ci/test" in events[0]["failed_checks"]

    def test_status_changed_emitted(self, workspace: Path):
        from gr2.python_cli.pr import create_pr_group, check_pr_group_status
        adapter = FakeAdapter()
        group = self._create_group(workspace, adapter)
        grip_pr = group["prs"][0]
        adapter.set_status("grip", grip_pr["pr_number"], PRStatus(
            ref=PRRef(repo="grip", number=grip_pr["pr_number"]),
            state="MERGED",
            checks=[],
        ))
        check_pr_group_status(
            workspace_root=workspace,
            pr_group_id=group["pr_group_id"],
            adapter=adapter,
            actor="agent:apollo",
        )
        events = _events_of_type(workspace, "pr.status_changed")
        assert len(events) == 1
        assert events[0]["repo"] == "grip"
        assert events[0]["new_status"] == "MERGED"

    def test_no_event_when_status_unchanged(self, workspace: Path):
        """Second status check with no changes emits no events."""
        from gr2.python_cli.pr import create_pr_group, check_pr_group_status
        adapter = FakeAdapter()
        group = self._create_group(workspace, adapter)
        # Default status is OPEN with no checks -- first check caches it
        check_pr_group_status(
            workspace_root=workspace,
            pr_group_id=group["pr_group_id"],
            adapter=adapter,
            actor="agent:apollo",
        )
        events_before = len(_read_outbox(workspace))
        # Second check, same status
        check_pr_group_status(
            workspace_root=workspace,
            pr_group_id=group["pr_group_id"],
            adapter=adapter,
            actor="agent:apollo",
        )
        events_after = len(_read_outbox(workspace))
        # No new status_changed events
        assert events_after == events_before


# ---------------------------------------------------------------------------
# 5. pr.review_submitted
# ---------------------------------------------------------------------------

class TestPRReviewSubmitted:

    def _create_group(self, workspace: Path, adapter: FakeAdapter) -> dict:
        from gr2.python_cli.pr import create_pr_group
        return create_pr_group(
            workspace_root=workspace,
            owner_unit="apollo",
            lane_name="feat/test",
            title="feat: test",
            base_branch="sprint-21",
            head_branch="feat/test",
            repos=["grip"],
            adapter=adapter,
            actor="agent:apollo",
        )

    def test_review_event_emitted(self, workspace: Path):
        from gr2.python_cli.pr import record_pr_review
        adapter = FakeAdapter()
        group = self._create_group(workspace, adapter)
        record_pr_review(
            workspace_root=workspace,
            pr_group_id=group["pr_group_id"],
            repo="grip",
            pr_number=group["prs"][0]["pr_number"],
            reviewer="agent:sentinel",
            state="APPROVED",
            actor="agent:sentinel",
        )
        events = _events_of_type(workspace, "pr.review_submitted")
        assert len(events) == 1

    def test_review_payload(self, workspace: Path):
        from gr2.python_cli.pr import record_pr_review
        adapter = FakeAdapter()
        group = self._create_group(workspace, adapter)
        record_pr_review(
            workspace_root=workspace,
            pr_group_id=group["pr_group_id"],
            repo="grip",
            pr_number=group["prs"][0]["pr_number"],
            reviewer="agent:sentinel",
            state="CHANGES_REQUESTED",
            actor="agent:sentinel",
        )
        event = _events_of_type(workspace, "pr.review_submitted")[0]
        assert event["pr_group_id"] == group["pr_group_id"]
        assert event["repo"] == "grip"
        assert event["pr_number"] == group["prs"][0]["pr_number"]
        assert event["reviewer"] == "agent:sentinel"
        assert event["state"] == "CHANGES_REQUESTED"
