"""gr2 channel bridge consumer.

Translates outbox events into channel messages per the mapping table in
HOOK-EVENT-CONTRACT.md section 8. Uses cursor-based consumption from
events.read_events_detailed(), which also reports lines it could not read.

The bridge is a pure function layer: format_event() maps an event dict to
a message string (or None), and run_bridge() orchestrates cursor reads and
posts via a caller-provided post_fn. This keeps the MCP/recall_channel
dependency out of the module and makes it fully testable.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

from .events import ack_events, read_events_detailed, warn_unreadable


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

    Uses the 'channel_bridge' cursor, which moves only after each event's effect, so delivery
    is at-least-once: a post_fn that raises leaves that event and the ones behind it to be
    offered again on the next run (the post_fn's failure propagates). Returns the number of
    messages posted.
    The post_fn receives formatted message strings; the caller decides how to
    deliver them (recall_channel, print, log, etc.).
    """
    read = read_events_detailed(workspace_root, _CONSUMER_NAME)
    # An unreadable outbox line is a message that never reaches a channel, and
    # the bridge is the only place that says so.
    #
    # It is reported on EVERY read, not once: the cursor filter compares seq
    # against the cursor and only lines that PARSE have a usable seq, so an
    # unreadable line can never be advanced past, wherever it sits in the file
    # (witnessed terminal AND mid-file). An UNTERMINATED record never reaches
    # this report at all: the reader splits on b"\n", so a COMPLETE record whose
    # only loss was its terminator is the final chunk and parses on the spot --
    # before any emit, repair or no repair (W11, and its truncated control).
    # What seam repair protects is the NEXT append, which would otherwise be
    # glued onto it. So everything reported here is a TRUNCATED record, and for
    # those the report is permanent: a bridge polling in a loop warns every
    # cycle until someone repairs the file. That cost is real and is recorded
    # as a residual.
    #
    # stderr keeps stdout parseable for callers that consume the posted count.
    warn_unreadable(read)
    posted = 0
    for event in read.events:
        try:
            msg = format_event(event)
        except Exception as exc:  # noqa: BLE001 -- the formatter's failure type is not a contract
            # A malformed event fails to format the same way every time, so leaving it
            # unacknowledged would stop the bridge at it on every run. Skip it on purpose and
            # say so (HOOK-EVENT-CONTRACT.md section 5.3: skip, log, do not crash). The catch
            # is around format_event ONLY and names no exception types: the first version
            # listed (KeyError, TypeError) and a pr.created with a mixed repos list raised
            # AttributeError past it, which wedged the bridge at that event. A post_fn
            # failure is the opposite case and is NOT caught: it may be transient, so it
            # propagates with the event, and everything behind it, still unacknowledged.
            print(
                f"channel bridge: skipping event {event.get('event_id', '?')}"
                f" ({event.get('type', '?')}): cannot format it ({exc!r})",
                file=sys.stderr,
            )
            ack_events(workspace_root, _CONSUMER_NAME, [event])
            continue
        if msg is not None:
            post_fn(msg)
            posted += 1
        # ACKNOWLEDGE AFTER THE EFFECT, one event at a time: a message posted, or an event
        # that maps to no message (a deliberate skip). If post_fn raised above, this line
        # was not reached, so that event and every event behind it are offered again.
        ack_events(workspace_root, _CONSUMER_NAME, [event])
    return posted
