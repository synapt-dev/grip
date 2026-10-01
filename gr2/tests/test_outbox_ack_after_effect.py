"""An outbox consumer acknowledges AFTER its effect, never before.

THE DEFECT, measured on grip dev ebd0371c: `read_events_detailed` saved the consumer cursor
at the last event it RETURNED, before the caller had done anything with the batch. The one
production consumer, `channel_bridge.run_bridge`, then looped `post_fn` over that batch, so a
`post_fn` that raised on event k left the cursor already past events k..n and they were never
offered again.

THE CONTRACT THIS RESTORES is the one docs/HOOK-EVENT-CONTRACT.md already states: section
5.1's reading flow is read, PROCESS each event, THEN update the cursor; section 10.2 says a
consumer that crashes after reading re-reads from `last_seq + 1` on restart; and section 5.3
makes consumers idempotent, with `event_id` for deduplication, precisely because delivery is
at-least-once. The code advanced the cursor at step 2.

THE SHAPE: reading never moves a cursor. `ack_events` moves it, monotonically, and the bridge
acknowledges each event after its effect (a posted message, or a deliberate skip).

THE LOAD-BEARING ROWS are the bridge failure rows. Row 1 alone would pass a change that
simply never saves a cursor; the success row is the control that defeats that, and the
monotonic row is the control that defeats an ack that can move a cursor backwards.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gr2.python_cli.channel_bridge import run_bridge
from gr2.python_cli.events import (
    EventType,
    ack_events,
    emit,
    read_events,
    read_events_detailed,
)

CONSUMER = "channel_bridge"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def _lane(workspace: Path, name: str) -> None:
    emit(
        event_type=EventType.LANE_ENTERED,
        workspace_root=workspace,
        actor="agent:apollo",
        owner_unit="apollo",
        payload={"lane_name": name, "lane_type": "feature", "repos": ["grip"]},
    )


def _unmapped(workspace: Path) -> None:
    """An event type the bridge maps to no message (format_event returns None)."""
    emit(
        event_type=EventType.SYNC_COMPLETED,
        workspace_root=workspace,
        actor="sink",
        owner_unit="unit-0",
        payload={"detail": "unmapped"},
    )


def _cursor(workspace: Path, consumer: str = CONSUMER) -> dict:
    path = workspace / ".grip" / "events" / "cursors" / f"{consumer}.json"
    return json.loads(path.read_text()) if path.exists() else {}


class Poster:
    """A post_fn that records what it was handed and can fail on a chosen message."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.posted: list[str] = []

    def __call__(self, message: str) -> None:
        if self.fail_on is not None and self.fail_on in message:
            raise ConnectionError("channel unreachable")
        self.posted.append(message)


# -- the reader: reading is a peek, acknowledging is the only thing that moves a cursor ----------


def test_reading_alone_never_moves_the_cursor(workspace: Path) -> None:
    for name in ("a", "b", "c"):
        _lane(workspace, name)

    first = read_events(workspace, "peeker")
    again = read_events(workspace, "peeker")

    assert [e["lane_name"] for e in first] == ["a", "b", "c"]
    assert [e["lane_name"] for e in again] == ["a", "b", "c"], "a read must not consume"
    assert read_events_detailed(workspace, "peeker").events == again
    assert _cursor(workspace, "peeker") == {}, "reading must not even create a cursor file"


def test_ack_moves_the_cursor_through_the_last_event_acknowledged(workspace: Path) -> None:
    for name in ("a", "b", "c"):
        _lane(workspace, name)
    events = read_events(workspace, "acker")

    ack_events(workspace, "acker", events[:2])

    assert [e["lane_name"] for e in read_events(workspace, "acker")] == ["c"]
    assert _cursor(workspace, "acker")["last_seq"] == events[1]["seq"]
    assert _cursor(workspace, "acker")["consumer"] == "acker"
    assert _cursor(workspace, "acker")["last_event_id"] == events[1]["event_id"]


def test_ack_never_moves_a_cursor_backwards(workspace: Path) -> None:
    """THE CONTROL for the ack itself: a late, lower acknowledgement is a no-op."""
    for name in ("a", "b", "c"):
        _lane(workspace, name)
    events = read_events(workspace, "mono")

    ack_events(workspace, "mono", events)
    ack_events(workspace, "mono", events[:1])

    assert _cursor(workspace, "mono")["last_seq"] == events[2]["seq"]
    assert read_events(workspace, "mono") == []


def test_acking_nothing_leaves_the_cursor_alone(workspace: Path) -> None:
    _lane(workspace, "a")
    ack_events(workspace, "empty", [])
    assert _cursor(workspace, "empty") == {}
    assert len(read_events(workspace, "empty")) == 1


def test_consumers_acknowledge_independently(workspace: Path) -> None:
    for name in ("a", "b"):
        _lane(workspace, name)
    ack_events(workspace, "one", read_events(workspace, "one"))

    assert read_events(workspace, "one") == []
    assert len(read_events(workspace, "two")) == 2


# -- the bridge: a failing effect loses nothing --------------------------------------------------


