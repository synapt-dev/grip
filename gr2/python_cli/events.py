"""gr2 event system runtime.

Implements the event contract from HOOK-EVENT-CONTRACT.md sections 3-8:
- EventType enum (section 7.2)
- emit() function (sections 4.2, 7.1)
- Outbox management with rotation (sections 4.1-4.4)
- Cursor-based consumer model (section 5.1)
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

_RESERVED_NAMES = frozenset(
    {
        "version",
        "event_id",
        "seq",
        "timestamp",
        "type",
        "workspace",
        "actor",
        "agent_id",
        "owner_unit",
    }
)

_ROTATION_THRESHOLD = 10 * 1024 * 1024


class EventEmitError(RuntimeError):
    """The event could not be durably recorded."""


class EventType(str, Enum):
    LANE_CREATED = "lane.created"
    LANE_ENTERED = "lane.entered"
    LANE_EXITED = "lane.exited"
    LANE_SWITCHED = "lane.switched"
    LANE_ARCHIVED = "lane.archived"

    LEASE_ACQUIRED = "lease.acquired"
    LEASE_RELEASED = "lease.released"
    LEASE_EXPIRED = "lease.expired"
    LEASE_FORCE_BROKEN = "lease.force_broken"

    HOOK_STARTED = "hook.started"
    HOOK_COMPLETED = "hook.completed"
    HOOK_FAILED = "hook.failed"
    HOOK_SKIPPED = "hook.skipped"

    PR_CREATED = "pr.created"
    PR_STATUS_CHANGED = "pr.status_changed"
    PR_CHECKS_PASSED = "pr.checks_passed"
    PR_CHECKS_FAILED = "pr.checks_failed"
    PR_REVIEW_SUBMITTED = "pr.review_submitted"
    PR_MERGED = "pr.merged"
    PR_MERGE_FAILED = "pr.merge_failed"

    SYNC_STARTED = "sync.started"
    SYNC_CACHE_SEEDED = "sync.cache_seeded"
    SYNC_CACHE_REFRESHED = "sync.cache_refreshed"
    SYNC_REPO_UPDATED = "sync.repo_updated"
    SYNC_REPO_FETCHED = "sync.repo_fetched"
    SYNC_REPO_SKIPPED = "sync.repo_skipped"
    SYNC_CONFLICT = "sync.conflict"
    SYNC_COMPLETED = "sync.completed"

    # Execution
    EXEC_STARTED = "exec.started"
    EXEC_COMPLETED = "exec.completed"
    EXEC_FAILED = "exec.failed"

    FAILURE_RESOLVED = "failure.resolved"
    LEASE_RECLAIMED = "lease.reclaimed"

    WORKSPACE_MATERIALIZED = "workspace.materialized"
    WORKSPACE_FILE_PROJECTED = "workspace.file_projected"

    # one event per propagation receipt: the daemon's notification line, carried on the
    # outbox so a consumer can relay it without the daemon knowing any channel
    PROPAGATION_RECEIPT = "propagation.receipt"


def _outbox_path(workspace_root: Path) -> Path:
    return workspace_root / ".grip" / "events" / "outbox.jsonl"


def _cursors_dir(workspace_root: Path) -> Path:
    return workspace_root / ".grip" / "events" / "cursors"


def _lock_path(outbox: Path) -> Path:
    return outbox.parent / "outbox.lock"


def _event_locking_enabled() -> bool:
    """Allow the stress harness to reproduce the pre-lock behavior."""
    return os.environ.get("GR2_DISABLE_EVENT_LOCKING") != "1"


@contextmanager
def _event_write_lock(outbox: Path):
    """Serialize sequence allocation, rotation, and append across processes."""
    with _lock_path(outbox).open("a+") as lock_file:
        locking_enabled = _event_locking_enabled()
        if locking_enabled:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if locking_enabled:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


# DELIBERATELY PLAIN CLASSES, NOT @dataclass, AND THE REASON IS LOAD-BEARING.
#
# This module is loaded out-of-tree by spawned workers via
# importlib.util.spec_from_file_location() + exec_module(), which does NOT
# register the module in sys.modules. @dataclass resolves field types through
# sys.modules[cls.__module__].__dict__, so under that loader it raises
# AttributeError: 'NoneType' object has no attribute '__dict__' AT IMPORT --
# every worker dies before running a line of its own.
#
# Measured: adding @dataclass here killed both writers in the concurrent-emit
# integrity test before either reached sequence allocation. The failure was
# visible an hour earlier in an ad-hoc probe and was dismissed as a loader
# artifact; it was a portability constraint on this file. Anything importable by
# a spawned worker must import without sys.modules registration.
# test_events_torn_line.py carries a witness for exactly this.


class MalformedLine:
    """One line the reader could not turn into an event, and why."""

    __slots__ = ("ordinal", "reason", "excerpt")

    def __init__(self, ordinal: int, reason: str, excerpt: str) -> None:
        self.ordinal = ordinal
        self.reason = reason
        self.excerpt = excerpt

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"MalformedLine(ordinal={self.ordinal!r}, reason={self.reason!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MalformedLine):
            return NotImplemented
        return (self.ordinal, self.reason, self.excerpt) == (
            other.ordinal,
            other.reason,
            other.excerpt,
        )


class EventRead:
    """Events read, AND the lines that could not be read.

    Both halves come from the SAME read. A second pass to "check health" would
    describe a different moment, and the outbox is appended to concurrently.
    """

    __slots__ = ("events", "malformed")

    def __init__(
        self,
        events: list[dict[str, object]],
        malformed: tuple[MalformedLine, ...],
    ) -> None:
        self.events = events
        self.malformed = malformed

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"EventRead(events={len(self.events)}, malformed={len(self.malformed)})"


def _decode_line(raw: bytes) -> tuple[str | None, str]:
    """Decode one line, or say why it could not be decoded.

    The DECODE is the acquisition, not a transformation of an existing string:
    reading the outbox in text mode manufactures the line as text outside every
    guard, so a single invalid byte escapes as UnicodeDecodeError from a place
    no parse guard can reach. Reading bytes and decoding per line puts the one
    operation that can fail on file CONTENT inside the funnel.
    """
    try:
        return raw.decode("utf-8"), ""
    except UnicodeDecodeError as exc:
        return None, str(exc)


def _read_object(line: str) -> tuple[dict[str, object] | None, str]:
    """Parse one line into a JSON object, or say why it is not one.

    This IS an enumerated tuple -- what changed is how the entries were chosen.
    The previous guard, (JSONDecodeError, TypeError), was a list of what had been
    SEEN: it caught syntax errors, and missed RecursionError, which json.loads
    raises on deeply nested input and which is NOT a ValueError, so deep nesting
    escaped a reader whose contract is to not raise on file content.

    This tuple is derived from what the OPERATION can raise: ValueError as the
    base class covering JSONDecodeError and any other value error, plus
    RecursionError, the one thing json.loads raises that ValueError does not
    cover. Structure -- is the result an object? -- is then checked separately
    below, because a valid JSON array parses cleanly and is still not an event.
    """
    try:
        obj = json.loads(line)
    except (ValueError, RecursionError) as exc:
        return None, str(exc)
    if not isinstance(obj, dict):
        return None, f"line is a {type(obj).__name__}, not an object"
    return obj, ""


def _read_seq(obj: dict[str, object]) -> tuple[int | None, str]:
    """The event's sequence number, or why it cannot be used as one.

    bool is an int subclass, so `isinstance(True, int)` is True and `max(0, True)`
    quietly yields 1. A float slips through comparison and arithmetic and then
    fails at serialization: 1e999 is valid JSON input, becomes inf, and
    json.dumps writes `Infinity`, which is not valid JSON output. Validate the
    type here rather than discovering it downstream.
    """
    seq = obj.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int):
        return None, f"seq is {type(seq).__name__}, not an integer"
    return seq, ""


def _iter_outbox(outbox: Path) -> Iterator[tuple[dict[str, object] | None, str, bytes]]:
    """Yield (object, reason, raw) per line, never raising on file CONTENT.

    Splitting bytes on b"\n" rather than iterating text: see _decode_line.
    """
    # DELIBERATELY CATCHES NOTHING. An I/O failure is not a malformed line: it
    # is not knowing what the file contains, and the caller's correct response
    # differs. _current_seq() is on the WRITE path, where swallowing this
    # returns 0 and emit() then allocates a sequence number that duplicates
    # existing ones -- silent event-log corruption, which is why an earlier fix
    # removed exactly this OSError-to-zero fallback and left a test standing
    # guard over it. Reintroducing it here was caught by that test and not by
    # any witness of mine.
    blob = outbox.read_bytes()
    for raw in blob.split(b"\n"):
        if not raw.strip():
            continue
        line, reason = _decode_line(raw)
        if line is None:
            yield None, reason, raw
            continue
        obj, reason = _read_object(line)
        yield obj, reason, raw


def _excerpt(raw: bytes) -> str:
    return raw[:120].decode("utf-8", "replace")


def _current_seq(outbox: Path) -> int:
    """Highest sequence number in the outbox.

    DELIBERATELY REPORTS NO COUNT, and that is a decision rather than an
    omission. This runs once per emit, inside the write lock, so a count here
    would be a per-APPEND number reported to whoever happened to be writing --
    the wrong altitude and the wrong audience. Unreadable lines are surfaced
    once, from read_events_detailed(), where the number is per-READ and reaches
    a consumer that can act on it.
    """
    if not outbox.exists():
        return 0
    last_seq = 0
    for obj, _reason, _raw in _iter_outbox(outbox):
        if obj is None:
            continue
        seq, _seq_reason = _read_seq(obj)
        if seq is None:
            continue
        last_seq = max(last_seq, seq)
    return last_seq


def _maybe_rotate(outbox: Path) -> None:
    if not outbox.exists():
        return
    try:
        size = outbox.stat().st_size
    except OSError:
        return
    if size <= _ROTATION_THRESHOLD:
        return
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    archive = outbox.parent / f"outbox.{ts}.jsonl"
    outbox.rename(archive)


def emit(
    event_type: EventType,
    workspace_root: Path,
    actor: str,
    owner_unit: str,
    payload: dict[str, object],
    *,
    agent_id: str | None = None,
) -> None:
    collisions = _RESERVED_NAMES & payload.keys()
    if collisions:
        raise ValueError(f"payload keys collide with reserved envelope/context names: {collisions}")

    outbox = _outbox_path(workspace_root)
    try:
        outbox.parent.mkdir(parents=True, exist_ok=True)
        with _event_write_lock(outbox):
            seq = _current_seq(outbox) + 1
            delay = float(os.environ.get("GR2_EVENT_TEST_DELAY", "0"))
            if delay:
                time.sleep(delay)
            _maybe_rotate(outbox)

            event: dict[str, object] = {
                "version": 1,
                "event_id": os.urandom(8).hex(),
                "seq": seq,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": str(event_type.value),
                "workspace": workspace_root.name,
                "actor": actor,
                "owner_unit": owner_unit,
            }
            if agent_id is not None:
                event["agent_id"] = agent_id
            event.update(payload)

            # TERMINATOR REPAIR. A previous write that died between write() and
            # fsync() leaves a last line with no "\n". Appending onto that GLUES
            # two records into one line, and the damage runs FORWARD from the
            # tear: the torn record and THE NEXT HEALTHY APPEND fuse into one
            # unparseable line, while the record before the tear is untouched.
            # So a torn write costs that record and the next one written after
            # it, permanently, because every later append builds on the glued
            # line. Probe the last byte and heal the seam before writing.
            # (Direction measured, not reasoned: see the glue witness in
            # test_events_torn_line.py. An earlier version of this comment
            # stated it backwards.)
            with outbox.open("a+b") as event_file:
                event_file.seek(0, os.SEEK_END)
                if event_file.tell() > 0:
                    event_file.seek(-1, os.SEEK_END)
                    if event_file.read(1) != b"\n":
                        event_file.write(b"\n")
                payload_bytes = json.dumps(event, separators=(",", ":")).encode("utf-8")
                event_file.write(payload_bytes + b"\n")
                event_file.flush()
                os.fsync(event_file.fileno())
    except Exception as exc:
        raise EventEmitError(f"event emit failed for {outbox}") from exc


def emit_after_outcome(
    event_type: EventType,
    workspace_root: Path,
    actor: str,
    owner_unit: str,
    payload: dict[str, object],
    *,
    agent_id: str | None = None,
) -> None:
    """Report a completed outcome without replacing it on sink failure.

    Callers must use this only after work that cannot honestly be reported as
    failed. Pre-work and no-work sites use strict ``emit`` directly.
    """

    try:
        emit(
            event_type=event_type,
            workspace_root=workspace_root,
            actor=actor,
            owner_unit=owner_unit,
            payload=payload,
            agent_id=agent_id,
        )
    except EventEmitError as exc:
        print(
            f"gr2: could not record {event_type.value} after its outcome completed ({exc})",
            file=sys.stderr,
        )


def read_events_detailed(workspace_root: Path, consumer: str) -> EventRead:
    """New events for `consumer`, AND the lines that could not be read.

    READING DOES NOT ADVANCE THE CURSOR. Two reads with no ack between them return the same
    events; call ack_events() after the effect to consume them.

    This is the primitive; read_events() is the list-shaped wrapper kept for the
    eleven existing call sites, all of them tests. The count is a RETURN VALUE rather than
    hidden state: these are module-level functions with no instance to hang
    health on, and a module-level accumulator would be wrong under concurrent
    readers, which the outbox explicitly has.

    An unreadable line here is a LOST EVENT -- for the channel bridge it is a
    message that never reaches a channel -- so silence is the failure, not the
    safe default.
    """
    outbox = _outbox_path(workspace_root)
    if not outbox.exists():
        return EventRead([], ())

    cursor = _load_cursor(workspace_root, consumer)
    last_seq = cursor.get("last_seq", 0)
    if isinstance(last_seq, bool) or not isinstance(last_seq, int):
        # A hand-edited or truncated cursor must not brick every future read.
        last_seq = 0

    events: list[dict[str, object]] = []
    malformed: list[MalformedLine] = []
    try:
        lines = list(_iter_outbox(outbox))
    except FileNotFoundError:
        # ONLY this one, and only on the read path: _maybe_rotate() renames the
        # outbox, so a reader can legitimately lose the file between exists()
        # and the read. The events are not gone, they are in an archive. Any
        # OTHER OSError is a real failure and propagates -- a reader that
        # swallows EIO reports "no new events" forever.
        return EventRead([], ())
    for ordinal, (obj, reason, raw) in enumerate(lines, start=1):
        if obj is None:
            malformed.append(MalformedLine(ordinal, reason, _excerpt(raw)))
            continue
        seq, seq_reason = _read_seq(obj)
        if seq is None:
            # An event whose seq is unusable cannot be ordered against the
            # cursor. Comparing it anyway is how "x" <= 0 raises TypeError from
            # inside a reader whose job is to not raise on file content.
            malformed.append(MalformedLine(ordinal, seq_reason, _excerpt(raw)))
            continue
        if seq <= last_seq:
            continue
        events.append(obj)

    # NO CURSOR IS SAVED HERE. A read is a peek: the cursor moves only when the consumer calls
    # ack_events() after its effect (section 5.1 steps 3 then 4). Saving it here, at the last
    # event RETURNED, was the defect: a consumer that failed after reading never saw the rest
    # of the batch again.
    return EventRead(events, tuple(malformed))


def read_events(workspace_root: Path, consumer: str) -> list[dict[str, object]]:
    """New events for `consumer`, DISCARDING the unreadable-line report.

    Like read_events_detailed(), it does not advance the cursor: acknowledge with
    ack_events() once the events have been handled.

    Kept list-shaped because eleven call sites index and len() the result, all
    of them tests -- no production caller remains once the bridge moves to
    read_events_detailed(), so this is a test-compatibility surface.
    It discards information, which is exactly the silence this sweep exists to
    remove -- so if you are writing a NEW consumer, call read_events_detailed()
    and say something about `malformed`. The one production consumer, the
    channel bridge, does.
    """
    return read_events_detailed(workspace_root, consumer).events


def ack_events(workspace_root: Path, consumer: str, events: Sequence[dict[str, object]]) -> None:
    """Acknowledge `events` for `consumer`: move its cursor through the last of them.

    THIS is the only thing that moves a cursor; reading never does. A consumer calls it
    AFTER its effect (a posted message, a written row, a deliberate skip), which is the
    reading flow HOOK-EVENT-CONTRACT.md section 5.1 states (read, process, THEN update the
    cursor) and the crash behaviour section 10.2 relies on (re-read from `last_seq + 1`).
    Delivery is therefore at-least-once: a consumer that dies between its effect and this
    call sees the event again, which is why section 5.3 makes consumers idempotent.

    MONOTONIC: an acknowledgement at or below the cursor is a no-op, so a late or repeated
    ack of an older batch can never move a cursor backwards and re-open delivered events.
    An empty `events` acknowledges nothing and does not create a cursor file.
    """
    seqs = [e["seq"] for e in events if isinstance(e.get("seq"), int) and not isinstance(e.get("seq"), bool)]
    if not seqs:
        return
    through = max(seqs)
    last = _load_cursor(workspace_root, consumer).get("last_seq", 0)
    if isinstance(last, int) and not isinstance(last, bool) and through <= last:
        return
    last_event = next(e for e in events if e.get("seq") == through)
    _save_cursor(
        workspace_root,
        consumer,
        {
            "consumer": consumer,
            "last_seq": through,
            "last_event_id": last_event.get("event_id", ""),
            "last_read": datetime.now(timezone.utc).isoformat(),
        },
    )


def warn_unreadable(read: EventRead, stream=None, *, show_content: bool = False) -> bool:
    """Report unreadable outbox lines on stderr. True when any were reported.

    stderr, never stdout: a --json consumer's stdout must stay parseable, and a
    warning written there turns a health report into a parse failure.

    THE EXCERPT IS NOT PRINTED BY DEFAULT, and that is a deliberate call rather
    than lost fidelity. `ordinal` and `reason` are STRUCTURAL -- a line number
    and a parser complaint -- and carry no payload. The excerpt is CONTENT, and
    printing it moves bytes out of a file the operator already owns into places
    that get copied: CI logs, terminal scrollback, transcripts pasted into a
    chat. Measured: a malformed line containing an API-key-shaped string echoed
    that string verbatim.

    Redacting it instead was rejected. Matching "secret-looking" patterns in
    arbitrary bytes is a denylist over untrusted input, which leaks by
    construction -- the same defect shape this module's parse guard exists to
    avoid. So the excerpt stays on MalformedLine, where a caller that needs it
    can ask, and stays out of the default report. Pass show_content=True to
    include it when you are debugging a specific file and know what is in it.
    """
    if not read.malformed:
        return False
    out = stream if stream is not None else sys.stderr
    count = len(read.malformed)
    plural = "" if count == 1 else "s"
    print(
        f"warning: skipped {count} unreadable line{plural} in the event outbox",
        file=out,
    )
    for bad in read.malformed:
        detail = f": {bad.excerpt}" if show_content else ""
        print(f"  line {bad.ordinal}: {bad.reason}{detail}", file=out)
    return True


def _load_cursor(workspace_root: Path, consumer: str) -> dict[str, object]:
    cursor_file = _cursors_dir(workspace_root) / f"{consumer}.json"
    if not cursor_file.exists():
        return {}
    try:
        return json.loads(cursor_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cursor(workspace_root: Path, consumer: str, data: dict[str, object]) -> None:
    cursors = _cursors_dir(workspace_root)
    cursors.mkdir(parents=True, exist_ok=True)
    cursor_file = cursors / f"{consumer}.json"
    tmp = cursor_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(cursor_file)
