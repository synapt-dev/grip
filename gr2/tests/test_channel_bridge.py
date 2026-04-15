"""Tests for gr2 channel bridge consumer.

Tests the event-to-channel-message mapping from HOOK-EVENT-CONTRACT.md
section 8. Written TDD-first.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# 1. format_event() message templates (section 8 mapping table)
# ---------------------------------------------------------------------------

class TestFormatEvent:
    """format_event() applies the mapping table to produce channel messages."""

    def test_lane_created(self):
        from gr2.python_cli.channel_bridge import format_event
        event = {
            "type": "lane.created",
            "actor": "agent:apollo",
            "owner_unit": "apollo",
            "lane_name": "feat/hook-events",
            "lane_type": "feature",
            "repos": ["grip", "synapt"],
        }
        msg = format_event(event)
        assert msg == "agent:apollo created lane feat/hook-events [feature] repos=['grip', 'synapt']"

    def test_lane_entered(self):
        from gr2.python_cli.channel_bridge import format_event
        event = {
            "type": "lane.entered",
            "actor": "agent:apollo",
            "owner_unit": "apollo",
            "lane_name": "feat/hook-events",
        }
        msg = format_event(event)
        assert msg == "agent:apollo entered apollo/feat/hook-events"

    def test_lane_exited(self):
        from gr2.python_cli.channel_bridge import format_event
        event = {
            "type": "lane.exited",
            "actor": "agent:apollo",
            "owner_unit": "apollo",
            "lane_name": "feat/hook-events",
        }
        msg = format_event(event)
        assert msg == "agent:apollo exited apollo/feat/hook-events"

    def test_pr_created(self):
        from gr2.python_cli.channel_bridge import format_event
        event = {
            "type": "pr.created",
            "actor": "agent:apollo",
            "pr_group_id": "pg_8a3f1b2c",
            "repos": [{"repo": "grip", "pr_number": 570}, {"repo": "synapt", "pr_number": 583}],
        }
        msg = format_event(event)
        assert "pg_8a3f1b2c" in msg
        assert "agent:apollo" in msg

    def test_pr_merged(self):
        from gr2.python_cli.channel_bridge import format_event
        event = {
            "type": "pr.merged",
            "actor": "agent:apollo",
            "pr_group_id": "pg_8a3f1b2c",
        }
        msg = format_event(event)
        assert msg == "agent:apollo merged PR group pg_8a3f1b2c"

    def test_pr_checks_failed(self):
        from gr2.python_cli.channel_bridge import format_event
        event = {
            "type": "pr.checks_failed",
            "repo": "grip",
            "pr_number": 574,
            "failed_checks": ["ci/test", "ci/lint"],
        }
        msg = format_event(event)
        assert "grip#574" in msg
        assert "ci/test" in msg

    def test_hook_failed_block(self):
        from gr2.python_cli.channel_bridge import format_event
        event = {
            "type": "hook.failed",
            "hook_name": "editable-install",
            "repo": "synapt",
            "on_failure": "block",
            "stderr_tail": "pip install failed",
        }
        msg = format_event(event)
        assert "editable-install" in msg
        assert "synapt" in msg
        assert "blocking" in msg

    def test_hook_failed_warn_not_mapped(self):
        """hook.failed with on_failure=warn should NOT produce a channel message."""
        from gr2.python_cli.channel_bridge import format_event
        event = {
            "type": "hook.failed",
            "hook_name": "lint",
            "repo": "synapt",
            "on_failure": "warn",
            "stderr_tail": "lint warnings",
        }
        msg = format_event(event)
        assert msg is None

    def test_hook_failed_skip_not_mapped(self):
        """hook.failed with on_failure=skip should NOT produce a channel message."""
        from gr2.python_cli.channel_bridge import format_event
        event = {
            "type": "hook.failed",
            "hook_name": "optional",
            "repo": "synapt",
            "on_failure": "skip",
            "stderr_tail": "skipped",
        }
        msg = format_event(event)
        assert msg is None

    def test_sync_conflict(self):
        from gr2.python_cli.channel_bridge import format_event
        event = {
            "type": "sync.conflict",
            "repo": "synapt",
            "conflicting_files": ["src/main.py", "tests/test_core.py"],
        }
        msg = format_event(event)
        assert "synapt" in msg
        assert "src/main.py" in msg

    def test_lease_force_broken(self):
        from gr2.python_cli.channel_bridge import format_event
        event = {
            "type": "lease.force_broken",
            "lane_name": "feat/hook-events",
            "broken_by": "agent:sentinel",
            "reason": "stale session",
        }
        msg = format_event(event)
        assert "feat/hook-events" in msg
        assert "agent:sentinel" in msg
        assert "stale session" in msg

    def test_failure_resolved(self):
        from gr2.python_cli.channel_bridge import format_event
        event = {
            "type": "failure.resolved",
            "resolved_by": "agent:apollo",
            "operation_id": "op_9f2a3b4c",
            "lane_name": "feat/hook-events",
        }
        msg = format_event(event)
        assert "agent:apollo" in msg
        assert "op_9f2a3b4c" in msg
        assert "feat/hook-events" in msg

    def test_lease_reclaimed(self):
        from gr2.python_cli.channel_bridge import format_event
        event = {
            "type": "lease.reclaimed",
            "lane_name": "feat/hook-events",
            "previous_holder": "agent:atlas",
        }
        msg = format_event(event)
        assert "feat/hook-events" in msg
        assert "agent:atlas" in msg


# ---------------------------------------------------------------------------
# 2. Unmapped events return None (section 8 exclusion list)
# ---------------------------------------------------------------------------

class TestUnmappedEvents:
    """Events not in the mapping table produce no channel message."""

    @pytest.mark.parametrize("event_type", [
        "hook.started",
        "hook.completed",
        "hook.skipped",
        "lease.acquired",
        "lease.released",
        "lease.expired",
        "sync.started",
        "sync.repo_updated",
        "sync.repo_skipped",
        "sync.completed",
        "workspace.materialized",
        "workspace.file_projected",
        "lane.switched",
        "lane.archived",
    ])
    def test_unmapped_returns_none(self, event_type):
        from gr2.python_cli.channel_bridge import format_event
        event = {"type": event_type, "actor": "agent:test", "owner_unit": "test"}
        assert format_event(event) is None


# ---------------------------------------------------------------------------
# 3. run_bridge() cursor-based consumption
# ---------------------------------------------------------------------------

class TestRunBridge:
    """run_bridge() reads events via cursor and calls post_fn for each."""

    def test_processes_mapped_events(self, workspace: Path):
        from gr2.python_cli.events import emit, EventType
        from gr2.python_cli.channel_bridge import run_bridge
        emit(
            event_type=EventType.LANE_ENTERED,
            workspace_root=workspace,
            actor="agent:apollo",
            owner_unit="apollo",
            payload={"lane_name": "feat/test", "lane_type": "feature", "repos": ["grip"]},
        )
        posted: list[str] = []
        run_bridge(workspace, post_fn=posted.append)
        assert len(posted) == 1
        assert "agent:apollo entered apollo/feat/test" in posted[0]

    def test_skips_unmapped_events(self, workspace: Path):
        from gr2.python_cli.events import emit, EventType
        from gr2.python_cli.channel_bridge import run_bridge
        emit(
            event_type=EventType.LEASE_ACQUIRED,
            workspace_root=workspace,
            actor="agent:apollo",
            owner_unit="apollo",
            payload={"lane_name": "feat/test", "mode": "edit", "ttl_seconds": 900, "lease_id": "x"},
        )
        posted: list[str] = []
        run_bridge(workspace, post_fn=posted.append)
        assert len(posted) == 0

    def test_cursor_advances(self, workspace: Path):
        """Second run_bridge call returns nothing if no new events."""
        from gr2.python_cli.events import emit, EventType
        from gr2.python_cli.channel_bridge import run_bridge
        emit(
            event_type=EventType.LANE_ENTERED,
            workspace_root=workspace,
            actor="agent:apollo",
            owner_unit="apollo",
            payload={"lane_name": "feat/test", "lane_type": "feature", "repos": ["grip"]},
        )
        posted: list[str] = []
        run_bridge(workspace, post_fn=posted.append)
        assert len(posted) == 1
        # Second call: cursor advanced, no new events
        posted.clear()
        run_bridge(workspace, post_fn=posted.append)
        assert len(posted) == 0

    def test_processes_only_new_events(self, workspace: Path):
        from gr2.python_cli.events import emit, EventType
        from gr2.python_cli.channel_bridge import run_bridge
        emit(
            event_type=EventType.LANE_ENTERED,
            workspace_root=workspace,
            actor="agent:apollo",
            owner_unit="apollo",
            payload={"lane_name": "first", "lane_type": "feature", "repos": ["grip"]},
        )
        posted: list[str] = []
        run_bridge(workspace, post_fn=posted.append)
        assert len(posted) == 1
        # Emit a new event
        emit(
            event_type=EventType.LANE_EXITED,
            workspace_root=workspace,
            actor="agent:apollo",
            owner_unit="apollo",
            payload={"lane_name": "first", "stashed_repos": []},
        )
        posted.clear()
        run_bridge(workspace, post_fn=posted.append)
        assert len(posted) == 1
        assert "exited" in posted[0]

    def test_mixed_mapped_and_unmapped(self, workspace: Path):
        """Only mapped events produce messages; unmapped are silently skipped."""
        from gr2.python_cli.events import emit, EventType
        from gr2.python_cli.channel_bridge import run_bridge
        emit(
            event_type=EventType.LANE_ENTERED,
            workspace_root=workspace,
            actor="agent:apollo",
            owner_unit="apollo",
            payload={"lane_name": "feat/test", "lane_type": "feature", "repos": ["grip"]},
        )
        emit(
            event_type=EventType.LEASE_ACQUIRED,
            workspace_root=workspace,
            actor="agent:apollo",
            owner_unit="apollo",
            payload={"lane_name": "feat/test", "mode": "edit", "ttl_seconds": 900, "lease_id": "x"},
        )
        emit(
            event_type=EventType.LANE_EXITED,
            workspace_root=workspace,
            actor="agent:apollo",
            owner_unit="apollo",
            payload={"lane_name": "feat/test", "stashed_repos": ["grip"]},
        )
        posted: list[str] = []
        run_bridge(workspace, post_fn=posted.append)
        # lane.entered and lane.exited are mapped; lease.acquired is not
        assert len(posted) == 2
        assert "entered" in posted[0]
        assert "exited" in posted[1]

    def test_returns_count(self, workspace: Path):
        """run_bridge returns the number of messages posted."""
        from gr2.python_cli.events import emit, EventType
        from gr2.python_cli.channel_bridge import run_bridge
        emit(
            event_type=EventType.LANE_ENTERED,
            workspace_root=workspace,
            actor="agent:apollo",
            owner_unit="apollo",
            payload={"lane_name": "feat/test", "lane_type": "feature", "repos": ["grip"]},
        )
        result = run_bridge(workspace, post_fn=lambda msg: None)
        assert result == 1
