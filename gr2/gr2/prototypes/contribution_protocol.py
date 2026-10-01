"""Prototype 2: the contribution protocol around the propagation state machine.

The machine (``propagation_state_machine``) lands ONE contribution on a canonical
remote by compare-and-swap. This module is the protocol that decides WHICH owner a
change is proposed to, lands SEVERAL contributions that must go together, refuses
to RETIRE a workspace that still holds open contributions, and gives declared
append-only surfaces the one behaviour they are allowed that everything else is
not: two writers, both land, neither replans.

Everything here is neutral: layer refs, destination ids, and surface names are
opaque strings the caller resolves. The module holds no notion of who an agent or
an org is, and who MAY contribute where is a policy it is handed, never derives.

Four pieces, each the smallest shape that its witness needs:

``ResolvedManifest`` / ``ResolvedEntry``
    Ownership is a RECORDED fact, never a guess over path shape. Every resolved
    entry carries ``declared_by`` (the layer whose declaration put it in the
    workspace), ``overridden_by`` (the layer whose override currently governs it,
    if any) and ``write_mode`` (``own`` / ``contribute`` / ``append`` / ``read``).
    ``owner()`` is ``overridden_by or declared_by``; ``classify()`` answers, for a
    path, whether a change is local authoring, a contribution (and to whom), an
    append, or refused. Two layers declaring the same entry without an override is
    a NAMED collision at resolution, not a silent precedence.

    Today's resolver FLATTENS this information away (it merges repos by name and
    records no provenance), so this dataclass is the specification of the field
    the resolver must grow, and the witnesses run against the stub.

``ContributionSet``
    Several contributions that must land together land in DECLARED ORDER with one
    receipt per member and one set receipt naming what landed — all-or-REPORT,
    not all-or-nothing. ``prepare()`` drives every member through its gates and
    stops before any verb (``stop_after=PLANNED``); if any member refuses there,
    nothing has landed and the set reports it. ``land()`` then resumes each member
    in order and STOPS at the first member that does not reach acknowledged,
    leaving earlier members landed (git history is forward-only; a rollback would
    be a new forward operation, never an un-push) and naming the landed, the
    refusing, and the not-attempted members in the set receipt.

``Subspace``
    A workspace that holds contributions. ``retire()`` REFUSES while any of its
    contributions is not terminal — not acknowledged and not explicitly
    abandoned — and lists their operation ids. ``abandon()`` writes an
    ``abandoned`` note to the contribution's own journal, so a workspace can never
    be retired with a change that simply evaporates.

``AppendSurface``
    A declared append-only file: one guarded append point (an exclusive lock held
    across write + flush + fsync), each record numbered in arrival order. Two
    writers both land; no expected base exists because the operation commutes.
    This is commutative BY DECLARATION, never inferred from the shape of a change,
    and it is a file-level surface: a git-tracked path is never an append surface
    to this protocol, because landing two appends there would require merging on
    an author's behalf.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from gr2.prototypes.propagation_state_machine import (
    Coordinate,
    Destination,
    MalformedLine,
    Propagator,
    Receipt,
    State,
)

ABANDONED_NOTE = "abandoned"


class WriteMode(StrEnum):
    OWN = "own"
    CONTRIBUTE = "contribute"
    APPEND = "append"
    READ = "read"


class ResolutionCollision(ValueError):
    """Two layers declared the same entry and neither said ``override``.

    Named and refused at resolution; never resolved by precedence silently.
    """


@dataclass(frozen=True)
class ResolvedEntry:
    """One surface of a materialized workspace, with its provenance recorded."""

    name: str
    path: str
    declared_by: str
    overridden_by: str | None
    write_mode: WriteMode

    @property
    def owner(self) -> str:
        return self.overridden_by or self.declared_by


@dataclass(frozen=True)
class Classification:
    """What a change to ``path`` is, under this workspace's recorded ownership."""

    entry: ResolvedEntry | None
    mode: WriteMode
    owner: str | None
    reason: str


@dataclass(frozen=True)
class Declaration:
    """One layer's declaration of one entry, as the resolver sees it before merging."""

    layer: str
    name: str
    path: str
    overrides: bool = False


