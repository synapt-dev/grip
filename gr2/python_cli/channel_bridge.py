"""gr2 channel bridge consumer.

Translates outbox events into channel messages per the mapping table in
HOOK-EVENT-CONTRACT.md section 8. Uses cursor-based consumption from
events.read_events().

The bridge is a pure function layer: format_event() maps an event dict to
a message string (or None), and run_bridge() orchestrates cursor reads and
posts via a caller-provided post_fn. This keeps the MCP/recall_channel
dependency out of the module and makes it fully testable.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from .events import read_events


_CONSUMER_NAME = "channel_bridge"


def format_event(event: dict[str, object]) -> str | None:
    """Apply the section 8 mapping table to produce a channel message.

    Returns None if the event type is not mapped (silently dropped).
    """
    etype = event.get("type", "")

    if etype == "lane.created":
        return (
            f"{event['actor']} created lane {event['lane_name']}"
            f" [{event.get('lane_type', 'unknown')}]"
            f" repos={event.get('repos', [])}"
        )

    if etype == "lane.entered":
        return f"{event['actor']} entered {event['owner_unit']}/{event['lane_name']}"

    if etype == "lane.exited":
        return f"{event['actor']} exited {event['owner_unit']}/{event['lane_name']}"

    if etype == "pr.created":
        repos = event.get("repos", [])
        if isinstance(repos, list) and repos and isinstance(repos[0], dict):
            repo_names = [r.get("repo", "") for r in repos]
        else:
            repo_names = repos
        return (
            f"{event['actor']} opened PR group {event['pr_group_id']}: {repo_names}"
        )

    if etype == "pr.merged":
        return f"{event['actor']} merged PR group {event['pr_group_id']}"

    if etype == "pr.checks_failed":
        failed = event.get("failed_checks", [])
        return f"CI failed on {event['repo']}#{event['pr_number']}: {failed}"

    if etype == "hook.failed":
        # Only blocking hook failures produce channel messages.
        if event.get("on_failure") != "block":
            return None
        return (
            f"Hook {event['hook_name']} failed in {event['repo']}"
            f" (blocking): {event.get('stderr_tail', '')}"
        )

    if etype == "sync.conflict":
        files = event.get("conflicting_files", [])
        return f"Sync conflict in {event['repo']}: {files}"

    if etype == "lease.force_broken":
        return (
            f"Lease on {event['lane_name']} force-broken"
            f" by {event['broken_by']}: {event.get('reason', '')}"
        )

    if etype == "failure.resolved":
        return (
            f"{event['resolved_by']} resolved failure"
            f" {event['operation_id']} on {event['lane_name']}"
        )

    if etype == "lease.reclaimed":
        return (
            f"Stale lease on {event['lane_name']} reclaimed"
            f" (was held by {event['previous_holder']})"
        )

    # Unmapped event type: silently dropped.
    return None


def run_bridge(
    workspace_root: Path,
    *,
    post_fn: Callable[[str], object],
) -> int:
    """Read new events from the outbox and post mapped messages.

    Uses the 'channel_bridge' cursor. Returns the number of messages posted.
    The post_fn receives formatted message strings; the caller decides how to
    deliver them (recall_channel, print, log, etc.).
    """
    events = read_events(workspace_root, _CONSUMER_NAME)
    posted = 0
    for event in events:
        msg = format_event(event)
        if msg is not None:
            post_fn(msg)
            posted += 1
    return posted
