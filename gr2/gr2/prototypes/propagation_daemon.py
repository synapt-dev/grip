"""Prototype 1 of the propagation daemon: ONE declared managed replica, a real source.

Prototype 0 (``propagation_state_machine``) proved the state contract on synthetic
repositories. This module runs that same machine on a loop against a single destination
that a declaration names as a managed replica, so the daemon can watch a real canonical
upstream, fetch, plan, apply, verify, acknowledge, and leave a neutral receipt behind
for every operation it ran. Nothing here grants the daemon authority over an authoring
clone: the declaration can only name a replica, and the machine's own gates (direction,
base unmoved, clean, fast-forward) refuse everything the declaration cannot vouch for.

What one tick does, in order:

1. observe the source with ``git ls-remote``; if the cursor already names the source
   revision there is no operation, no receipt, and nothing is written;
2. otherwise drive the machine once and take its receipt, whatever its state --
   a refusal is a receipt too, because "I did not apply and here is why" is the
   only honest thing a replica manager can say about a destination it refused;
3. write the receipt as its own JSON file, with the per-state latency derived from
   the receipt's own transition timestamps rather than from a stopwatch around
   the call, so the numbers describe what the journal describes;
4. emit one ``propagation.receipt`` event on the gr2 outbox carrying the receipt's
   one-line summary. The design names a one-line channel notification; this module
   writes that line to the outbox and to stdout and knows no channel, so the
   prototype depends on nothing outside ``gr2``.

Success for Prototype 1 is measured latency plus exact receipts, not the absence of an
exception. Every receipt names the exact source and destination revisions, and every
latency figure is recomputable from the receipt that carries it.

The git environment is a declared choice. Tests run isolated from the host's
configuration (the Prototype 0 default); a real private remote needs the host's
credential helper, so a declaration may say ``"git_env": "inherit"``. The daemon never
creates a commit (fast-forward only), so inheriting the host configuration does not
invoke signing. In both modes ``GIT_TERMINAL_PROMPT=0`` is set: a daemon never answers
a prompt, so a missing credential fails the tick instead of hanging the loop on a tty.
A tick whose git call fails is printed as ``propagation tick-failed`` and counted, and
the loop continues; the next tick replays whatever the machine left pending.

The first dogfood run found the inherit seam the hard way: the machine collapsed an
explicit empty environment into its isolated default because ``{}`` is falsy, so the
clone that ``ensure_replica`` made with the host's credentials was followed by an
``ls-remote`` that prompted for a username. The machine now distinguishes ``None``
from ``{}`` and the seam is witnessed at both ends.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from gr2.prototypes.propagation_state_machine import (
    Coordinate,
    Destination,
    DestinationKind,
    DestinationUnreadable,
    Direction,
    Operation,
    Policy,
    Propagator,
    Receipt,
    SourceUnobservable,
    State,
)
from gr2.python_cli.events import EventType, emit

_ISOLATED_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}

ACTOR = "propagation-daemon"


class DeclarationError(ValueError):
    """The declaration does not describe exactly one managed replica."""


class DeclarationMismatch(RuntimeError):
    """The destination path exists and is not the declared replica."""


@dataclass(frozen=True)
class Declaration:
    """Exactly one managed replica of one branch of one source.

    Every field is opaque to the machine except ``branch`` (what ls-remote asks for) and
    ``destination_path`` (where the replica lives). ``kind`` is carried so a reader of the
    declaration can see the only value it may hold; ``load_declaration`` refuses any other.
    """

    source_url: str
    branch: str
    destination_id: str
    destination_path: Path
    state_dir: Path
    outbox_root: Path
    coordinate: Coordinate
    interval_seconds: float
    policy_hash: str
    git_env_mode: str = "isolated"
    kind: DestinationKind = DestinationKind.REPLICA

    @property
    def receipts_dir(self) -> Path:
        return self.state_dir / "receipts"

    @property
    def git_env(self) -> dict[str, str]:
        # A daemon never answers a prompt: in both modes a missing credential must fail
        # the git call (and the tick) rather than hang the loop on a tty. "inherit" carries
        # ONLY that override, so the host's credential helper and config are in force.
        base = {} if self.git_env_mode == "inherit" else dict(_ISOLATED_GIT_ENV)
        return {**base, "GIT_TERMINAL_PROMPT": "0"}

    def destination(self) -> Destination:
        return Destination(
            destination_id=self.destination_id, path=self.destination_path, kind=self.kind
        )

    def policy(self) -> Policy:
        # downward only: the one direction a managed replica can be the destination of
        return Policy(policy_hash=self.policy_hash, allowed_directions=frozenset({Direction.DOWN}))


_REQUIRED = (
    "source_url",
    "branch",
    "destination_id",
    "destination_path",
    "state_dir",
    "outbox_root",
    "coordinate",
)


def declaration_from_dict(data: dict[str, object]) -> Declaration:
    missing = [key for key in _REQUIRED if key not in data]
    if missing:
        raise DeclarationError(f"declaration is missing {missing}")
    kind = str(data.get("kind", DestinationKind.REPLICA))
    if kind != str(DestinationKind.REPLICA):
        # the one thing this daemon must never be told to do: an authoring clone is not
        # a destination it may drive, and that is decided here, before any git call
        raise DeclarationError(f"this daemon drives managed replicas only; declared kind={kind!r}")
    git_env_mode = str(data.get("git_env", "isolated"))
    if git_env_mode not in {"isolated", "inherit"}:
        raise DeclarationError(f"git_env must be 'isolated' or 'inherit', got {git_env_mode!r}")
    coord = data["coordinate"]
    if not isinstance(coord, dict):
        raise DeclarationError("coordinate must be an object")
    for key in ("source", "layer", "artifact_class"):
        if key not in coord:
            raise DeclarationError(f"coordinate is missing {key!r}")
    interval = float(data.get("interval_seconds", 30.0))
    if interval <= 0:
        raise DeclarationError("interval_seconds must be positive")
    destination_id = str(data["destination_id"])
    coordinate = Coordinate(
        source=str(coord["source"]),
        destination=destination_id,
        layer=str(coord["layer"]),
        direction=Direction.DOWN,
        operation=Operation.APPLY,
        artifact_class=str(coord["artifact_class"]),
    )
    return Declaration(
        source_url=str(data["source_url"]),
        branch=str(data["branch"]),
        destination_id=destination_id,
        destination_path=Path(str(data["destination_path"])).expanduser(),
        state_dir=Path(str(data["state_dir"])).expanduser(),
        outbox_root=Path(str(data["outbox_root"])).expanduser(),
        coordinate=coordinate,
        interval_seconds=interval,
        policy_hash=str(data.get("policy_hash", "prototype-1-downward-only")),
        git_env_mode=git_env_mode,
    )


def load_declaration(path: Path) -> Declaration:
    with path.open() as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise DeclarationError("declaration must be a JSON object")
    return declaration_from_dict(data)


# --------------------------------------------------------------------------- replica


def _git(repo: Path, *args: str, env: dict[str, str]) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **env},
    )
    return proc.stdout.strip()


def ensure_replica(declaration: Declaration) -> Path:
    """Make the declared replica exist, or refuse to treat what is there as the replica.

    Absent: clone the declared branch of the declared source, single-branch, so the only
    thing at that path is what the declaration says. Present: it must be a git checkout
    whose ``origin`` is the declared source and whose current branch is the declared
    branch; anything else is refused here, before the machine ever reads it, because the
    alternative is a daemon fast-forwarding a clone that happens to sit at the path.
    """
    path = declaration.destination_path
    env = declaration.git_env
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "git",
                "clone",
                "-q",
                "--branch",
                declaration.branch,
                "--single-branch",
                declaration.source_url,
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, **env},
        )
        return path
    try:
        origin = _git(path, "remote", "get-url", "origin", env=env)
        branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD", env=env)
    except (subprocess.CalledProcessError, OSError) as exc:
        raise DeclarationMismatch(f"{path} exists and is not a git checkout: {exc}") from exc
    if origin != declaration.source_url:
        raise DeclarationMismatch(
            f"{path} has origin {origin!r}, declaration names {declaration.source_url!r}"
        )
    if branch != declaration.branch:
        raise DeclarationMismatch(
            f"{path} is on {branch!r}, declaration names {declaration.branch!r}"
        )
    return path


# --------------------------------------------------------------------------- ticks


@dataclass(frozen=True)
class TickResult:
    observed_at: str
    source_rev: str
    cursor_before: str | None
    receipt: Receipt | None
    receipt_path: Path | None
    latency_seconds: dict[str, float] | None
    summary: str

    @property
    def was_operation(self) -> bool:
        return self.receipt is not None


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def latency_from_receipt(receipt: Receipt) -> dict[str, float]:
    """Per-state latency derived from the receipt's own transition timestamps.

    Keys are ``<from>-><to>`` for each consecutive pair and ``total`` from the first to
    the last transition. A replayed receipt carries the ORIGINAL transitions, so its
    latency describes the run that did the work, not the replay that found it.
    """
    transitions = receipt.transitions
    out: dict[str, float] = {}
    for previous, current in zip(transitions, transitions[1:], strict=False):
        delta = (_parse_ts(current.timestamp) - _parse_ts(previous.timestamp)).total_seconds()
        out[f"{previous.state}->{current.state}"] = round(delta, 6)
    if transitions:
        total = (
            _parse_ts(transitions[-1].timestamp) - _parse_ts(transitions[0].timestamp)
        ).total_seconds()
        out["total"] = round(total, 6)
    return out


def _short(rev: str | None) -> str:
    return (rev or "-")[:12]


def summarize(receipt: Receipt, latency: dict[str, float]) -> str:
    """The one line a channel reader needs: what, where, which revisions, how long."""
    landed = _short(receipt.after) if receipt.after else "unchanged"
    head = (
        f"propagation {receipt.state}: {receipt.coordinate.destination} "
        f"at {_short(receipt.expected_base)} -> {landed}, intended {_short(receipt.source_rev)} "
        f"(source {receipt.coordinate.source}, attempt {receipt.attempt}"
    )
    if receipt.replayed:
        head += ", replayed"
    head += f", total {latency.get('total', 0.0):.3f}s)"
    if receipt.state is State.REFUSED and receipt.refusal_reason:
        head += f" refused: {receipt.refusal_reason}"
    return head


def write_receipt(
    declaration: Declaration, receipt: Receipt, latency: dict[str, float], observed_at: str
) -> Path:
    declaration.receipts_dir.mkdir(parents=True, exist_ok=True)
    stamp = observed_at.replace(":", "").replace("+00:00", "Z")
    name = f"{stamp}-{receipt.pending_id[:12]}-{receipt.state}.json"
    path = declaration.receipts_dir / name
    payload = {
        "daemon": ACTOR,
        "declaration": {
            "source_url": declaration.source_url,
            "branch": declaration.branch,
            "destination_id": declaration.destination_id,
            "destination_path": str(declaration.destination_path),
        },
        "observed_at": observed_at,
        "latency_seconds": latency,
        "receipt": receipt.as_dict(),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return path


def notify(declaration: Declaration, receipt: Receipt, summary: str, receipt_path: Path) -> None:
    emit(
        EventType.PROPAGATION_RECEIPT,
        declaration.outbox_root,
        ACTOR,
        declaration.destination_id,
        {
            "summary": summary,
            "state": str(receipt.state),
            "pending_id": receipt.pending_id,
            "operation_id": receipt.operation_id,
            "source_rev": receipt.source_rev,
            "expected_base": receipt.expected_base,
            "after": receipt.after,
            "replayed": receipt.replayed,
            "receipt_path": str(receipt_path),
        },
    )


def make_propagator(declaration: Declaration) -> Propagator:
    # The machine only ever stringifies ``source_remote`` (ls-remote, fetch), so a URL
    # passes through as-is. It must NOT be wrapped in Path: Path collapses the "//" of a
    # URL scheme and the source would silently become a relative directory.
    return Propagator(
        source_remote=declaration.source_url,  # type: ignore[arg-type]
        branch=declaration.branch,
        state_dir=declaration.state_dir,
        policy=declaration.policy(),
        git_env=declaration.git_env,
    )


def tick(declaration: Declaration, propagator: Propagator) -> TickResult:
    observed_at = datetime.now(UTC).isoformat()
    coordinate = declaration.coordinate
    observation = propagator.observe_source(coordinate)
    if not observation.is_new:
        # Not an operation, but not a shortcut past the machine either: it must account
        # for a cursor that sits at the source revision, and it raises JournalInconsistent
        # for one the journal never acknowledged. An earlier version of this tick returned
        # here without asking, and "current; not an operation" would have hidden corrupted
        # sink state on every tick forever. Found by the witness, not by the dogfood.
        propagator.run(coordinate, declaration.destination(), observation=observation)
        return TickResult(
            observed_at=observed_at,
            source_rev=observation.source_rev,
            cursor_before=observation.cursor,
            receipt=None,
            receipt_path=None,
            latency_seconds=None,
            summary=(
                f"propagation current: {declaration.destination_id} cursor already at "
                f"{_short(observation.source_rev)}; not an operation"
            ),
        )
    receipt = propagator.run(coordinate, declaration.destination(), observation=observation)
    if receipt is None:
        # the machine's contract allows None for "not new"; with the observation shared it
        # cannot answer that here, but a None is still not an operation and writes nothing
        return TickResult(
            observed_at=observed_at,
            source_rev=observation.source_rev,
            cursor_before=observation.cursor,
            receipt=None,
            receipt_path=None,
            latency_seconds=None,
            summary=(
                f"propagation current: {declaration.destination_id} source returned to the "
                f"cursor between observe and run; not an operation"
            ),
        )
    latency = latency_from_receipt(receipt)
    summary = summarize(receipt, latency)
    receipt_path = write_receipt(declaration, receipt, latency, observed_at)
    notify(declaration, receipt, summary, receipt_path)
    return TickResult(
        observed_at=observed_at,
        source_rev=observation.source_rev,
        cursor_before=observation.cursor,
        receipt=receipt,
        receipt_path=receipt_path,
        latency_seconds=latency,
        summary=summary,
    )


@dataclass
class LoopStats:
    ticks: int = 0
    operations: int = 0
    failures: int = 0
    by_state: dict[str, int] = field(default_factory=dict)
    last: TickResult | None = None


# Every way a tick can fail on the environment rather than on this module, derived from
# the machine's raise sites: the source cannot be observed (before any state is touched),
# the destination cannot be read (at observe, plan, or verify, each a point the machine
# replays from), or a git call failed / could not be spawned. DestinationUnreadable WRAPS
# the last two, so catching them without it would let the wrapped form escape.
_TICK_FAILURES = (
    SourceUnobservable,
    DestinationUnreadable,
    subprocess.CalledProcessError,
    OSError,
)


def _git_failure_line(exc: BaseException) -> str:
    if isinstance(exc, SourceUnobservable):
        return str(exc)
    if isinstance(exc, DestinationUnreadable):
        return f"destination unreadable: {exc}"
    if isinstance(exc, subprocess.CalledProcessError):
        argv = (
            " ".join(str(part) for part in exc.cmd)
            if isinstance(exc.cmd, list | tuple)
            else exc.cmd
        )
        stderr = (exc.stderr or "").strip().splitlines()
        tail = stderr[-1] if stderr else ""
        return f"git exited {exc.returncode} ({argv}): {tail}"
    return f"{type(exc).__name__}: {exc}"


def run_loop(
    declaration: Declaration,
    *,
    once: bool = False,
    stop: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    out: TextIO | None = None,
) -> LoopStats:
    """Tick until ``stop()`` says so (or once). Every tick prints exactly one line.

    A tick that fails on the environment (the source unobservable, the destination
    unreadable because the checkout went away or a volume unmounted, a credential refused,
    the mirror fetch interrupted) is printed and counted and the loop goes on: nothing
    about the declaration changed, and whatever the machine left pending is replayed on
    the next tick, which is the machine's own kill-and-replay contract. The machine names
    the first two ``SourceUnobservable`` (raised before any state is touched) and
    ``DestinationUnreadable`` (raised at observe, plan, or verify, each a replay point);
    the rest arrive as ``CalledProcessError`` / ``OSError`` from its git calls. What
    propagates, by design: ``JournalInconsistent`` and ``LookupError`` from the machine (a
    journal that cannot account for its own cursor is corrupted sink state, and guessing
    would hide it), ``DeclarationMismatch`` from ``ensure_replica`` at startup, and any
    defect in this module.

    ``out`` defaults to the stdout in force at CALL time, not at import time, so a caller
    that redirects stdout (a test, a wrapper, a supervisor) gets the lines.
    """
    stream = out if out is not None else sys.stdout
    ensure_replica(declaration)
    propagator = make_propagator(declaration)
    stats = LoopStats()
    while True:
        observed_at = datetime.now(UTC).isoformat()
        try:
            result = tick(declaration, propagator)
        except _TICK_FAILURES as exc:
            stats.ticks += 1
            stats.failures += 1
            print(
                f"{observed_at} propagation tick-failed: {declaration.destination_id} "
                f"{_git_failure_line(exc)}; this tick left no receipt, the next tick "
                f"replays whatever the machine left pending",
                file=stream,
                flush=True,
            )
        else:
            stats.ticks += 1
            stats.last = result
            if result.receipt is not None:
                stats.operations += 1
                key = str(result.receipt.state)
                stats.by_state[key] = stats.by_state.get(key, 0) + 1
            print(f"{result.observed_at} {result.summary}", file=stream, flush=True)
        if once or (stop is not None and stop()):
            return stats
        sleep(declaration.interval_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--declaration", required=True, type=Path)
    parser.add_argument("--once", action="store_true", help="one tick, then exit")
    parser.add_argument(
        "--interval", type=float, default=None, help="override the declared interval"
    )
    args = parser.parse_args(argv)
    declaration = load_declaration(args.declaration)
    if args.interval is not None:
        declaration = replace(declaration, interval_seconds=float(args.interval))
    try:
        run_loop(declaration, once=args.once)
    except KeyboardInterrupt:
        print("propagation daemon: stopped", flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main() in tests
    sys.exit(main())