class ResolvedManifest:
    """Ownership as a lookup. Built by ``resolve``; read by ``classify``."""

    def __init__(self, entries: Iterable[ResolvedEntry]) -> None:
        self._entries = {e.name: e for e in entries}

    @classmethod
    def resolve(
        cls, this_layer: str, declarations: Sequence[Declaration], *, appends: Iterable[str] = ()
    ) -> ResolvedManifest:
        """Merge layer declarations (ancestors first) with provenance kept.

        A later declaration of a name already declared is a ``ResolutionCollision``
        unless it says ``overrides``; an override records ``overridden_by`` and keeps
        ``declared_by``. Names in ``appends`` are declared append-only by their owner.
        """
        merged: dict[str, ResolvedEntry] = {}
        append_names = set(appends)
        for d in declarations:
            prior = merged.get(d.name)
            if prior is None:
                merged[d.name] = ResolvedEntry(
                    name=d.name,
                    path=d.path,
                    declared_by=d.layer,
                    overridden_by=None,
                    write_mode=WriteMode.READ,
                )
                continue
            if not d.overrides:
                raise ResolutionCollision(
                    f"{d.name!r} declared by {prior.declared_by!r} at {prior.path!r} and again by "
                    f"{d.layer!r} at {d.path!r} without override"
                )
            merged[d.name] = ResolvedEntry(
                name=d.name,
                path=d.path,
                declared_by=prior.declared_by,
                overridden_by=d.layer,
                write_mode=WriteMode.READ,
            )
        entries = []
        for e in merged.values():
            if e.name in append_names:
                mode = WriteMode.APPEND
            elif e.owner == this_layer:
                mode = WriteMode.OWN
            else:
                mode = WriteMode.CONTRIBUTE
            entries.append(
                ResolvedEntry(
                    name=e.name,
                    path=e.path,
                    declared_by=e.declared_by,
                    overridden_by=e.overridden_by,
                    write_mode=mode,
                )
            )
        return cls(entries)

    def entry(self, name: str) -> ResolvedEntry:
        return self._entries[name]

    def owner(self, name: str) -> str:
        return self._entries[name].owner

    def classify(self, path: str) -> Classification:
        """The longest declared path prefix wins; a path under no entry is refused (READ)."""
        best: ResolvedEntry | None = None
        for e in self._entries.values():
            root = e.path.rstrip("/") + "/"
            if (path == e.path or path.startswith(root)) and (
                best is None or len(e.path) > len(best.path)
            ):
                best = e
        if best is None:
            return Classification(
                entry=None,
                mode=WriteMode.READ,
                owner=None,
                reason=f"{path!r} is under no declared entry",
            )
        return Classification(
            entry=best,
            mode=best.write_mode,
            owner=best.owner,
            reason=f"{path!r} is under {best.name!r} declared_by={best.declared_by!r} "
            f"overridden_by={best.overridden_by!r}",
        )


# --------------------------------------------------------------------------- sets


@dataclass(frozen=True)
class SetMember:
    member_id: str
    coordinate: Coordinate
    destination: Destination
    propagator: Propagator


@dataclass(frozen=True)
class MemberOutcome:
    member_id: str
    state: str  # a State value, or "not-attempted"
    receipt: Receipt | None

    def as_dict(self) -> dict[str, object]:
        return {
            "member_id": self.member_id,
            "state": self.state,
            "receipt": self.receipt.as_dict() if self.receipt is not None else None,
        }


@dataclass(frozen=True)
class SetReceipt:
    set_id: str
    phase: str  # "prepared" | "refused-at-prepare" | "landed" | "stopped"
    order: tuple[str, ...]
    outcomes: tuple[MemberOutcome, ...]
    timestamp: str

    @property
    def landed(self) -> tuple[str, ...]:
        return tuple(o.member_id for o in self.outcomes if o.state == str(State.ACKNOWLEDGED))

    @property
    def refused(self) -> tuple[str, ...]:
        return tuple(
            o.member_id
            for o in self.outcomes
            if o.state in (str(State.REFUSED), str(State.UNVERIFIABLE))
        )

    @property
    def not_attempted(self) -> tuple[str, ...]:
        return tuple(o.member_id for o in self.outcomes if o.state == "not-attempted")

    def as_dict(self) -> dict[str, object]:
        return {
            "set_id": self.set_id,
            "phase": self.phase,
            "order": list(self.order),
            "landed": list(self.landed),
            "refused": list(self.refused),
            "not_attempted": list(self.not_attempted),
            "outcomes": [o.as_dict() for o in self.outcomes],
            "timestamp": self.timestamp,
        }


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def state_latencies(receipt: Receipt) -> list[tuple[str, float]]:
    """Per-state latency from the receipt's OWN timestamps, in seconds.

    Each row is ``(state, seconds since the previous transition)``; the first row is
    ``(state, 0.0)``. A measurement, not a witness: the receipts already carry the
    timestamps, so the number comes from the artifact and not from a stopwatch around
    it. Printed so a daemon's ``up`` budget can start from data.
    """
    rows: list[tuple[str, float]] = []
    previous: datetime | None = None
    for t in receipt.transitions:
        at = datetime.fromisoformat(t.timestamp)
        rows.append((str(t.state), 0.0 if previous is None else (at - previous).total_seconds()))
        previous = at
    return rows


