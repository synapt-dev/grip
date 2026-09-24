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
from gr2.python_cli.events import EventEmitError
from gr2.python_cli.merge_verification import MergeVerificationTarget
from gr2.python_cli.platform import (
    AdapterError,
    CreatePRRequest,
    MergeMethod,
    MergeReceipt,
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

    def edit_pr_body(self, repo: str, number: int, body: str) -> None:
        """Recording no-op. This double is not about the sibling pass, but the
        adapter Protocol now declares the call, so a double that omits it fails the
        moment a group has more than one PR."""
        self.edited = getattr(self, "edited", [])
        self.edited.append((repo, number, body))

    def merge_pr(
        self,
        repo: str,
        number: int,
        *,
        method: MergeMethod,
    ) -> MergeReceipt:
        if (repo, number) in self._fail_merge:
            raise AdapterError(f"merge conflict in {repo}#{number}")
        self.merged.append((repo, number))
        return MergeReceipt(
            requested=PRRef(repo=repo, number=number),
            observed=PRRef(repo=repo, number=number, url=f"observed://{repo}/{number}"),
            commit_sha=None,
            requested_method=method,
        )

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


def _merge_contract(workspace: Path, group: dict) -> dict[str, object]:
    return {
        "method": MergeMethod.MERGE,
        "verification_targets": {
            str(item["repo"]): MergeVerificationTarget(
                repo_root=workspace / str(item["repo"]),
                remote="unused-for-missing-oid",
            )
            for item in group["prs"]
        },
        "report": lambda _message: None,
    }


# ---------------------------------------------------------------------------
# 1. pr.created (section 3.2, PR-LIFECYCLE.md section 3.1)
# ---------------------------------------------------------------------------

class TestPRCreated:

    def test_event_failure_does_not_erase_created_prs(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from gr2.python_cli import events as events_module
        from gr2.python_cli import pr as pr_module

        adapter = FakeAdapter()
        monkeypatch.setattr(
            events_module,
            "emit",
            lambda **_kwargs: (_ for _ in ()).throw(EventEmitError("event sink unavailable")),
        )

        result = pr_module.create_pr_group(
            workspace_root=workspace,
            owner_unit="apollo",
            lane_name="feat/hook-events",
            title="feat: hook events",
            base_branch="sprint-21",
            head_branch="test/event-system-runtime",
            repos=["app", "api"],
            adapter=adapter,
            actor="agent:apollo",
        )

        persisted = json.loads(Path(result["state_path"]).read_text())
        assert [request.repo for request in adapter.created] == ["app", "api"]
        assert persisted["prs"] == result["prs"]
        assert "could not record pr.created" in capsys.readouterr().err

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
            repos=["app", "api"],
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
            repos=["app", "api"],
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
            repos=["app"],
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
            repos=["app"],
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
            repos=["app", "api", "billing"],
            adapter=adapter,
            actor="agent:apollo",
        )
        assert len(adapter.created) == 3
        assert [r.repo for r in adapter.created] == ["app", "api", "billing"]


# ---------------------------------------------------------------------------
# 2. pr.merged (section 3.2, PR-LIFECYCLE.md section 3.3)
# ---------------------------------------------------------------------------

class TestPRMerged:

    def test_event_failure_does_not_erase_merged_outcome(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from gr2.python_cli import events as events_module
        from gr2.python_cli import pr as pr_module

        adapter = FakeAdapter()
        group = self._create_group(workspace, adapter)
        monkeypatch.setattr(
            events_module,
            "emit",
            lambda **_kwargs: (_ for _ in ()).throw(EventEmitError("event sink unavailable")),
        )

        result = pr_module.merge_pr_group(
            workspace_root=workspace,
            pr_group_id=group["pr_group_id"],
            adapter=adapter,
            actor="agent:apollo",
            **_merge_contract(workspace, group),
        )

        state_path = workspace / ".grip" / "pr_groups" / f"{group['pr_group_id']}.json"
        persisted = json.loads(state_path.read_text())
        assert [repo for repo, _number in adapter.merged] == ["app", "api"]
        assert result["group_state"] == "merged"
        assert persisted["group_state"] == "merged"
        assert "could not record pr.merged" in capsys.readouterr().err

    def _create_group(self, workspace: Path, adapter: FakeAdapter, repos: list[str] | None = None) -> dict:
        from gr2.python_cli.pr import create_pr_group
        return create_pr_group(
            workspace_root=workspace,
            owner_unit="apollo",
            lane_name="feat/test",
            title="feat: test",
            base_branch="sprint-21",
            head_branch="feat/test",
            repos=repos or ["app", "api"],
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
            **_merge_contract(workspace, group),
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
            **_merge_contract(workspace, group),
        )
        event = _events_of_type(workspace, "pr.merged")[0]
        assert event["pr_group_id"] == group["pr_group_id"]
        assert isinstance(event["repos"], list)
        assert len(event["repos"]) == 2

    def test_merges_in_repo_order(self, workspace: Path):
        from gr2.python_cli.pr import merge_pr_group
        adapter = FakeAdapter()
        group = self._create_group(workspace, adapter, repos=["app", "api", "billing"])
        merge_pr_group(
            workspace_root=workspace,
            pr_group_id=group["pr_group_id"],
            adapter=adapter,
            actor="agent:apollo",
            **_merge_contract(workspace, group),
        )
        assert [r for r, _ in adapter.merged] == ["app", "api", "billing"]


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
            repos=["app", "api"],
            adapter=adapter,
            actor="agent:apollo",
        )

    def test_emits_merge_failed(self, workspace: Path):
        from gr2.python_cli.pr import PRMergeError, merge_pr_group
        adapter = FakeAdapter()
        group = self._create_group(workspace, adapter)
        # Make api fail
        api_pr = [p for p in group["prs"] if p["repo"] == "api"][0]
        adapter.set_fail_merge("api", api_pr["pr_number"])
        with pytest.raises(PRMergeError):
            merge_pr_group(
                workspace_root=workspace,
                pr_group_id=group["pr_group_id"],
                adapter=adapter,
                actor="agent:apollo",
                **_merge_contract(workspace, group),
            )
        events = _events_of_type(workspace, "pr.merge_failed")
        assert len(events) == 1

    def test_merge_failed_payload(self, workspace: Path):
        from gr2.python_cli.pr import PRMergeError, merge_pr_group
        adapter = FakeAdapter()
        group = self._create_group(workspace, adapter)
        api_pr = [p for p in group["prs"] if p["repo"] == "api"][0]
        adapter.set_fail_merge("api", api_pr["pr_number"])
        with pytest.raises(PRMergeError):
            merge_pr_group(
                workspace_root=workspace,
                pr_group_id=group["pr_group_id"],
                adapter=adapter,
                actor="agent:apollo",
                **_merge_contract(workspace, group),
            )
        event = _events_of_type(workspace, "pr.merge_failed")[0]
        assert event["pr_group_id"] == group["pr_group_id"]
        assert event["repo"] == "api"
        assert "reason" in event
        assert [item["repo"] for item in event["completed"]] == ["app"]

    def test_stops_after_first_failure(self, workspace: Path):
        """Merge stops at first failure; remaining repos are not attempted."""
        from gr2.python_cli.pr import PRMergeError, create_pr_group, merge_pr_group
        adapter = FakeAdapter()
        group = create_pr_group(
            workspace_root=workspace,
            owner_unit="apollo",
            lane_name="feat/test",
            title="feat: test",
            base_branch="sprint-21",
            head_branch="feat/test",
            repos=["app", "api", "billing"],
            adapter=adapter,
            actor="agent:apollo",
        )
        # Make grip (first repo) fail
        grip_pr = [p for p in group["prs"] if p["repo"] == "app"][0]
        adapter.set_fail_merge("app", grip_pr["pr_number"])
        with pytest.raises(PRMergeError) as raised:
            merge_pr_group(
                workspace_root=workspace,
                pr_group_id=group["pr_group_id"],
                adapter=adapter,
                actor="agent:apollo",
                **_merge_contract(workspace, group),
            )
        # Only app was attempted; api and billing were not
        assert raised.value.completed == []
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
            repos=["app"],
            adapter=adapter,
            actor="agent:apollo",
        )

    def test_checks_passed_emitted(self, workspace: Path):
        from gr2.python_cli.pr import check_pr_group_status, create_pr_group
        adapter = FakeAdapter()
        group = self._create_group(workspace, adapter)
        grip_pr = group["prs"][0]
        # Set checks to all passing
        adapter.set_status("app", grip_pr["pr_number"], PRStatus(
            ref=PRRef(repo="app", number=grip_pr["pr_number"]),
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
        assert events[0]["repo"] == "app"
        assert events[0]["pr_group_id"] == group["pr_group_id"]

    def test_checks_failed_emitted(self, workspace: Path):
        from gr2.python_cli.pr import check_pr_group_status, create_pr_group
        adapter = FakeAdapter()
        group = self._create_group(workspace, adapter)
        grip_pr = group["prs"][0]
        adapter.set_status("app", grip_pr["pr_number"], PRStatus(
            ref=PRRef(repo="app", number=grip_pr["pr_number"]),
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
        assert events[0]["repo"] == "app"
        assert "ci/test" in events[0]["failed_checks"]

    def test_status_changed_emitted(self, workspace: Path):
        from gr2.python_cli.pr import check_pr_group_status, create_pr_group
        adapter = FakeAdapter()
        group = self._create_group(workspace, adapter)
        grip_pr = group["prs"][0]
        adapter.set_status("app", grip_pr["pr_number"], PRStatus(
            ref=PRRef(repo="app", number=grip_pr["pr_number"]),
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
        assert events[0]["repo"] == "app"
        assert events[0]["new_status"] == "MERGED"

    def test_no_event_when_status_unchanged(self, workspace: Path):
        """Second status check with no changes emits no events."""
        from gr2.python_cli.pr import check_pr_group_status, create_pr_group
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
            repos=["app"],
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
            repo="app",
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
            repo="app",
            pr_number=group["prs"][0]["pr_number"],
            reviewer="agent:sentinel",
            state="CHANGES_REQUESTED",
            actor="agent:sentinel",
        )
        event = _events_of_type(workspace, "pr.review_submitted")[0]
        assert event["pr_group_id"] == group["pr_group_id"]
        assert event["repo"] == "app"
        assert event["pr_number"] == group["prs"][0]["pr_number"]
        assert event["reviewer"] == "agent:sentinel"
        assert event["state"] == "CHANGES_REQUESTED"
