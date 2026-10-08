#!/usr/bin/env python3
"""Repeated before/after stress harness for event-log sequence integrity."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import sys
import tempfile
from pathlib import Path
from queue import Empty as QueueEmpty

SOURCE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE_ROOT))

# A rendezvous that cannot complete within this is a NAMED failure rather than a
# hang -- see the barrier comment in `_worker`.
RENDEZVOUS_TIMEOUT = 30.0

# How long to wait for one worker's outcome after the join. A missing outcome is
# reported as a NAMED worker failure, never as a bare `queue.Empty` -- which is an
# ERROR rather than an assertion, so it used to report the row as BROKEN and cost
# a re-derivation to diagnose.
OUTCOME_TIMEOUT = 5.0

# The start gate's own bound, named because the JOIN must outlast the sum of these
# and not just one of them: a worker's worst path from process start is
# `START_GATE_TIMEOUT + RENDEZVOUS_TIMEOUT`, so a join of `RENDEZVOUS_TIMEOUT + 10`
# left no margin over it at all. Measured by a reader: join 40 against a worst path
# of 40.
START_GATE_TIMEOUT = 10.0

# The join must outlast a worker's WORST path, which is the sum above, plus room
# for it to place its named outcome on the queue afterwards.
JOIN_TIMEOUT = START_GATE_TIMEOUT + RENDEZVOUS_TIMEOUT + OUTCOME_TIMEOUT * 2

from gr2.python_cli.events import EventType, emit  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=30)
    parser.add_argument("--writers", type=int, default=2)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def _error_chain(exc: BaseException, depth: int = 5) -> str:
    """The exception and its `__cause__` chain, outermost first, one line.

    EXTRACTED SO A ROW CAN DRIVE IT WITHOUT SPAWNING A WRITER, and because the
    chain is the whole point: `emit` wraps a failure from the sequence read as
    `EventEmitError(...) from exc`, so what reaches the worker is the WRAPPER and
    the diagnosis lives in `__cause__`. A record that carries only the outermost
    name reports a generic emit failure for a barrier timeout -- which is what the
    comment beside the timeout, the PR body and one assertion message all claimed
    it did not.
    """
    chain: list[str] = []
    seen: BaseException | None = exc
    while seen is not None and len(chain) < depth:
        chain.append(f"{type(seen).__name__}: {seen}")
        seen = seen.__cause__
    return " <- ".join(chain)


def _worker(
    workspace: str,
    actor: str,
    start: object,
    queue: object,
    unlocked: bool,
    expected_source_root: str,
    rendezvous: object | None = None,
    arrivals: object | None = None,
) -> None:
    try:
        import gr2

        module_file = Path(gr2.__file__).resolve() if gr2.__file__ is not None else None
        source_root = Path(expected_source_root).resolve()
        if module_file is None or not module_file.is_relative_to(source_root):
            raise RuntimeError(
                f"spawned worker imported gr2 outside expected worktree: "
                f"module={module_file}, expected_root={source_root}"
            )
        if unlocked:
            os.environ["GR2_DISABLE_EVENT_LOCKING"] = "1"
            os.environ["GR2_EVENT_TEST_DELAY"] = "0.03"
        if rendezvous is not None:
            # FORCE THE INTERLEAVING, and this is the whole point of the harness.
            #
            # `emit` looks `_current_seq` up as a MODULE GLOBAL AT CALL TIME, so
            # wrapping that one function on the module puts a rendezvous exactly
            # between the READ and the WRITE -- and it does so with NO CHANGE to
            # production code, which is why this shape was chosen over a test-only
            # hook inside `emit`.
            #
            # Every writer therefore completes its read before ANY writer takes
            # the write, so the duplicate is a property of the FIXTURE rather than
            # of the scheduler. That is the difference between this and the delay
            # beside it: the delay WIDENS the window and hopes; this CLOSES the
            # question. A writer's process can be descheduled for a second and the
            # collision still happens, because nobody writes until everybody has
            # read.
            #
            # THE TIMEOUT IS NOT DECORATION. A barrier with no timeout hangs
            # forever if one writer dies before arriving, which would turn "a
            # worker crashed" into "the round never finished" -- the same
            # conflation of SLOW with HUNG that the fixed bounds elsewhere in this
            # file already produce. `BrokenBarrierError` is a NAMED failure and
            # reaches the row as `ok=False`.
            from gr2.python_cli import events as _events

            real_current_seq = _events._current_seq

            def _rendezvous_current_seq(
                outbox: Path,
                _real: object = real_current_seq,
                _barrier: object = rendezvous,
                _arrivals: object = arrivals,
            ) -> int:
                seq = _real(outbox)  # type: ignore[operator]
                # COUNTED BEFORE THE WAIT, so the number is an ARRIVAL and not a
                # completion: a writer increments whether or not the barrier ever
                # releases it. That distinction is what makes the count a
                # measurement of the rendezvous rather than of the round
                # finishing -- and it is the fix for a flag that reported the
                # ARGUMENT. `_run_round` used to return `"forced": force`, which a
                # stub spawning no writer at all would still report as True.
                if _arrivals is not None:
                    with _arrivals.get_lock():  # type: ignore[attr-defined]
                        _arrivals.value += 1  # type: ignore[attr-defined]
                _barrier.wait(timeout=RENDEZVOUS_TIMEOUT)  # type: ignore[attr-defined]
                return seq

            _events._current_seq = _rendezvous_current_seq
        if not start.wait(timeout=START_GATE_TIMEOUT):
            raise TimeoutError("start gate timed out")
        emit(
            event_type=EventType.LANE_ENTERED,
            workspace_root=Path(workspace),
            actor=actor,
            owner_unit="event-stress",
            payload={"round_actor": actor},
        )
    except Exception as exc:
        # RECORD THE WHOLE CAUSE CHAIN, because the exception that ARRIVES here is
        # not the one that matters. `emit` wraps any failure from the sequence read
        # as `EventEmitError(f"event emit failed for {outbox}") from exc`, so a
        # barrier timeout -- the exact case the timeout exists for -- reaches this
        # handler as a GENERIC emit failure with the `BrokenBarrierError` only as
        # `__cause__`. `repr(exc)` alone reported the wrapper and lost the
        # diagnosis, which made three separate claims false: the comment beside the
        # timeout, the PR body, and the assertion message in the row. Measured by a
        # reader before it was fixed.
        queue.put({"ok": False, "error": _error_chain(exc), "error_head": repr(exc)})
    else:
        queue.put({"ok": True, "gr2_module": str(module_file)})


def _read_rows(outbox: Path) -> tuple[list[dict[str, object]], int]:
    rows: list[dict[str, object]] = []
    corruption_count = 0
    for line in outbox.read_text().splitlines() if outbox.exists() else []:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            corruption_count += 1
            continue
        if not isinstance(row, dict):
            corruption_count += 1
            continue
        rows.append(row)
    return rows, corruption_count


def prove_corruption_detector() -> dict[str, object]:
    """Prove the reported zero can distinguish a known malformed event."""
    with tempfile.TemporaryDirectory(prefix="gr2-event-corruption-control-") as tmp:
        outbox = Path(tmp) / "outbox.jsonl"
        outbox.write_text("{malformed-json\n")
        _, corruption_count = _read_rows(outbox)
    return {
        "proven": corruption_count == 1,
        "fixture": "malformed-json",
        "detected_count": corruption_count,
    }


def _run_round(writers: int, unlocked: bool, *, force: bool | None = None) -> dict[str, object]:
    # THE UNLOCKED ARM IS FORCED BY DEFAULT, so the detector control stops
    # depending on the scheduler. `force=False` exists for the one caller that
    # wants the old hope-shaped behaviour on purpose (a differential row).
    force = unlocked if force is None else force
    ctx = multiprocessing.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="gr2-event-stress-") as tmp:
        workspace = Path(tmp)
        (workspace / ".grip").mkdir()
        start = ctx.Event()
        queue = ctx.Queue()
        # ONLY THE UNLOCKED ARM GETS A BARRIER, and that is not an optimisation:
        # in the LOCKED arm the write lock serialises the writers, so a barrier
        # between their reads would deadlock -- each would hold the lock while
        # waiting for a peer that cannot acquire it.
        rendezvous = ctx.Barrier(writers) if force else None
        # THE ARRIVAL COUNT IS THE OBSERVATION THE FLAG USED TO LACK. A counter in
        # shared memory, incremented inside the wrapper, is evidence that a
        # rendezvous actually happened in this process set -- where `force` was
        # only ever the argument the caller passed.
        arrivals = ctx.Value("i", 0) if force else None
        processes = [
            ctx.Process(
                target=_worker,
                args=(
                    str(workspace),
                    f"writer:{index}",
                    start,
                    queue,
                    unlocked,
                    str(SOURCE_ROOT),
                    rendezvous,
                    arrivals,
                ),
            )
            for index in range(writers)
        ]
        for process in processes:
            process.start()
        start.set()
        # THE BOUNDS ARE ONE DEADLINE, AND EVERY FAILURE IS NAMED.
        #
        # Before this, the join gave up at 15 s and the queue read at 2 s, so a
        # worker still running or still waiting read as a round that produced no
        # outcome -- and `queue.Empty` is an ERROR, not an assertion, so the row
        # reported as BROKEN rather than failed and the diagnosis cost a
        # re-derivation. The join now outlasts the rendezvous, so a worker that
        # times out at the barrier gets to put its own NAMED outcome on the queue
        # before the join gives up; and a missing outcome is named with whether
        # its worker was still alive, which is the distinction between SLOW and
        # HUNG that this file did not previously make.
        for process in processes:
            process.join(timeout=JOIN_TIMEOUT)
        outcomes: list[dict[str, object]] = []
        for process in processes:
            try:
                outcomes.append(queue.get(timeout=OUTCOME_TIMEOUT))
            except QueueEmpty:
                outcomes.append(
                    {
                        "ok": False,
                        "error": (
                            f"no outcome within {OUTCOME_TIMEOUT}s "
                            f"(pid={process.pid}, still alive={process.is_alive()})"
                        ),
                    }
                )
        rows, corruption_count = _read_rows(workspace / ".grip" / "events" / "outbox.jsonl")
        seqs = [row.get("seq") for row in rows]
        return {
            "all_workers_succeeded": all(outcome["ok"] for outcome in outcomes),
            "lost_events": len(rows) != writers,
            "duplicate_seq": len(seqs) != len(set(seqs)),
            "corruption_count": corruption_count,
            "forced": force,
            # OBSERVED, not requested: how many writers actually reached the
            # rendezvous. `forced` alone is the argument.
            "rendezvous_arrivals": int(arrivals.value) if arrivals is not None else 0,
            "writers": writers,
            # The CAUSE CHAINS of any worker that failed, so a row can assert on
            # the real failure rather than on the wrapper `emit` puts around it.
            "worker_errors": [
                str(outcome.get("error", "")) for outcome in outcomes if not outcome["ok"]
            ],
        }


def run_phase(*, rounds: int, writers: int, unlocked: bool) -> dict[str, object]:
    results = [_run_round(writers, unlocked) for _ in range(rounds)]
    return {
        "locking": "disabled" if unlocked else "enabled",
        "rounds": rounds,
        "writers": writers,
        "duplicate_seq_rounds": sum(bool(row["duplicate_seq"]) for row in results),
        "lost_event_rounds": sum(bool(row["lost_events"]) for row in results),
        "corruption_count": sum(int(row["corruption_count"]) for row in results),
        "worker_failure_rounds": sum(not bool(row["all_workers_succeeded"]) for row in results),
        # Reported so a row can assert the harness actually FORCED the window
        # rather than merely describing it. All of them, because a round that
        # silently fell back to hope is the flaky shape this exists to remove.
        # MEASURED, in both halves of the conjunction. `forced` says the caller
        # ASKED for the rendezvous; `rendezvous_arrivals == writers` says every
        # writer actually REACHED it in that round. Only the pair is evidence, and
        # the flag used to carry the first half alone -- so a stub that spawned no
        # writer reported the window as forced.
        "rendezvous_arrivals": sum(int(row["rendezvous_arrivals"]) for row in results),
        "worker_errors": [err for row in results for err in row["worker_errors"]],
        "forced_interleaving": bool(results)
        and all(
            bool(row["forced"]) and int(row["rendezvous_arrivals"]) == int(row["writers"])
            for row in results
        ),
    }


def sequential_control(writes: int = 8) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="gr2-event-sequential-control-") as tmp:
        workspace = Path(tmp)
        (workspace / ".grip").mkdir()
        old_disabled = os.environ.get("GR2_DISABLE_EVENT_LOCKING")
        old_delay = os.environ.get("GR2_EVENT_TEST_DELAY")
        os.environ["GR2_DISABLE_EVENT_LOCKING"] = "1"
        os.environ["GR2_EVENT_TEST_DELAY"] = "0"
        try:
            for index in range(writes):
                emit(
                    event_type=EventType.LANE_ENTERED,
                    workspace_root=workspace,
                    actor="sequential-control",
                    owner_unit="event-stress",
                    payload={"index": index},
                )
        finally:
            if old_disabled is None:
                os.environ.pop("GR2_DISABLE_EVENT_LOCKING", None)
            else:
                os.environ["GR2_DISABLE_EVENT_LOCKING"] = old_disabled
            if old_delay is None:
                os.environ.pop("GR2_EVENT_TEST_DELAY", None)
            else:
                os.environ["GR2_EVENT_TEST_DELAY"] = old_delay
        rows, corruption_count = _read_rows(workspace / ".grip" / "events" / "outbox.jsonl")
        seqs = [row.get("seq") for row in rows]
        return {
            "writes": writes,
            "strictly_monotonic": seqs == list(range(1, writes + 1)),
            "corruption_count": corruption_count,
        }


def main() -> int:
    args = parse_args()
    payload = {
        "corruption_detector_control": prove_corruption_detector(),
        "sequential_control": sequential_control(),
        "before_locking": run_phase(rounds=args.rounds, writers=args.writers, unlocked=True),
        "after_locking": run_phase(rounds=args.rounds, writers=args.writers, unlocked=False),
    }
    print(json.dumps(payload, indent=2))
    after = payload["after_locking"]
    return int(
        not payload["corruption_detector_control"]["proven"]
        or not payload["sequential_control"]["strictly_monotonic"]
        or after["duplicate_seq_rounds"] != 0
        or after["lost_event_rounds"] != 0
        or after["corruption_count"] != 0
        or after["worker_failure_rounds"] != 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