class ContributionSet:
    """Several contributions that must land together: prepare all, land in order, report."""

    def __init__(self, set_id: str, members: Sequence[SetMember]) -> None:
        if not members:
            raise ValueError("a contribution set needs at least one member")
        ids = [m.member_id for m in members]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate member ids in set {set_id!r}: {ids}")
        self.set_id = set_id
        self.members = tuple(members)

    @property
    def order(self) -> tuple[str, ...]:
        return tuple(m.member_id for m in self.members)

    def prepare(self) -> SetReceipt:
        """Drive every member to ``planned`` without running a verb.

        If any member refuses at plan, the set is refused BEFORE anything lands and the
        receipt carries every member's evidence. A member whose source has nothing new
        (``run`` returned ``None``) is reported as not-attempted: it is not part of this
        set's landing.
        """
        outcomes: list[MemberOutcome] = []
        for m in self.members:
            receipt = m.propagator.run(m.coordinate, m.destination, stop_after=State.PLANNED)
            if receipt is None:
                outcomes.append(MemberOutcome(m.member_id, "not-attempted", None))
            else:
                outcomes.append(MemberOutcome(m.member_id, str(receipt.state), receipt))
        any_refused = any(o.state == str(State.REFUSED) for o in outcomes)
        return SetReceipt(
            set_id=self.set_id,
            phase="refused-at-prepare" if any_refused else "prepared",
            order=self.order,
            outcomes=tuple(outcomes),
            timestamp=_now(),
        )

    def land(self, *, on_member_landed: Callable[[str], None] | None = None) -> SetReceipt:
        """Resume each prepared member in order; STOP at the first that does not acknowledge.

        Earlier members stay landed. The receipt names landed / refused / not-attempted
        members so the author can resolve forward. Nothing is rolled back.
        """
        outcomes: list[MemberOutcome] = []
        stopped = False
        for m in self.members:
            if stopped:
                outcomes.append(MemberOutcome(m.member_id, "not-attempted", None))
                continue
            receipt = m.propagator.run(m.coordinate, m.destination)
            if receipt is None:
                outcomes.append(MemberOutcome(m.member_id, "not-attempted", None))
                continue
            outcomes.append(MemberOutcome(m.member_id, str(receipt.state), receipt))
            if receipt.state is State.ACKNOWLEDGED:
                if on_member_landed is not None:
                    on_member_landed(m.member_id)
            else:
                stopped = True
        return SetReceipt(
            set_id=self.set_id,
            phase="stopped" if stopped else "landed",
            order=self.order,
            outcomes=tuple(outcomes),
            timestamp=_now(),
        )


# --------------------------------------------------------------------------- subspaces


@dataclass(frozen=True)
class OpenContribution:
    coordinate_key: str
    source_rev: str
    last_state: str
    operation_id: str | None


class RetireRefused(RuntimeError):
    """The subspace holds contributions that are neither acknowledged nor abandoned."""

    def __init__(self, subspace: str, open_contributions: Sequence[OpenContribution]) -> None:
        self.subspace = subspace
        self.open_contributions = tuple(open_contributions)
        listed = ", ".join(
            f"{o.coordinate_key} @ {o.source_rev[:12]} ({o.last_state})" for o in open_contributions
        )
        super().__init__(f"{subspace}: {len(open_contributions)} open contribution(s): {listed}")


