"""Tests for gr2 event system runtime.

These tests define the contract from HOOK-EVENT-CONTRACT.md sections 3-8.
Written TDD-first: they must fail until events.py is implemented.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# 1. EventType enum (section 7.2)
# ---------------------------------------------------------------------------

class TestEventTypeEnum:
    """EventType enum must contain all 28 event types from the taxonomy."""

    def test_import(self):
        from gr2.python_cli.events import EventType
        assert EventType is not None

    def test_lane_lifecycle_types(self):
        from gr2.python_cli.events import EventType
        assert EventType.LANE_CREATED == "lane.created"
        assert EventType.LANE_ENTERED == "lane.entered"
        assert EventType.LANE_EXITED == "lane.exited"
        assert EventType.LANE_SWITCHED == "lane.switched"
        assert EventType.LANE_ARCHIVED == "lane.archived"

    def test_lease_lifecycle_types(self):
        from gr2.python_cli.events import EventType
        assert EventType.LEASE_ACQUIRED == "lease.acquired"
        assert EventType.LEASE_RELEASED == "lease.released"
        assert EventType.LEASE_EXPIRED == "lease.expired"
        assert EventType.LEASE_FORCE_BROKEN == "lease.force_broken"

    def test_hook_execution_types(self):
        from gr2.python_cli.events import EventType
        assert EventType.HOOK_STARTED == "hook.started"
        assert EventType.HOOK_COMPLETED == "hook.completed"
        assert EventType.HOOK_FAILED == "hook.failed"
        assert EventType.HOOK_SKIPPED == "hook.skipped"

    def test_pr_lifecycle_types(self):
        from gr2.python_cli.events import EventType
        assert EventType.PR_CREATED == "pr.created"
        assert EventType.PR_STATUS_CHANGED == "pr.status_changed"
        assert EventType.PR_CHECKS_PASSED == "pr.checks_passed"
        assert EventType.PR_CHECKS_FAILED == "pr.checks_failed"
        assert EventType.PR_REVIEW_SUBMITTED == "pr.review_submitted"
        assert EventType.PR_MERGED == "pr.merged"
        assert EventType.PR_MERGE_FAILED == "pr.merge_failed"

    def test_sync_operation_types(self):
        from gr2.python_cli.events import EventType
        assert EventType.SYNC_STARTED == "sync.started"
        assert EventType.SYNC_REPO_UPDATED == "sync.repo_updated"
        assert EventType.SYNC_REPO_SKIPPED == "sync.repo_skipped"
        assert EventType.SYNC_CONFLICT == "sync.conflict"
        assert EventType.SYNC_COMPLETED == "sync.completed"

    def test_recovery_types(self):
        from gr2.python_cli.events import EventType
        assert EventType.FAILURE_RESOLVED == "failure.resolved"
        assert EventType.LEASE_RECLAIMED == "lease.reclaimed"

    def test_workspace_operation_types(self):
        from gr2.python_cli.events import EventType
        assert EventType.WORKSPACE_MATERIALIZED == "workspace.materialized"
        assert EventType.WORKSPACE_FILE_PROJECTED == "workspace.file_projected"

    def test_total_count(self):
        from gr2.python_cli.events import EventType
        # 5 lane + 4 lease + 4 hook + 7 PR + 5 sync + 2 recovery + 2 workspace = 29
        assert len(EventType) == 29


# ---------------------------------------------------------------------------
# 2. emit() function (sections 4.2, 7.1)
# ---------------------------------------------------------------------------

class TestEmit:
    """emit() must produce flat JSONL events in .grip/events/outbox.jsonl."""

    def test_creates_outbox_file(self, workspace: Path):
        from gr2.python_cli.events import emit, EventType
        emit(
            event_type=EventType.LANE_ENTERED,
            workspace_root=workspace,
            actor="agent:apollo",
            owner_unit="apollo",
            payload={"lane_name": "feat/test", "lane_type": "feature", "repos": ["grip"]},
        )
        outbox = workspace / ".grip" / "events" / "outbox.jsonl"
        assert outbox.exists()

    def test_single_json_line(self, workspace: Path):
        from gr2.python_cli.events import emit, EventType
        emit(
            event_type=EventType.LANE_ENTERED,
            workspace_root=workspace,
            actor="agent:apollo",
            owner_unit="apollo",
            payload={"lane_name": "feat/test", "lane_type": "feature", "repos": ["grip"]},
        )
        outbox = workspace / ".grip" / "events" / "outbox.jsonl"
        lines = outbox.read_text().strip().split("\n")
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert isinstance(event, dict)

    def test_flat_envelope(self, workspace: Path):
        """Event must be flat: domain fields at top level, no nested payload."""
        from gr2.python_cli.events import emit, EventType
        emit(
            event_type=EventType.LANE_ENTERED,
            workspace_root=workspace,
            actor="agent:apollo",
            owner_unit="apollo",
            payload={"lane_name": "feat/test", "lane_type": "feature", "repos": ["grip"]},
        )
        outbox = workspace / ".grip" / "events" / "outbox.jsonl"
        event = json.loads(outbox.read_text().strip())
        # Domain fields must be top-level
        assert event["lane_name"] == "feat/test"
        assert event["lane_type"] == "feature"
        assert event["repos"] == ["grip"]
        # No nested payload key
        assert "payload" not in event

    def test_envelope_fields(self, workspace: Path):
        """Envelope fields: version, event_id, seq, timestamp, type."""
        from gr2.python_cli.events import emit, EventType
        emit(
            event_type=EventType.LANE_ENTERED,
            workspace_root=workspace,
            actor="agent:apollo",
            owner_unit="apollo",
            payload={"lane_name": "feat/test", "lane_type": "feature", "repos": ["grip"]},
        )
        outbox = workspace / ".grip" / "events" / "outbox.jsonl"
        event = json.loads(outbox.read_text().strip())
        assert event["version"] == 1
        assert event["type"] == "lane.entered"
        assert "event_id" in event
        assert "seq" in event
        assert "timestamp" in event

    def test_event_id_format(self, workspace: Path):
        """event_id must be 16-char hex from os.urandom(8).hex()."""
        from gr2.python_cli.events import emit, EventType
        emit(
            event_type=EventType.LANE_ENTERED,
            workspace_root=workspace,
            actor="agent:apollo",
            owner_unit="apollo",
            payload={"lane_name": "feat/test", "lane_type": "feature", "repos": ["grip"]},
        )
        outbox = workspace / ".grip" / "events" / "outbox.jsonl"
        event = json.loads(outbox.read_text().strip())
        event_id = event["event_id"]
        assert len(event_id) == 16
        assert all(c in "0123456789abcdef" for c in event_id)

    def test_context_fields(self, workspace: Path):
        """Context fields: workspace, actor, owner_unit."""
        from gr2.python_cli.events import emit, EventType
        emit(
            event_type=EventType.LANE_ENTERED,
            workspace_root=workspace,
            actor="agent:apollo",
            owner_unit="apollo",
            payload={"lane_name": "feat/test", "lane_type": "feature", "repos": ["grip"]},
        )
        outbox = workspace / ".grip" / "events" / "outbox.jsonl"
        event = json.loads(outbox.read_text().strip())
        assert event["workspace"] == workspace.name
        assert event["actor"] == "agent:apollo"
        assert event["owner_unit"] == "apollo"

    def test_optional_agent_id(self, workspace: Path):
        """agent_id is included when provided, absent when not."""
        from gr2.python_cli.events import emit, EventType
        emit(
            event_type=EventType.LANE_ENTERED,
            workspace_root=workspace,
            actor="agent:apollo",
            owner_unit="apollo",
            agent_id="agent_apollo_xyz789",
            payload={"lane_name": "feat/test", "lane_type": "feature", "repos": ["grip"]},
        )
        outbox = workspace / ".grip" / "events" / "outbox.jsonl"
        event = json.loads(outbox.read_text().strip())
        assert event["agent_id"] == "agent_apollo_xyz789"

    def test_agent_id_absent_when_not_provided(self, workspace: Path):
        from gr2.python_cli.events import emit, EventType
        emit(
            event_type=EventType.LANE_ENTERED,
            workspace_root=workspace,
            actor="agent:apollo",
            owner_unit="apollo",
            payload={"lane_name": "feat/test", "lane_type": "feature", "repos": ["grip"]},
        )
        outbox = workspace / ".grip" / "events" / "outbox.jsonl"
        event = json.loads(outbox.read_text().strip())
        assert "agent_id" not in event

    def test_timestamp_is_iso8601_with_tz(self, workspace: Path):
        from gr2.python_cli.events import emit, EventType
        emit(
            event_type=EventType.LANE_ENTERED,
            workspace_root=workspace,
            actor="agent:apollo",
            owner_unit="apollo",
            payload={"lane_name": "feat/test", "lane_type": "feature", "repos": ["grip"]},
        )
        outbox = workspace / ".grip" / "events" / "outbox.jsonl"
        event = json.loads(outbox.read_text().strip())
        ts = datetime.fromisoformat(event["timestamp"])
        assert ts.tzinfo is not None

    def test_reserved_name_collision_raises(self, workspace: Path):
        """Payload keys must not collide with envelope/context field names."""
        from gr2.python_cli.events import emit, EventType
        with pytest.raises((ValueError, KeyError)):
            emit(
                event_type=EventType.LANE_ENTERED,
                workspace_root=workspace,
                actor="agent:apollo",
                owner_unit="apollo",
                payload={"version": 99, "lane_name": "feat/test"},
            )


# ---------------------------------------------------------------------------
# 3. Monotonic sequence numbers (section 4.2)
# ---------------------------------------------------------------------------

class TestSequenceNumbers:
    """seq must be strictly monotonically increasing, starting at 1."""

    def test_first_event_seq_is_1(self, workspace: Path):
        from gr2.python_cli.events import emit, EventType
        emit(
            event_type=EventType.LANE_ENTERED,
            workspace_root=workspace,
            actor="agent:apollo",
            owner_unit="apollo",
            payload={"lane_name": "feat/test", "lane_type": "feature", "repos": ["grip"]},
        )
        outbox = workspace / ".grip" / "events" / "outbox.jsonl"
        event = json.loads(outbox.read_text().strip())
        assert event["seq"] == 1

    def test_monotonic_across_multiple_emits(self, workspace: Path):
        from gr2.python_cli.events import emit, EventType
        for _ in range(5):
            emit(
                event_type=EventType.LANE_ENTERED,
                workspace_root=workspace,
                actor="agent:apollo",
                owner_unit="apollo",
                payload={"lane_name": "feat/test", "lane_type": "feature", "repos": ["grip"]},
            )
        outbox = workspace / ".grip" / "events" / "outbox.jsonl"
        lines = outbox.read_text().strip().split("\n")
        seqs = [json.loads(line)["seq"] for line in lines]
        assert seqs == [1, 2, 3, 4, 5]

    def test_unique_event_ids(self, workspace: Path):
        from gr2.python_cli.events import emit, EventType
        for _ in range(10):
            emit(
                event_type=EventType.LANE_ENTERED,
                workspace_root=workspace,
                actor="agent:apollo",
                owner_unit="apollo",
                payload={"lane_name": "feat/test", "lane_type": "feature", "repos": ["grip"]},
            )
        outbox = workspace / ".grip" / "events" / "outbox.jsonl"
        lines = outbox.read_text().strip().split("\n")
        ids = [json.loads(line)["event_id"] for line in lines]
        assert len(set(ids)) == 10


# ---------------------------------------------------------------------------
# 4. Outbox rotation (section 4.3)
# ---------------------------------------------------------------------------

class TestOutboxRotation:
    """Outbox rotates at 10MB threshold."""

    def test_rotation_creates_timestamped_archive(self, workspace: Path):
        from gr2.python_cli.events import emit, EventType, _outbox_path
        outbox = _outbox_path(workspace)
        # Write a large payload to push past 10MB
        outbox.parent.mkdir(parents=True, exist_ok=True)
        outbox.write_text("x" * (10 * 1024 * 1024 + 1))
        emit(
            event_type=EventType.LANE_ENTERED,
            workspace_root=workspace,
            actor="agent:apollo",
            owner_unit="apollo",
            payload={"lane_name": "feat/test", "lane_type": "feature", "repos": ["grip"]},
        )
        # Old file should be renamed to outbox.{timestamp}.jsonl
        archives = list(outbox.parent.glob("outbox.*.jsonl"))
        assert len(archives) == 1
        # New outbox should exist with the fresh event
        assert outbox.exists()
        event = json.loads(outbox.read_text().strip())
        assert event["type"] == "lane.entered"

    def test_seq_continues_after_rotation(self, workspace: Path):
        from gr2.python_cli.events import emit, EventType, _outbox_path
        outbox = _outbox_path(workspace)
        outbox.parent.mkdir(parents=True, exist_ok=True)
        # Write 5 fake events to set seq baseline
        lines = []
        for i in range(1, 6):
            lines.append(json.dumps({"seq": i, "type": "test"}))
        outbox.write_text("\n".join(lines) + "\n")
        # Pad to trigger rotation
        with outbox.open("a") as f:
            f.write("x" * (10 * 1024 * 1024))
        emit(
            event_type=EventType.LANE_ENTERED,
            workspace_root=workspace,
            actor="agent:apollo",
            owner_unit="apollo",
            payload={"lane_name": "feat/test", "lane_type": "feature", "repos": ["grip"]},
        )
        event = json.loads(outbox.read_text().strip())
        assert event["seq"] == 6  # continues from last seq


# ---------------------------------------------------------------------------
# 5. Cursor-based consumption (section 5.1)
# ---------------------------------------------------------------------------

class TestCursorModel:
    """Cursor-based reading for event consumers."""

    def test_read_events_from_empty_outbox(self, workspace: Path):
        from gr2.python_cli.events import read_events
        events = read_events(workspace, "test_consumer")
        assert events == []

    def test_read_events_returns_all_for_new_consumer(self, workspace: Path):
        from gr2.python_cli.events import emit, read_events, EventType
        for i in range(3):
            emit(
                event_type=EventType.LANE_ENTERED,
                workspace_root=workspace,
                actor="agent:apollo",
                owner_unit="apollo",
                payload={"lane_name": f"lane-{i}", "lane_type": "feature", "repos": ["grip"]},
            )
        events = read_events(workspace, "test_consumer")
        assert len(events) == 3
        assert [e["lane_name"] for e in events] == ["lane-0", "lane-1", "lane-2"]

    def test_cursor_advances_after_read(self, workspace: Path):
        from gr2.python_cli.events import emit, read_events, EventType
        for i in range(3):
            emit(
                event_type=EventType.LANE_ENTERED,
                workspace_root=workspace,
                actor="agent:apollo",
                owner_unit="apollo",
                payload={"lane_name": f"lane-{i}", "lane_type": "feature", "repos": ["grip"]},
            )
        # First read: get all 3
        events = read_events(workspace, "my_consumer")
        assert len(events) == 3
        # Second read: get nothing (cursor advanced)
        events = read_events(workspace, "my_consumer")
        assert len(events) == 0

    def test_cursor_only_returns_new_events(self, workspace: Path):
        from gr2.python_cli.events import emit, read_events, EventType
        emit(
            event_type=EventType.LANE_ENTERED,
            workspace_root=workspace,
            actor="agent:apollo",
            owner_unit="apollo",
            payload={"lane_name": "first", "lane_type": "feature", "repos": ["grip"]},
        )
        read_events(workspace, "my_consumer")
        # Emit more after cursor advanced
        emit(
            event_type=EventType.LANE_EXITED,
            workspace_root=workspace,
            actor="agent:apollo",
            owner_unit="apollo",
            payload={"lane_name": "first", "stashed_repos": []},
        )
        events = read_events(workspace, "my_consumer")
        assert len(events) == 1
        assert events[0]["type"] == "lane.exited"

    def test_cursor_file_created(self, workspace: Path):
        from gr2.python_cli.events import emit, read_events, EventType
        emit(
            event_type=EventType.LANE_ENTERED,
            workspace_root=workspace,
            actor="agent:apollo",
            owner_unit="apollo",
            payload={"lane_name": "feat/test", "lane_type": "feature", "repos": ["grip"]},
        )
        read_events(workspace, "test_consumer")
        cursor_file = workspace / ".grip" / "events" / "cursors" / "test_consumer.json"
        assert cursor_file.exists()
        cursor = json.loads(cursor_file.read_text())
        assert cursor["consumer"] == "test_consumer"
        assert cursor["last_seq"] == 1

    def test_independent_cursors(self, workspace: Path):
        """Different consumers maintain independent cursors."""
        from gr2.python_cli.events import emit, read_events, EventType
        for i in range(3):
            emit(
                event_type=EventType.LANE_ENTERED,
                workspace_root=workspace,
                actor="agent:apollo",
                owner_unit="apollo",
                payload={"lane_name": f"lane-{i}", "lane_type": "feature", "repos": ["grip"]},
            )
        # Consumer A reads all 3
        events_a = read_events(workspace, "consumer_a")
        assert len(events_a) == 3
        # Consumer B hasn't read yet, gets all 3
        events_b = read_events(workspace, "consumer_b")
        assert len(events_b) == 3


# ---------------------------------------------------------------------------
# 6. Outbox path helper (section 4.1)
# ---------------------------------------------------------------------------

class TestOutboxPath:

    def test_outbox_path(self, workspace: Path):
        from gr2.python_cli.events import _outbox_path
        assert _outbox_path(workspace) == workspace / ".grip" / "events" / "outbox.jsonl"


# ---------------------------------------------------------------------------
# 7. emit() error handling (section 10.1)
# ---------------------------------------------------------------------------

class TestEmitErrorHandling:

    def test_emit_does_not_raise_on_write_failure(self, workspace: Path):
        """emit() logs to stderr but does not crash on write failure."""
        from gr2.python_cli.events import emit, EventType
        # Make the events directory read-only to force a write failure
        events_dir = workspace / ".grip" / "events"
        events_dir.chmod(0o444)
        try:
            # Should not raise
            emit(
                event_type=EventType.LANE_ENTERED,
                workspace_root=workspace,
                actor="agent:apollo",
                owner_unit="apollo",
                payload={"lane_name": "feat/test", "lane_type": "feature", "repos": ["grip"]},
            )
        finally:
            events_dir.chmod(0o755)

    def test_emit_creates_events_dir_if_missing(self, workspace: Path):
        """emit() creates .grip/events/ if it doesn't exist."""
        from gr2.python_cli.events import emit, EventType
        events_dir = workspace / ".grip" / "events"
        # Remove the events directory
        events_dir.rmdir()
        emit(
            event_type=EventType.LANE_ENTERED,
            workspace_root=workspace,
            actor="agent:apollo",
            owner_unit="apollo",
            payload={"lane_name": "feat/test", "lane_type": "feature", "repos": ["grip"]},
        )
        assert (events_dir / "outbox.jsonl").exists()
