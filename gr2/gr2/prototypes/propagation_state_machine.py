"""Prototype 0: a propagation state machine over git repositories.

This prototype proves the state contract for moving one change from a source
repository to one or more destination clones, before any daemon is allowed near
a real authoring clone. It is deliberately neutral: every identifier it carries
(source, destination, layer, artifact class, policy hash) is an opaque string
the caller resolves; the machine interprets none of them and holds no notion of
who an agent or a workspace is.

The one rule the contract enforces:

    Every state transition names the observation that establishes it, and that
    observation must be capable of returning a different answer.

States, in order, each with the observation that establishes it:

    observed      a source change was detected at a named revision
    fetched       the object is present in the sink's mirror and its digest was
                  recomputed from the object itself
    planned       a plan exists, bound to the destination base it was computed
                  against, with every gate recorded individually
    applied       the destination itself reports the intended revision (read
                  back from the destination, never from the verb's exit status)
    verified      the declared postcondition was checked by name and holds
    acknowledged  the source cursor advances, only now

and three more reachable from any of them:

    refused       a named refusal condition fired, and which one is recorded
    partial       some declared targets verified and others did not, both listed
    unverifiable  the effect may or may not have happened and no observation
                  available to the sink can tell; never collapsed into either
                  neighbour, because one invites a double apply and the other
                  invites work on an effect that may not exist

Idempotency and compare-and-swap:

* every operation has an id derived from (coordinate, source revision, expected
  base, digest); the journal is keyed so a replay resumes the same operation
* every destination transition is compare-and-swap on the expected base; a
  moved base refuses and reports what was observed, it never merges or forces
* the apply step tolerates exactly one benign discrepancy: a destination that
  already reports the intended revision is recorded as applied without running
  the verb again, which is how a sink killed mid-apply replays without applying
  twice

Replay rules the journal implements:

* an acknowledged operation replays as a no-op returning the original outcome
* a refused operation is terminal for that attempt only: a later run at the same
  source revision starts a new attempt, because a refusal describes a moment
  (a dirty worktree, a moved base) and the author may have changed it
* anything else resumes from the last recorded state without skipping a state

Everything under ``state_dir`` belongs to the sink: ``journal.jsonl`` (append
only, one row per transition or note, fsynced), ``cursors/`` (one small file per
coordinate, written only on acknowledged), and ``mirror.git`` (a bare mirror the
sink fetches into, so destinations are read but never written before apply).

Fault injection for tests: ``kill_after`` raises :class:`SinkKilled` right after
the named state is journaled (or right after the apply verb ran, before the read
back, with ``KILL_AFTER_APPLY_VERB``); ``after_apply_verb`` runs a callable
between the verb and the read back so a destination can be made unreadable;
``before_apply_verb`` runs a callable after the apply step's own head check and
before the verb, so a destination can be moved inside that window.

Prototype 2 (the contribution protocol) adds ONE destination kind and no new
state: :attr:`DestinationKind.CANONICAL` is a bare remote that OWNS the branch, and
an operation with ``direction=up`` lands on it by a fast-forward push guarded by
``--force-with-lease`` on the expected base. The receiving repository enforces the
compare-and-swap; the plan's fast-forward gate guarantees the lease never forces;
a rejected lease is a REFUSAL carrying the revision the owner holds, not an error.
Replanning a refused contribution is the author's act — the machine never rebases,
merges, or forces on anyone's behalf.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

KILL_AFTER_APPLY_VERB = "apply-verb"

_GATE_IDS = (
    "policy.direction",
    "destination.base-unmoved",
    "destination.clean",
    "destination.fast-forward",
)
_POSTCONDITION = "head-is-intended-after-and-tree-matches-digest"
# A canonical destination is bare: the postcondition is about its BRANCH ref, and
# "clean" is not part of it because there is no worktree to be clean.
_POSTCONDITION_CANONICAL = "branch-is-intended-after-and-tree-matches-digest"
_GATE_NOT_RUN = "not-run"

# The prototype runs git without the user's global or system configuration so
# that signing, hook paths, and identity settings on the host cannot reach the
# synthetic repositories. Callers may pass their own environment.
_ISOLATED_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


class SinkKilled(RuntimeError):
    """The sink process died at a fault-injection point."""


class DestinationUnreadable(RuntimeError):
    """A read against the destination returned an error instead of an answer."""


@dataclass(frozen=True)
class MalformedLine:
    """A journal line that could not be parsed, kept so a drop is never silent.

    ``index`` counts lines in the file, blank ones included, so it names the line a
    reader would count to by hand.
    """

    index: int
    excerpt: str
    reason: str


class JournalInconsistent(RuntimeError):
    """The cursor names a revision the journal cannot account for.

    A corrupted sink state: the prototype says so and stops rather than guessing an
    outcome. Raised deliberately and left to propagate by every caller, by name.
    """


class SourceUnobservable(RuntimeError):
    """``git ls-remote`` against the source failed or advertised no such branch.

    Raised before any cursor, journal, or destination state is touched, so a caller that
    loops (a daemon) can count it and try again on its next tick with nothing to repair.
    A ``RuntimeError`` subclass, so a caller that caught the bare class still does.
    """


class Direction(StrEnum):
    DOWN = "down"
    UP = "up"
    ACROSS = "across"


class Operation(StrEnum):
    APPLY = "apply"
    CONTRIBUTE = "contribute"
    OBSERVE = "observe"


class DestinationKind(StrEnum):
    REPLICA = "replica"
    AUTHORING = "authoring"
    # Prototype 2 (the contribution protocol): a bare remote that OWNS the branch and
    # accepts a contribution by a fast-forward push guarded by ``--force-with-lease`` on the
    # expected base — compare-and-swap enforced by the receiving repository, not by
    # this process. It has no worktree, so cleanliness is not a property it has.
    CANONICAL = "canonical"


class State(StrEnum):
    OBSERVED = "observed"
    FETCHED = "fetched"
    PLANNED = "planned"
    APPLIED = "applied"
    VERIFIED = "verified"
    ACKNOWLEDGED = "acknowledged"
    REFUSED = "refused"
    PARTIAL = "partial"
    UNVERIFIABLE = "unverifiable"


_ORDERED = (
    State.OBSERVED,
    State.FETCHED,
    State.PLANNED,
    State.APPLIED,
    State.VERIFIED,
    State.ACKNOWLEDGED,
)


@dataclass(frozen=True)
class Coordinate:
    source: str
    destination: str
    layer: str
    direction: Direction
    operation: Operation
    artifact_class: str

    def key(self) -> str:
        """Canonical, injective key: the JSON of ``as_dict()`` with sorted keys.

        The fields are OPAQUE identifiers, so no delimiter can be assumed absent
        from them. A ``"|".join`` made ``("source|dest", "target", ...)`` and
        ``("source", "dest|target", ...)`` the same key, and the key names the
        journal rows and the cursor file, so two coordinates would have shared
        replay state (found in review of the first cut). JSON string encoding
        escapes every byte that could be mistaken for structure, so the key
        round-trips: ``json.loads(key) == as_dict()``. Distinct coordinates
        therefore cannot collide, and the witness asserts the round-trip rather
        than any particular pair.
        """
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_key(cls, key: str) -> Coordinate:
        data = json.loads(key)
        return cls(
            source=str(data["source"]),
            destination=str(data["destination"]),
            layer=str(data["layer"]),
            direction=Direction(str(data["direction"])),
            operation=Operation(str(data["operation"])),
            artifact_class=str(data["artifact_class"]),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "destination": self.destination,
            "layer": self.layer,
            "direction": str(self.direction),
            "operation": str(self.operation),
            "artifact_class": self.artifact_class,
        }


@dataclass(frozen=True)
class Destination:
    destination_id: str
    path: Path
    kind: DestinationKind


@dataclass(frozen=True)
class Policy:
    """Opaque policy inputs. The caller resolves them; the machine only applies them.

    ``allowed_directions`` has no default on purpose: a permissive default would
    be the decision, whoever wrote it.
    """

    policy_hash: str
    allowed_directions: frozenset[Direction]


@dataclass(frozen=True)
class GateResult:
    gate_id: str
    result: str
    detail: str
    result_hash: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class Transition:
    state: State
    observation: dict[str, object]
    timestamp: str
    operation_id: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "state": str(self.state),
            "observation": self.observation,
            "timestamp": self.timestamp,
            "operation_id": self.operation_id,
        }


@dataclass(frozen=True)
class Observation:
    source_rev: str
    cursor: str | None
    is_new: bool


@dataclass(frozen=True)
class Receipt:
    pending_id: str
    attempt: int
    operation_id: str | None
    coordinate: Coordinate
    source_rev: str
    expected_base: str
    observed_base: str | None
    after: str | None
    digest: str | None
    plan_hash: str | None
    policy_hash: str
    gate_results: tuple[GateResult, ...]
    state: State
    postcondition_checked: str | None
    postcondition_holds: bool | None
    timestamp: str
    refusal_reason: str | None
    detail: str
    transitions: tuple[Transition, ...]
    replayed: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "pending_id": self.pending_id,
            "attempt": self.attempt,
            "operation_id": self.operation_id,
            "coordinate": self.coordinate.as_dict(),
            "source_rev": self.source_rev,
            "expected_base": self.expected_base,
            "observed_base": self.observed_base,
            "after": self.after,
            "digest": self.digest,
            "plan_hash": self.plan_hash,
            "policy_hash": self.policy_hash,
            "gate_results": [g.as_dict() for g in self.gate_results],
            "state": str(self.state),
            "postcondition_checked": self.postcondition_checked,
            "postcondition_holds": self.postcondition_holds,
            "timestamp": self.timestamp,
            "refusal_reason": self.refusal_reason,
            "detail": self.detail,
            "transitions": [t.as_dict() for t in self.transitions],
            "replayed": self.replayed,
        }


@dataclass(frozen=True)
class PlanOutcome:
    state: State
    reached: tuple[str, ...]
    not_reached: tuple[tuple[str, str], ...]
    receipts: tuple[Receipt, ...]


# --------------------------------------------------------------------------- helpers


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_json(obj: object) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def operation_id_for(coordinate_key: str, source_rev: str, expected_base: str, digest: str) -> str:
    return _sha256_json(
        {
            "coordinate": coordinate_key,
            "source_rev": source_rev,
            "expected_base": expected_base,
            "digest": digest,
        }
    )


def pending_id_for(coordinate_key: str, source_rev: str, attempt: int) -> str:
    return _sha256_json(
        {"coordinate": coordinate_key, "source_rev": source_rev, "attempt": attempt}
    )


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or _ISOLATED_GIT_ENV)},
    )
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, proc.args, proc.stdout, proc.stderr)
    return proc.stdout.strip()


def tree_digest(repo: Path, rev: str, env: dict[str, str] | None = None) -> str:
    """The content digest of a revision: its tree id, recomputed from the object store."""
    return _git(repo, "rev-parse", f"{rev}^{{tree}}", env=env)


# --------------------------------------------------------------------------- journal


class Journal:
    """Append-only record of transitions and notes, plus per-coordinate cursors."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.path = state_dir / "journal.jsonl"
        self.cursors_dir = state_dir / "cursors"
        # Rows are cached against the file's (size, mtime_ns) because Prototype 1 asks
        # the machine to account for an observation on EVERY tick, including idle ones,
        # which moved this parse from once-per-change to once-per-tick. The Propagator
        # is built once per loop and holds one Journal, so the cache spans ticks.
        #
        # LIMITATION, stated rather than assumed away: an in-place mutation that changes
        # neither size nor mtime_ns is not detected. The append-only writer never
        # produces that state, and the next real append invalidates the entry — but a
        # cache sitting on a corruption-detection path should name what it cannot see.
        self._cache_key: tuple[int, int] | None = None
        self._cached_rows: list[dict[str, object]] = []
        self._malformed: tuple[MalformedLine, ...] = ()

    @property
    def malformed_lines(self) -> tuple[MalformedLine, ...]:
        """Lines the last read could not parse. Empty is the ordinary answer."""
        return self._malformed

    # -- rows

    def _rows(self) -> list[dict[str, object]]:
        """Every readable row, oldest first. Unreadable lines are dropped and counted.

        A kill between ``_write``'s write and its fsync leaves a partial line, so an
        unparseable line is an ordinary consequence of the writer being killed — not
        evidence of corruption, and not a reason to refuse. Raising here would exit the
        daemon on its first tick and on every restart after it, because the loop lets
        anything outside its named failure list propagate.

        Position does NOT discriminate cause. A tear is trailing only until the daemon
        restarts and appends again, after which the same orphan sits mid-file with intact
        rows on both sides. The question that does discriminate is whether the CURSOR can
        still be accounted for, and ``replay`` already asks it and already raises
        ``JournalInconsistent`` by name. Dropping a line here lets that check be the one
        that decides, instead of a bare ``ValueError`` from this parse pre-empting it.

        Returned rows are shared with the cache and must be treated as read-only; every
        consumer in this module filters or copies rather than mutating.
        """
        try:
            stat = self.path.stat()
        except (FileNotFoundError, NotADirectoryError):
            self._cache_key, self._cached_rows, self._malformed = None, [], ()
            return []

        key = (stat.st_size, stat.st_mtime_ns)
        if key == self._cache_key:
            return self._cached_rows

        rows: list[dict[str, object]] = []
        malformed: list[MalformedLine] = []
        for index, raw in enumerate(self.path.read_text().splitlines()):
            line = raw.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                malformed.append(
                    MalformedLine(index=index, excerpt=line[:120], reason=str(exc))
                )
        self._cache_key, self._cached_rows, self._malformed = key, rows, tuple(malformed)
        return rows

    def _write(self, row: dict[str, object]) -> None:
        """Append one row, terminating a torn remnant first.

        Without the repair, appending onto a partial line GLUES this row to it, and the
        tear costs two rows instead of one: the interrupted write, and the next write,
        which was itself correct and fsynced. Terminating first confines the damage to
        the line that was actually interrupted.

        Two writers racing can at worst both terminate, leaving a blank line, which
        ``_rows`` already skips.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        unterminated = False
        try:
            with self.path.open("rb") as probe:
                if probe.seek(0, os.SEEK_END):
                    probe.seek(-1, os.SEEK_END)
                    unterminated = probe.read(1) != b"\n"
        except FileNotFoundError:
            pass
        with self.path.open("a") as handle:
            if unterminated:
                handle.write("\n")
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def append(
        self,
        *,
        pending_id: str,
        attempt: int,
        operation_id: str | None,
        coordinate_key: str,
        source_rev: str,
        state: State,
        observation: dict[str, object],
    ) -> Transition:
        transition = Transition(
            state=state, observation=observation, timestamp=_now(), operation_id=operation_id
        )
        self._write(
            {
                "kind": "transition",
                "pending_id": pending_id,
                "attempt": attempt,
                "operation_id": operation_id,
                "coordinate_key": coordinate_key,
                "source_rev": source_rev,
                **transition.as_dict(),
            }
        )
        return transition

    def note(
        self,
        *,
        pending_id: str,
        attempt: int,
        coordinate_key: str,
        source_rev: str,
        note: str,
        data: dict[str, object] | None = None,
    ) -> None:
        self._write(
            {
                "kind": "note",
                "pending_id": pending_id,
                "attempt": attempt,
                "coordinate_key": coordinate_key,
                "source_rev": source_rev,
                "note": note,
                "data": data or {},
                "timestamp": _now(),
            }
        )

    def rows_for(self, coordinate_key: str, source_rev: str) -> list[dict[str, object]]:
        return [
            r
            for r in self._rows()
            if r.get("coordinate_key") == coordinate_key and r.get("source_rev") == source_rev
        ]

    def find(self, coordinate_key: str, source_rev: str) -> list[list[Transition]]:
        """Every attempt at (coordinate, source revision), oldest first, each as its transitions."""
        attempts: dict[int, list[Transition]] = {}
        for row in self.rows_for(coordinate_key, source_rev):
            if row.get("kind") != "transition":
                continue
            attempts.setdefault(int(row["attempt"]), []).append(
                Transition(
                    state=State(str(row["state"])),
                    observation=dict(row["observation"]),  # type: ignore[arg-type]
                    timestamp=str(row["timestamp"]),
                    operation_id=row.get("operation_id"),  # type: ignore[arg-type]
                )
            )
        return [attempts[k] for k in sorted(attempts)]

    def notes(self, coordinate_key: str, source_rev: str, note: str) -> int:
        return sum(
            1
            for r in self.rows_for(coordinate_key, source_rev)
            if r.get("kind") == "note" and r.get("note") == note
        )

    # -- cursors

    def cursor_path(self, coordinate_key: str) -> Path:
        return self.cursors_dir / (hashlib.sha256(coordinate_key.encode()).hexdigest() + ".json")

    def cursor(self, coordinate_key: str) -> str | None:
        path = self.cursor_path(coordinate_key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        value = data.get("source_rev")
        return str(value) if value else None

    def advance_cursor(self, coordinate_key: str, source_rev: str, pending_id: str) -> None:
        self.cursors_dir.mkdir(parents=True, exist_ok=True)
        path = self.cursor_path(coordinate_key)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "coordinate_key": coordinate_key,
                    "source_rev": source_rev,
                    "pending_id": pending_id,
                    "advanced_at": _now(),
                },
                indent=2,
            )
        )
        with tmp.open("rb") as handle:
            os.fsync(handle.fileno())
        tmp.rename(path)


# ------------------------------------------------------------------------ the machine


@dataclass
class _Op:
    """The in-flight operation, reconstructed from the journal on resume."""

    coordinate: Coordinate
    destination: Destination
    source_rev: str
    attempt: int
    pending_id: str
    previous_cursor: str | None
    expected_base: str | None = None
    digest: str | None = None
    operation_id: str | None = None
    observed_base: str | None = None
    intended_after: str | None = None
    plan_hash: str | None = None
    gate_results: tuple[GateResult, ...] = ()
    transitions: list[Transition] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.coordinate.key()


class Propagator:
    def __init__(
        self,
        source_remote: Path,
        branch: str,
        state_dir: Path,
        policy: Policy,
        *,
        kill_after: State | str | None = None,
        after_apply_verb: Callable[[], None] | None = None,
        before_apply_verb: Callable[[], None] | None = None,
        git_env: dict[str, str] | None = None,
    ) -> None:
        self.source_remote = source_remote
        self.branch = branch
        self.state_dir = state_dir
        self.policy = policy
        self.kill_after = kill_after
        self.after_apply_verb = after_apply_verb
        # Prototype 2 test seam: runs AFTER the apply step's own head check and BEFORE
        # the verb, so a test can move a canonical destination inside the window the
        # process-side check cannot see. The lease is what must catch that move.
        self.before_apply_verb = before_apply_verb
        # None means "the default, isolated from the host"; an explicit {} means "inherit the
        # host environment" and must not collapse into the default because it is falsy
        self.git_env = _ISOLATED_GIT_ENV if git_env is None else git_env
        self.journal = Journal(state_dir)
        self.mirror = state_dir / "mirror.git"

    # -- observation of the source

    def observe_source_at(self, cursor: str | None) -> Observation:
        out = subprocess.run(
            ["git", "ls-remote", str(self.source_remote), f"refs/heads/{self.branch}"],
            capture_output=True,
            text=True,
            env={**os.environ, **self.git_env},
        )
        if out.returncode != 0 or not out.stdout.strip():
            stderr = out.stderr.strip().splitlines()
            tail = stderr[-1] if stderr else "no output"
            raise SourceUnobservable(
                f"git ls-remote exited {out.returncode} for {self.source_remote} "
                f"refs/heads/{self.branch}: {tail}"
            )
        source_rev = out.stdout.split()[0]
        return Observation(source_rev=source_rev, cursor=cursor, is_new=(source_rev != cursor))

    def observe_source(self, coordinate: Coordinate) -> Observation:
        return self.observe_source_at(self.journal.cursor(coordinate.key()))

    # -- destination reads (never writes before apply)

    def read_head(self, destination: Destination) -> str:
        # A canonical destination is read at its BRANCH ref, never HEAD: a bare
        # repository's HEAD is a symref that may point anywhere, and the thing a
        # contribution lands on is the branch the owner declared.
        ref = (
            f"refs/heads/{self.branch}" if destination.kind is DestinationKind.CANONICAL else "HEAD"
        )
        try:
            return _git(destination.path, "rev-parse", "--verify", ref, env=self.git_env)
        except (subprocess.CalledProcessError, OSError) as exc:
            raise DestinationUnreadable(f"{destination.destination_id}: {exc}") from exc

    def _porcelain(self, destination: Destination) -> str:
        return _git(destination.path, "status", "--porcelain", env=self.git_env)

    def _observed_ref_source(self, destination: Destination) -> str:
        """The ref the mirror fetches for ahead/behind: the branch for a canonical, else HEAD."""
        if destination.kind is DestinationKind.CANONICAL:
            return f"refs/heads/{self.branch}"
        return "HEAD"

    # -- driver

    def run(
        self,
        coordinate: Coordinate,
        destination: Destination,
        observation: Observation | None = None,
        *,
        stop_after: State | None = None,
    ) -> Receipt | None:
        """Drive one coordinate. ``None`` means no new source revision: not an operation.

        Raises ``JournalInconsistent`` if the cursor sits at a revision the journal never
        acknowledged. A caller that has already observed the source this tick may pass
        its ``observation`` so the source is not asked twice; the cursor check runs on it
        all the same, because "nothing new" is exactly the answer that would hide a
        corrupted sink state, whoever observed.

        ``stop_after=State.PLANNED`` drives the operation through its gates and stops
        BEFORE the apply verb, journaled at ``planned``; a later ``run`` resumes it from
        there. This is how a contribution SET prepares every member against its owner's
        current base before landing any of them (Prototype 2).
        """
        return self._run(
            coordinate,
            destination,
            report_current=False,
            observation=observation,
            stop_after=stop_after,
        )

    def _run(
        self,
        coordinate: Coordinate,
        destination: Destination,
        *,
        report_current: bool,
        observation: Observation | None = None,
        stop_after: State | None = None,
    ) -> Receipt | None:
        key = coordinate.key()
        if observation is None:
            observation = self.observe_source(coordinate)
        if not observation.is_new:
            # The cursor is AT the source revision, which only happens after an
            # acknowledgement at that revision; _terminal_receipt RAISES if the
            # journal carries no such acknowledgement, on both paths, because a
            # cursor the journal cannot account for is a corrupted sink state and
            # "nothing new" is exactly the answer that would hide it. For an
            # aggregate the target was declared and already reached; report the
            # terminal outcome rather than dropping the target, because a dropped
            # target is indistinguishable from one never part of the invocation.
            terminal = self._terminal_receipt(coordinate, observation.source_rev)
            return terminal if report_current else None
        source_rev = observation.source_rev

        attempts = self.journal.find(key, source_rev)
        if attempts:
            last = attempts[-1]
            last_state = last[-1].state
            if last_state is State.ACKNOWLEDGED:
                # terminal: the original outcome, no verb, and the cursor repaired if it lagged
                attempt = len(attempts)
                pending_id = pending_id_for(key, source_rev, attempt)
                self.journal.note(
                    pending_id=pending_id,
                    attempt=attempt,
                    coordinate_key=key,
                    source_rev=source_rev,
                    note="replayed-terminal",
                    data={"state": str(last_state)},
                )
                self.journal.advance_cursor(key, source_rev, pending_id)
                return self._receipt(coordinate, source_rev, attempt, replayed=True)
            if last_state is State.REFUSED:
                op = self._fresh(
                    coordinate,
                    destination,
                    source_rev,
                    observation.cursor,
                    attempt=len(attempts) + 1,
                )
            else:
                op = self._resume(
                    coordinate,
                    destination,
                    source_rev,
                    observation.cursor,
                    attempt=len(attempts),
                    transitions=last,
                )
        else:
            op = self._fresh(coordinate, destination, source_rev, observation.cursor, attempt=1)

        self._drive(op, stop_after=stop_after)
        return self._receipt(coordinate, source_rev, op.attempt, replayed=False)

    def _terminal_receipt(self, coordinate: Coordinate, source_rev: str) -> Receipt:
        """The original outcome for a coordinate whose cursor is already at ``source_rev``."""
        attempts = self.journal.find(coordinate.key(), source_rev)
        if not attempts or attempts[-1][-1].state is not State.ACKNOWLEDGED:
            # A cursor at a revision the journal never acknowledged is a corrupted
            # sink state, and the prototype says so rather than guessing an outcome.
            raise JournalInconsistent(
                f"cursor for {coordinate.destination} is at {source_rev} but the journal "
                "carries no acknowledged attempt at that revision"
            )
        return self._receipt(coordinate, source_rev, len(attempts), replayed=True)

    def run_all(self, targets: Iterable[tuple[Coordinate, Destination]]) -> PlanOutcome | None:
        """Drive every declared target and classify the aggregate.

        A declared target whose cursor is already at the source revision was reached
        by an earlier invocation; it is reported as reached with its terminal receipt
        (``replayed=True``), never dropped. Dropping it would reclassify a replayed
        ``partial`` as ``refused``, which is what the first cut did (found in review):
        the aggregate must distinguish "already reached" from "not part of this
        invocation", and the only way to do that is to keep reporting it.
        ``None`` only when no targets were declared.
        """
        receipts: list[Receipt] = []
        for coordinate, destination in targets:
            receipt = self._run(coordinate, destination, report_current=True)
            if receipt is not None:
                receipts.append(receipt)
        if not receipts:
            return None
        reached = tuple(r.coordinate.destination for r in receipts if r.state is State.ACKNOWLEDGED)
        not_reached = tuple(
            (r.coordinate.destination, r.refusal_reason or r.detail or f"state={r.state}")
            for r in receipts
            if r.state is not State.ACKNOWLEDGED
        )
        if not not_reached:
            state = State.ACKNOWLEDGED
        elif not reached:
            state = State.REFUSED
        else:
            state = State.PARTIAL
        return PlanOutcome(
            state=state, reached=reached, not_reached=not_reached, receipts=tuple(receipts)
        )

    # -- attempt construction

    def _fresh(
        self,
        coordinate: Coordinate,
        destination: Destination,
        source_rev: str,
        cursor: str | None,
        *,
        attempt: int,
    ) -> _Op:
        return _Op(
            coordinate=coordinate,
            destination=destination,
            source_rev=source_rev,
            attempt=attempt,
            pending_id=pending_id_for(coordinate.key(), source_rev, attempt),
            previous_cursor=cursor,
        )

    def _resume(
        self,
        coordinate: Coordinate,
        destination: Destination,
        source_rev: str,
        cursor: str | None,
        *,
        attempt: int,
        transitions: list[Transition],
    ) -> _Op:
        op = self._fresh(coordinate, destination, source_rev, cursor, attempt=attempt)
        op.transitions = list(transitions)
        for t in transitions:
            obs = t.observation
            if t.state is State.OBSERVED:
                op.expected_base = str(obs["expected_base"])
            elif t.state is State.FETCHED:
                op.digest = str(obs["digest"])
                op.operation_id = t.operation_id
            elif t.state is State.PLANNED:
                op.observed_base = str(obs["observed_base"])
                op.intended_after = str(obs["intended_after"])
                op.plan_hash = str(obs["plan_hash"])
                op.gate_results = tuple(
                    GateResult(**g)
                    for g in obs["gate_results"]  # type: ignore[arg-type]
                )
        return op

    # -- the state machine

    def _drive(self, op: _Op, *, stop_after: State | None = None) -> None:
        last = op.transitions[-1].state if op.transitions else None
        if last is None:
            self._observe(op)
            last = State.OBSERVED
        if last is State.OBSERVED:
            self._fetch(op)
            last = State.FETCHED
        if last is State.FETCHED:
            if not self._plan(op):
                return  # refused at plan; journaled
            last = State.PLANNED
        if stop_after is State.PLANNED and last is State.PLANNED:
            return  # prepared: gates evaluated and journaled, no verb has run
        if last in (State.PLANNED, State.UNVERIFIABLE):
            if not self._apply(op):
                return  # refused or unverifiable; journaled
            last = State.APPLIED
        if last is State.APPLIED:
            if not self._verify(op):
                return  # postcondition did not hold; journaled as a note, state stays applied
            last = State.VERIFIED
        if last is State.VERIFIED:
            self._acknowledge(op)

    def _record(self, op: _Op, state: State, observation: dict[str, object]) -> None:
        transition = self.journal.append(
            pending_id=op.pending_id,
            attempt=op.attempt,
            operation_id=op.operation_id,
            coordinate_key=op.key,
            source_rev=op.source_rev,
            state=state,
            observation=observation,
        )
        op.transitions.append(transition)
        if self.kill_after == state:
            raise SinkKilled(f"killed after {state}")

    def _note(self, op: _Op, note: str, data: dict[str, object] | None = None) -> None:
        self.journal.note(
            pending_id=op.pending_id,
            attempt=op.attempt,
            coordinate_key=op.key,
            source_rev=op.source_rev,
            note=note,
            data=data,
        )

    def _observe(self, op: _Op) -> None:
        op.expected_base = self.read_head(op.destination)
        self._record(
            op,
            State.OBSERVED,
            {
                "source_rev": op.source_rev,
                "source_branch": self.branch,
                "previous_cursor": op.previous_cursor,
                "expected_base": op.expected_base,
                "established_by": (
                    "ls-remote on the source reported a revision the cursor did not name"
                ),
            },
        )

    def _fetch(self, op: _Op) -> None:
        if not self.mirror.exists():
            self.mirror.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "init", "-q", "--bare", str(self.mirror)],
                check=True,
                capture_output=True,
                text=True,
                env={**os.environ, **self.git_env},
            )
        _git(
            self.mirror,
            "fetch",
            "-q",
            str(self.source_remote),
            f"+refs/heads/{self.branch}:refs/remotes/source/{self.branch}",
            env=self.git_env,
        )
        # presence is asserted against the object itself, not the fetch's exit status
        _git(self.mirror, "cat-file", "-e", f"{op.source_rev}^{{commit}}", env=self.git_env)
        op.digest = tree_digest(self.mirror, op.source_rev, env=self.git_env)
        assert op.expected_base is not None
        op.operation_id = operation_id_for(op.key, op.source_rev, op.expected_base, op.digest)
        self._record(
            op,
            State.FETCHED,
            {
                "digest": op.digest,
                "mirror": str(self.mirror),
                "established_by": (
                    "cat-file -e on the mirror and the tree id recomputed from the object"
                ),
            },
        )

    def _gate(self, gate_id: str, passed: bool, detail: str) -> GateResult:
        result = "pass" if passed else "fail"
        return GateResult(
            gate_id=gate_id,
            result=result,
            detail=detail,
            result_hash=_sha256_json({"gate": gate_id, "result": result, "detail": detail}),
        )

    def _gate_not_run(self, gate_id: str, detail: str) -> GateResult:
        """A gate judged inapplicable to this destination kind, recorded as such.

        ``not-run`` is neither pass nor fail: it does not refuse, and it does not let a
        reader mistake "the list was empty" for "every gate passed".
        """
        return GateResult(
            gate_id=gate_id,
            result=_GATE_NOT_RUN,
            detail=detail,
            result_hash=_sha256_json({"gate": gate_id, "result": _GATE_NOT_RUN, "detail": detail}),
        )

    def _plan(self, op: _Op) -> bool:
        assert op.expected_base is not None and op.digest is not None
        gates: list[GateResult] = []

        allowed = op.coordinate.direction in self.policy.allowed_directions
        gates.append(
            self._gate(
                "policy.direction",
                allowed,
                f"direction={op.coordinate.direction} "
                f"allowed={sorted(str(d) for d in self.policy.allowed_directions)}",
            )
        )

        op.observed_base = self.read_head(op.destination)
        gates.append(
            self._gate(
                "destination.base-unmoved",
                op.observed_base == op.expected_base,
                f"expected_base={op.expected_base} observed_base={op.observed_base}",
            )
        )

        if op.destination.kind is DestinationKind.CANONICAL:
            # a bare repository has no worktree; the gate is recorded as NOT RUN rather
            # than omitted, so a receipt with an empty gate list and one where this
            # gate was judged inapplicable remain distinguishable (a gate list must say
            # which gates ran, individually; an aggregate cannot)
            gates.append(
                self._gate_not_run(
                    "destination.clean",
                    "bare canonical destination: cleanliness is not a property it has",
                )
            )
        else:
            porcelain = self._porcelain(op.destination)
            gates.append(
                self._gate(
                    "destination.clean",
                    porcelain == "",
                    "worktree and index clean"
                    if porcelain == ""
                    else f"dirty: {len(porcelain.splitlines())} path(s)",
                )
            )

        # the destination's branch (canonical) or HEAD (clone) is fetched INTO the
        # mirror (a read of the destination), so ahead/behind is computed in the
        # sink's own store
        observed_tag = hashlib.sha256(op.destination.destination_id.encode()).hexdigest()[:16]
        observed_ref = f"refs/observed/{observed_tag}"
        _git(
            self.mirror,
            "fetch",
            "-q",
            str(op.destination.path),
            f"+{self._observed_ref_source(op.destination)}:{observed_ref}",
            env=self.git_env,
        )
        counts = _git(
            self.mirror,
            "rev-list",
            "--left-right",
            "--count",
            f"{op.source_rev}...{observed_ref}",
            env=self.git_env,
        )
        behind_s, ahead_s = counts.split()
        behind, ahead = int(behind_s), int(ahead_s)
        gates.append(
            self._gate(
                "destination.fast-forward",
                ahead == 0,
                f"ahead={ahead} behind={behind} (destination relative to source revision)",
            )
        )

        op.intended_after = op.source_rev
        op.gate_results = tuple(gates)
        op.plan_hash = _sha256_json(
            {
                "operation_id": op.operation_id,
                "intended_after": op.intended_after,
                "policy_hash": self.policy.policy_hash,
                "gates": [g.gate_id for g in gates],
                "destination_kind": str(op.destination.kind),
            }
        )
        self._record(
            op,
            State.PLANNED,
            {
                "observed_base": op.observed_base,
                "intended_after": op.intended_after,
                "plan_hash": op.plan_hash,
                "policy_hash": self.policy.policy_hash,
                "gate_results": [g.as_dict() for g in gates],
                "established_by": (
                    "every gate evaluated and recorded individually against the destination "
                    "as read now"
                ),
            },
        )
        failed = [g for g in gates if g.result == "fail"]
        if failed:
            first = failed[0]
            self._record(
                op,
                State.REFUSED,
                {
                    "refusal_reason": f"{first.gate_id}: {first.detail}",
                    "failed_gates": [g.gate_id for g in failed],
                    "observed_base": op.observed_base,
                },
            )
            return False
        return True

    def _apply(self, op: _Op) -> bool:
        assert op.expected_base is not None and op.intended_after is not None
        try:
            head = self.read_head(op.destination)
        except DestinationUnreadable as exc:
            # nothing has been done to the destination yet, so this is a named refusal
            self._record(
                op,
                State.REFUSED,
                {"refusal_reason": f"destination.unreadable: {exc}", "observed_base": None},
            )
            return False

        if head == op.intended_after:
            # the verb landed in an earlier life of this sink; do not run it again
            self._record(
                op,
                State.APPLIED,
                {
                    "after": head,
                    "observed_base": head,
                    "verb_ran_now": False,
                    "established_by": (
                        "read-back found the intended revision already at the destination"
                    ),
                },
            )
            return True
        if head != op.expected_base:
            self._record(
                op,
                State.REFUSED,
                {
                    "refusal_reason": (
                        f"expected_base moved: expected {op.expected_base}, observed {head}"
                    ),
                    "observed_base": head,
                },
            )
            return False

        self._note(
            op, "apply-verb-started", {"expected_base": head, "intended_after": op.intended_after}
        )
        if self.before_apply_verb is not None:
            self.before_apply_verb()
        if op.destination.kind is DestinationKind.CANONICAL:
            # Prototype 2 (the contribution protocol): the contribution lands by a push FROM
            # the mirror (which holds the fetched source objects) TO the owner's bare remote, as a
            # fast-forward of ``expected_base`` guarded by ``--force-with-lease`` on
            # that same base. The receiving repository enforces the compare-and-swap:
            # if its branch is no longer at ``expected_base`` when the push arrives —
            # including a move inside the window after this process's own head check —
            # the push is rejected and nothing lands. That rejection is a REFUSAL with
            # the observed base, not an exception: it is the protocol doing its job.
            # Why ``--force-with-lease`` can never FORCE here: the plan's
            # ``destination.fast-forward`` gate already established that ``intended_after``
            # descends from ``expected_base`` (ahead == 0), so when the lease holds the
            # update is a genuine fast-forward, and when the owner moved the lease rejects
            # it before any update. Mutation-checked: forcing the gate to pass lets a stale
            # contribution overwrite the owner (W2b goes red); dropping the lease for a bare
            # ``--force`` lets a move inside the check→push window be overwritten (W2d goes
            # red). The two halves are one mechanism and neither is sufficient alone.
            lease = f"--force-with-lease=refs/heads/{self.branch}:{op.expected_base}"
            push = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.mirror),
                    "push",
                    "-q",
                    lease,
                    str(op.destination.path),
                    f"{op.intended_after}:refs/heads/{self.branch}",
                ],
                capture_output=True,
                text=True,
                env={**os.environ, **self.git_env},
            )
            if push.returncode != 0:
                try:
                    moved_to = self.read_head(op.destination)
                except DestinationUnreadable as exc:
                    self._record(
                        op,
                        State.UNVERIFIABLE,
                        {
                            "detail": (
                                "the lease push returned non-zero and the destination could "
                                f"not be read back: {exc}"
                            ),
                            "observed_base": None,
                        },
                    )
                    return False
                if moved_to != op.expected_base:
                    self._record(
                        op,
                        State.REFUSED,
                        {
                            "refusal_reason": (
                                f"destination.lease-refused: the owner's branch moved to "
                                f"{moved_to} after the base check; expected {op.expected_base}"
                            ),
                            "observed_base": moved_to,
                            "established_by": (
                                "the receiving repository rejected the lease; read back names "
                                "the revision it holds"
                            ),
                        },
                    )
                    return False
                raise RuntimeError(
                    "lease push failed although the owner's branch is still at the expected "
                    f"base: {push.stderr.strip()}"
                )
        else:
            _git(
                op.destination.path,
                "fetch",
                "-q",
                str(self.mirror),
                f"refs/remotes/source/{self.branch}",
                env=self.git_env,
            )
            _git(
                op.destination.path, "merge", "-q", "--ff-only", op.intended_after, env=self.git_env
            )
        if self.after_apply_verb is not None:
            self.after_apply_verb()
        if self.kill_after == KILL_AFTER_APPLY_VERB:
            raise SinkKilled("killed after the apply verb, before the read back")

        try:
            after = self.read_head(op.destination)
        except DestinationUnreadable as exc:
            self._record(
                op,
                State.UNVERIFIABLE,
                {
                    "detail": (
                        f"the apply verb returned but the destination could not be read back: {exc}"
                    ),
                    "observed_base": None,
                },
            )
            return False

        if after != op.intended_after:
            self._record(
                op,
                State.REFUSED,
                {
                    "refusal_reason": (
                        f"destination.apply-mismatch: verb returned but destination reports {after}"
                    ),
                    "observed_base": after,
                },
            )
            return False

        self._record(
            op,
            State.APPLIED,
            {
                "after": after,
                "observed_base": head,
                "verb_ran_now": True,
                "established_by": (
                    "rev-parse HEAD on the destination after the verb reports the intended revision"
                ),
            },
        )
        return True

    def _verify(self, op: _Op) -> bool:
        assert op.intended_after is not None and op.digest is not None
        head = self.read_head(op.destination)
        tree = tree_digest(op.destination.path, head, env=self.git_env)
        if op.destination.kind is DestinationKind.CANONICAL:
            postcondition = _POSTCONDITION_CANONICAL
            holds = head == op.intended_after and tree == op.digest
            detail = f"branch={head} tree={tree}"
        else:
            postcondition = _POSTCONDITION
            porcelain = self._porcelain(op.destination)
            holds = head == op.intended_after and tree == op.digest and porcelain == ""
            detail = f"head={head} tree={tree} clean={porcelain == ''}"
        if not holds:
            self._note(
                op, "postcondition-failed", {"postcondition": postcondition, "detail": detail}
            )
            return False
        self._record(
            op,
            State.VERIFIED,
            {
                "postcondition_checked": postcondition,
                "holds": True,
                "detail": detail,
                "established_by": "the named postcondition re-read from the destination",
            },
        )
        return True

    def _acknowledge(self, op: _Op) -> None:
        # the journal row is the acknowledgment; the cursor is derived from it and
        # repaired from it on replay, so the row is written first
        self._record(
            op,
            State.ACKNOWLEDGED,
            {"cursor": op.source_rev, "established_by": "verified was recorded for this operation"},
        )
        self.journal.advance_cursor(op.key, op.source_rev, op.pending_id)

    # -- receipts

    def _receipt(
        self, coordinate: Coordinate, source_rev: str, attempt: int, *, replayed: bool
    ) -> Receipt:
        key = coordinate.key()
        # rows are read in journal order: a note and a later transition about the same
        # fact resolve to whichever came last, never to whichever kind is handled last
        rows = [r for r in self.journal.rows_for(key, source_rev) if int(r["attempt"]) == attempt]
        transitions: list[Transition] = []

        expected_base = ""
        observed_base: str | None = None
        after: str | None = None
        digest: str | None = None
        operation_id: str | None = None
        plan_hash: str | None = None
        gate_results: tuple[GateResult, ...] = ()
        postcondition_checked: str | None = None
        postcondition_holds: bool | None = None
        refusal_reason: str | None = None
        detail = ""

        for row in rows:
            if row.get("kind") == "note":
                if row.get("note") == "postcondition-failed":
                    data = row.get("data") or {}
                    postcondition_checked = str(data.get("postcondition", _POSTCONDITION))  # type: ignore[union-attr]
                    postcondition_holds = False
                    detail = str(data.get("detail", ""))  # type: ignore[union-attr]
                continue
            t = Transition(
                state=State(str(row["state"])),
                observation=dict(row["observation"]),  # type: ignore[arg-type]
                timestamp=str(row["timestamp"]),
                operation_id=row.get("operation_id"),  # type: ignore[arg-type]
            )
            transitions.append(t)
            obs = t.observation
            if t.state is State.OBSERVED:
                expected_base = str(obs["expected_base"])
            elif t.state is State.FETCHED:
                digest = str(obs["digest"])
                operation_id = t.operation_id
            elif t.state is State.PLANNED:
                observed_base = str(obs["observed_base"])
                plan_hash = str(obs["plan_hash"])
                gate_results = tuple(GateResult(**g) for g in obs["gate_results"])  # type: ignore[arg-type]
            elif t.state is State.APPLIED:
                after = str(obs["after"])
                observed_base = str(obs["observed_base"])
            elif t.state is State.VERIFIED:
                postcondition_checked = str(obs["postcondition_checked"])
                postcondition_holds = True
                detail = str(obs.get("detail", ""))
            elif t.state is State.REFUSED:
                refusal_reason = str(obs["refusal_reason"])
                if obs.get("observed_base") is not None:
                    observed_base = str(obs["observed_base"])
            elif t.state is State.UNVERIFIABLE:
                detail = str(obs["detail"])

        if not transitions:
            raise LookupError(
                f"no transitions journaled for attempt {attempt} of {key} @ {source_rev}"
            )
        state = transitions[-1].state
        return Receipt(
            pending_id=pending_id_for(key, source_rev, attempt),
            attempt=attempt,
            operation_id=operation_id,
            coordinate=coordinate,
            source_rev=source_rev,
            expected_base=expected_base,
            observed_base=observed_base,
            after=after,
            digest=digest,
            plan_hash=plan_hash,
            policy_hash=self.policy.policy_hash,
            gate_results=gate_results,
            state=state,
            postcondition_checked=postcondition_checked,
            postcondition_holds=postcondition_holds,
            timestamp=transitions[-1].timestamp,
            refusal_reason=refusal_reason,
            detail=detail,
            transitions=tuple(transitions),
            replayed=replayed,
        )


__all__ = [
    "KILL_AFTER_APPLY_VERB",
    "Coordinate",
    "Destination",
    "DestinationKind",
    "DestinationUnreadable",
    "JournalInconsistent",
    "MalformedLine",
    "Direction",
    "GateResult",
    "Journal",
    "Observation",
    "Operation",
    "PlanOutcome",
    "Policy",
    "Propagator",
    "Receipt",
    "SinkKilled",
    "State",
    "Transition",
    "operation_id_for",
    "pending_id_for",
    "tree_digest",
]