@dataclass
class Subspace:
    """A workspace and the contributions authored in it, each carried by its own sink."""

    name: str
    contributions: list[tuple[Coordinate, Propagator]] = field(default_factory=list)
    retired: bool = False

    def open_contributions(self) -> list[OpenContribution]:
        """Every (coordinate, source revision) attempt whose last state is not terminal.

        Terminal means ACKNOWLEDGED, or REFUSED-and-then-ABANDONED by an explicit note.
        A refused attempt with no abandon note is OPEN: the refusal described a moment and
        the author has not said what becomes of the change.
        """
        found: list[OpenContribution] = []
        for coordinate, propagator in self.contributions:
            key = coordinate.key()
            revs: dict[str, list[dict[str, object]]] = {}
            for row in propagator.journal._rows():
                if row.get("coordinate_key") != key:
                    continue
                revs.setdefault(str(row["source_rev"]), []).append(row)
            for source_rev, rows in revs.items():
                states = [str(r["state"]) for r in rows if "state" in r]
                last_state = states[-1] if states else "(no transition)"
                if last_state == str(State.ACKNOWLEDGED):
                    continue
                if any(r.get("note") == ABANDONED_NOTE for r in rows):
                    continue
                op_ids = [str(r["operation_id"]) for r in rows if r.get("operation_id")]
                found.append(
                    OpenContribution(
                        coordinate_key=key,
                        source_rev=source_rev,
                        last_state=last_state,
                        operation_id=op_ids[-1] if op_ids else None,
                    )
                )
        return found

    def abandon(self, coordinate: Coordinate, source_rev: str, *, reason: str) -> None:
        """Explicitly give up a contribution: an ``abandoned`` note in its own journal."""
        for c, propagator in self.contributions:
            if c.key() != coordinate.key():
                continue
            attempts = propagator.journal.find(coordinate.key(), source_rev)
            attempt = len(attempts) if attempts else 1
            pending_id = f"{coordinate.key()}@{source_rev}"
            if attempts and attempts[-1][-1].operation_id:
                pending_id = str(attempts[-1][-1].operation_id)
            propagator.journal.note(
                pending_id=pending_id,
                attempt=attempt,
                coordinate_key=coordinate.key(),
                source_rev=source_rev,
                note=ABANDONED_NOTE,
                data={"reason": reason},
            )
            return
        raise KeyError(f"{self.name}: no contribution at {coordinate.key()}")

    def retire(self) -> None:
        """Refuse while any contribution is open; otherwise mark retired."""
        open_ = self.open_contributions()
        if open_:
            raise RetireRefused(self.name, open_)
        self.retired = True


# --------------------------------------------------------------------------- append surfaces


@dataclass(frozen=True)
class AppendRecord:
    seq: int
    writer: str
    payload: dict[str, object]
    timestamp: str


def _decode_line(raw: bytes) -> tuple[str | None, str]:
    """Bytes become text HERE, so the failure that can happen HERE is guarded here.

    A JSONL file is a BYTE format. Read in text mode, the decode happens while the
    iterator produces the line — outside every ``try`` in this module — so one
    invalid byte anywhere raises before a guard can see it and the whole scan dies.
    The exception type was never the problem: ``UnicodeDecodeError`` subclasses
    ``ValueError``, which was already caught. The OPERATION that raises it simply sat
    outside the guarded region. A line does not ARRIVE as text, it is MANUFACTURED as
    text, and manufacturing it can fail on content; decoding per line confines a bad
    byte to that line.
    """

    try:
        return raw.decode("utf-8"), ""
    except UnicodeDecodeError as exc:
        return None, str(exc)


def _excerpt(raw: bytes) -> str:
    """A malformed line must be reportable even when it is not valid text."""

    return raw[:120].decode("utf-8", "replace")


def _read_json_object(line: str) -> tuple[dict[str, object] | None, str]:
    """Parse ONE line into a JSON object, or say why it cannot be read.

    ``json.loads`` is the ONLY operation in this file that can raise on file
    content. It raises ``ValueError`` (``JSONDecodeError`` subclasses it, as does
    the integer-literal length limit) or ``RecursionError`` (deeply nested input).
    Everything after it VALIDATES rather than coerces, and that is the whole point:
    a coercion over untrusted bytes forces the caller to enumerate the exceptions it
    might raise, which is a denylist, and a denylist leaks. ``int()`` on a value
    ``json`` can legitimately produce (``1e999`` parses to ``inf``) raises
    ``OverflowError`` — a type no list written from the parse side would predict.
    Validating instead removes the raise rather than cataloguing it.
    """

    try:
        obj = json.loads(line)
    except (ValueError, RecursionError) as exc:
        return None, str(exc)
    if not isinstance(obj, dict):
        return None, f"line is a {type(obj).__name__}, not an object"
    return obj, ""


def _read_seq(obj: dict[str, object]) -> tuple[int | None, str]:
    """A sequence number must be a real integer. ``bool`` is an ``int`` and is not one."""

    seq = obj.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int):
        return None, f"seq is {type(seq).__name__}, not an integer"
    return seq, ""