def test_a_post_that_fails_midway_loses_no_event(workspace: Path) -> None:
    """THE DEFECT, as a bridge row: post_fn raises on the 2nd of 3 events."""
    for name in ("one", "two", "three"):
        _lane(workspace, name)

    failing = Poster(fail_on="apollo/two")
    with pytest.raises(ConnectionError):
        run_bridge(workspace, post_fn=failing)
    assert len(failing.posted) == 1 and "apollo/one" in failing.posted[0]

    healthy = Poster()
    posted = run_bridge(workspace, post_fn=healthy)

    # The failed event and the one behind it are offered again; the delivered one is not.
    assert posted == 2
    assert [("apollo/two" in m, "apollo/three" in m) for m in healthy.posted] == [(True, False), (False, True)]


def test_a_post_that_fails_on_the_first_event_loses_every_event(workspace: Path) -> None:
    for name in ("one", "two"):
        _lane(workspace, name)

    with pytest.raises(ConnectionError):
        run_bridge(workspace, post_fn=Poster(fail_on="apollo/one"))
    assert _cursor(workspace) == {}, "nothing was delivered, so nothing is acknowledged"

    healthy = Poster()
    assert run_bridge(workspace, post_fn=healthy) == 2


def test_a_successful_run_acknowledges_everything_it_posted(workspace: Path) -> None:
    """THE CONTROL: without it, "never save a cursor" would pass every failure row."""
    for name in ("one", "two"):
        _lane(workspace, name)

    first = Poster()
    assert run_bridge(workspace, post_fn=first) == 2
    second = Poster()
    assert run_bridge(workspace, post_fn=second) == 0
    assert second.posted == []


def test_an_event_that_maps_to_no_message_is_acknowledged_not_replayed(workspace: Path) -> None:
    """A deliberate skip is an effect that happened. Left unacknowledged it would be
    re-offered on every run, forever."""
    _unmapped(workspace)
    _lane(workspace, "after")

    assert run_bridge(workspace, post_fn=Poster()) == 1
    assert read_events(workspace, CONSUMER) == []


def test_an_unmapped_event_before_a_failing_post_is_acknowledged_and_the_failure_is_not(
    workspace: Path,
) -> None:
    _unmapped(workspace)
    _lane(workspace, "boom")

    with pytest.raises(ConnectionError):
        run_bridge(workspace, post_fn=Poster(fail_on="apollo/boom"))

    remaining = read_events(workspace, CONSUMER)
    assert [e["type"] for e in remaining] == ["lane.entered"]


def _corrupt_middle_event(workspace: Path, corrupt) -> str:
    """Emit three lane events, then rewrite the middle row in place with `corrupt(row)`,
    keeping its seq and event_id. Returns the middle event's id."""
    for name in ("good-before", "bad", "good-after"):
        _lane(workspace, name)
    outbox = workspace / ".grip" / "events" / "outbox.jsonl"
    rows = [json.loads(line) for line in outbox.read_text().splitlines()]
    corrupt(rows[1])
    outbox.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return rows[1]["event_id"]


def _drop_fields(row: dict) -> None:
    for field in ("actor", "owner_unit", "lane_name"):
        del row[field]  # the formatter's KeyError


def _mixed_repos(row: dict) -> None:
    """A pr.created whose repos list mixes a dict and a string: format_event calls .get on
    the string, an AttributeError, which is neither a KeyError nor a TypeError."""
    row.update({"type": "pr.created", "pr_group_id": "grp-1", "repos": [{"repo": "grip"}, "premium"]})


@pytest.mark.parametrize(
    "corrupt",
    [_drop_fields, _mixed_repos],
    ids=["missing-field-KeyError", "mixed-repos-AttributeError"],
)
def test_a_malformed_event_is_skipped_and_acknowledged_not_a_wedge(
    workspace: Path, capsys: pytest.CaptureFixture[str], corrupt
) -> None:
    """Acknowledging after the effect makes a DETERMINISTIC failure permanent: a format_event
    that raises on one event would otherwise stop the bridge at that event on every run.
    Before this change the same event was lost by accident; the contract (section 5.3: skip,
    log, do not crash) says skip it on purpose.

    TWO exception types on purpose. The first version of this catch named (KeyError,
    TypeError) and a reviewer found a pr.created with a mixed repos list raising
    AttributeError, which wedged the bridge there forever. The formatter's failure type is
    not a contract, so the catch is around format_event and is not a list of types; a post
    failure is outside it, and test_a_post_that_fails_midway_loses_no_event holds that line."""
    bad_id = _corrupt_middle_event(workspace, corrupt)

    poster = Poster()
    run_bridge(workspace, post_fn=poster)

    assert any("good-before" in m for m in poster.posted)
    assert any("good-after" in m for m in poster.posted), "the bridge must get PAST the bad event"
    assert bad_id in capsys.readouterr().err, "the skip must be reported, not silent"
    assert run_bridge(workspace, post_fn=Poster()) == 0, "and acknowledged, so it is not replayed"