def _read_record(obj: dict[str, object]) -> tuple[AppendRecord | None, str]:
    """Every field checked by TYPE, so nothing here can raise on hostile content."""

    seq, reason = _read_seq(obj)
    if seq is None:
        return None, reason
    writer = obj.get("writer")
    payload = obj.get("payload")
    timestamp = obj.get("timestamp")
    if not isinstance(writer, str):
        return None, f"writer is {type(writer).__name__}, not a string"
    if not isinstance(payload, dict):
        return None, f"payload is {type(payload).__name__}, not an object"
    if not isinstance(timestamp, str):
        return None, f"timestamp is {type(timestamp).__name__}, not a string"
    return AppendRecord(seq=seq, writer=writer, payload=payload, timestamp=timestamp), ""


class AppendSurface:
    """A declared append-only file with one guarded append point.

    ``append`` takes an exclusive lock on the file, reads the last sequence number,
    writes ``seq + 1`` with the record, flushes, fsyncs, and releases. Two writers
    interleave in ARRIVAL order and both land; there is no expected base to refuse on
    because appends commute. Nothing here ever rewrites an earlier record.

    A writer killed between ``write`` and its terminator leaves a remnant line. Both
    scans SKIP such a line and COUNT it (``malformed_lines``, reflecting the most
    recent full scan): an uncounted drop is silent, and this surface is the one place
    a caller looks. ``append`` also repairs a missing terminator before writing, so a
    remnant never GLUES the next record onto itself — without that repair the record
    that follows a torn write is swallowed into an unparseable line and lost.

    The file is read as BYTES and decoded one line at a time. A JSONL file is a byte
    format, and in text mode the decode happens while the iterator produces the line,
    outside every guard here — so a single invalid byte anywhere destroyed the whole
    scan, including every record written correctly around it. A line passes through
    four layers: read, decode, parse, and shape-check. The last three are each guarded
    where they happen. Unbounded line length (a file with no terminator for a very
    long span) is a resource limit on the first layer and is NOT defended here.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._malformed: tuple[MalformedLine, ...] = ()

    @property
    def malformed_lines(self) -> tuple[MalformedLine, ...]:
        """Lines the most recent full scan could not read. Empty is the healthy case."""
        return self._malformed

    def append(self, writer: str, payload: dict[str, object]) -> AppendRecord:
        with open(self.path, "a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                last = 0
                malformed: list[MalformedLine] = []
                raw_last = b""
                for index, raw in enumerate(handle):
                    raw_last = raw
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    line, reason = _decode_line(stripped)
                    if line is not None:
                        obj, reason = _read_json_object(line)
                        if obj is not None:
                            seq, reason = _read_seq(obj)
                            if seq is not None:
                                last = max(last, seq)
                                continue
                    malformed.append(
                        MalformedLine(index=index, excerpt=_excerpt(stripped), reason=reason)
                    )
                self._malformed = tuple(malformed)
                record = AppendRecord(
                    seq=last + 1, writer=writer, payload=payload, timestamp=_now()
                )
                handle.seek(0, os.SEEK_END)
                if raw_last and not raw_last.endswith(b"\n"):
                    handle.write(b"\n")
                handle.write((json.dumps(asdict(record), sort_keys=True) + "\n").encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
                return record
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def records(self) -> list[AppendRecord]:
        out: list[AppendRecord] = []
        malformed: list[MalformedLine] = []
        for index, raw in enumerate(self.path.read_bytes().split(b"\n")):
            stripped = raw.strip()
            if not stripped:
                continue
            line, reason = _decode_line(stripped)
            if line is not None:
                obj, reason = _read_json_object(line)
                if obj is not None:
                    record, reason = _read_record(obj)
                    if record is not None:
                        out.append(record)
                        continue
            malformed.append(MalformedLine(index=index, excerpt=_excerpt(stripped), reason=reason))
        self._malformed = tuple(malformed)
        return out


__all__ = [
    "ABANDONED_NOTE",
    "AppendRecord",
    "AppendSurface",
    "Classification",
    "ContributionSet",
    "Declaration",
    "MemberOutcome",
    "OpenContribution",
    "ResolutionCollision",
    "ResolvedEntry",
    "ResolvedManifest",
    "RetireRefused",
    "SetMember",
    "SetReceipt",
    "Subspace",
    "WriteMode",
    "state_latencies",
]
