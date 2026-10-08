"""Integrity tests for concurrent event writes and prototype evidence."""

from __future__ import annotations

import importlib.util
import json
import multiprocessing
import sys
import tempfile
import time
from argparse import Namespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _gated_emit_worker(
    source_root: str,
    workspace_root: str,
    read_reached: object,
    release_read: object,
    result_queue: object,
) -> None:
    """Pause each process after reading seq so an unlocked RMW races deterministically."""
    events_path = Path(source_root) / "gr2" / "gr2" / "python_cli" / "events.py"
    spec = importlib.util.spec_from_file_location("event_integrity_worker_events", events_path)
    assert spec is not None and spec.loader is not None
    events = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(events)

    original_current_seq = events._current_seq

    def gated_current_seq(outbox: Path) -> int:
        seq = original_current_seq(outbox)
        read_reached.set()
        if not release_read.wait(timeout=10):
            raise TimeoutError("parent did not release the sequence-read gate")
        return seq

    events._current_seq = gated_current_seq
    try:
        events.emit(
            event_type=events.EventType.LANE_ENTERED,
            workspace_root=Path(workspace_root),
            actor=f"worker:{multiprocessing.current_process().name}",
            owner_unit="event-stress",
            payload={"lane_name": "integrity"},
        )
    except Exception as exc:  # pragma: no cover - reported across process boundary
        result_queue.put({"ok": False, "error": repr(exc)})
    else:
        result_queue.put({"ok": True})


def _wait_until_any(events: list[object], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(event.is_set() for event in events):
            return True
        time.sleep(0.005)
    return False


def test_concurrent_emit_serializes_sequence_read_and_append(tmp_path: Path) -> None:
    """Removing the write lock lets both processes allocate seq=1."""
    (tmp_path / ".grip").mkdir()
    ctx = multiprocessing.get_context("spawn")
    reached = [ctx.Event(), ctx.Event()]
    release = ctx.Event()
    results = ctx.Queue()
    processes = [
        ctx.Process(
            target=_gated_emit_worker,
            args=(
                str(Path(__file__).resolve().parents[2]),
                str(tmp_path),
                reached[index],
                release,
                results,
            ),
            name=f"emit-{index}",
        )
        for index in range(2)
    ]

    for process in processes:
        process.start()

    assert _wait_until_any(reached, timeout=10), "neither writer reached sequence allocation"
    time.sleep(0.25)
    both_read_before_release = all(event.is_set() for event in reached)
    release.set()

    for process in processes:
        process.join(timeout=10)
        assert not process.is_alive(), "event writer hung"
        assert process.exitcode == 0

    outcomes = [results.get(timeout=2) for _ in processes]
    assert outcomes == [{"ok": True}, {"ok": True}]
    assert not both_read_before_release, "sequence allocation was not serialized"

    outbox = tmp_path / ".grip" / "events" / "outbox.jsonl"
    rows = [json.loads(line) for line in outbox.read_text().splitlines()]
    assert sorted(row["seq"] for row in rows) == [1, 2]


def test_sequential_emit_control_is_strictly_monotonic(tmp_path: Path) -> None:
    """The zero-concurrency control proves the sequence detector can report clean fruit."""
    from gr2.python_cli.events import EventType, emit

    (tmp_path / ".grip").mkdir()
    for index in range(8):
        emit(
            event_type=EventType.LANE_ENTERED,
            workspace_root=tmp_path,
            actor="sequential-control",
            owner_unit="event-stress",
            payload={"index": index},
        )

    outbox = tmp_path / ".grip" / "events" / "outbox.jsonl"
    rows = [json.loads(line) for line in outbox.read_text().splitlines()]
    assert [row["seq"] for row in rows] == list(range(1, 9))


def test_repeated_stress_distinguishes_unlocked_and_locked_paths() -> None:
    """Repeated fruit must expose the old race while the locked path stays clean."""
    from gr2.prototypes.concurrent_event_stress import run_phase, sequential_control

    control = sequential_control()
    unlocked = run_phase(rounds=3, writers=2, unlocked=True)
    locked = run_phase(rounds=3, writers=2, unlocked=False)

    assert control == {
        "writes": 8,
        "strictly_monotonic": True,
        "corruption_count": 0,
    }
    # THE INTERLEAVING IS FORCED, so the count below is CERTAIN rather than
    # hoped for. Asserted first and asserted at all, because "the harness says it
    # forces the window" is the claim the rest of the row rests on: with the
    # rendezvous removed, `duplicate_seq_rounds` still reads 3 on a fast host and
    # reads less under load, which is the shape this change exists to delete.
    assert unlocked["forced_interleaving"] is True
    # AND THE FLAG IS NOT ENOUGH ON ITS OWN. It used to be the caller's ARGUMENT
    # (`_run_round` returned `"forced": force`), so a stub that spawned no writer
    # at all still reported the window as forced -- a reader demonstrated exactly
    # that before this assertion existed. The count is the OBSERVATION: every
    # writer, in every round, actually reached the rendezvous.
    assert unlocked["rendezvous_arrivals"] == 6, (
        f"3 rounds x 2 writers must each reach the rendezvous; got "
        f"{unlocked['rendezvous_arrivals']} -- a count below 6 means some writer "
        f"never entered the forced window and the duplicate is not a property of "
        f"the fixture"
    )
    assert locked["forced_interleaving"] is False, (
        "the locked arm must NOT rendezvous: the write lock already serialises it, "
        "and a barrier there would deadlock"
    )
    assert unlocked["duplicate_seq_rounds"] == 3
    assert unlocked["worker_failure_rounds"] == 0, (
        "every writer must reach the rendezvous; a failure there arrives as "
        "EventEmitError with the real cause in its chain, which is why the harness "
        "records the chain rather than repr(exc)"
    )
    assert locked["duplicate_seq_rounds"] == 0
    assert locked["lost_event_rounds"] == 0
    assert locked["corruption_count"] == 0
    assert locked["worker_failure_rounds"] == 0


def test_emit_resolves_current_seq_at_call_time_so_a_fixture_can_force_the_window() -> None:
    """The SEAM the forced interleaving rests on, proved with ONE process.

    The harness forces the read/write window by wrapping `events._current_seq` on
    the module from inside each spawned worker. That works only if `emit` looks
    the name up AT CALL TIME. If `emit` had captured the function at import -- or
    called a local alias -- the wrapper would be dead code, and the forced row
    would go green while proving nothing about the race.

    This is deliberately a SINGLE-PROCESS, load-free row: it is the part of the
    work that can be measured without spawning writers, so the concurrent rows
    are the only thing a clean host or CI has to add. It is also the mutation
    target for the seam -- binding `_current_seq` at import in `emit` reddens
    this row and nothing else.
    """
    # ONE NAME. This row used to be written against the flat name `python_cli` because that
    # name and `gr2.python_cli` were two module objects over one file (the packaging map
    # mapped a flat directory onto a dotted name), each with its own globals, so a patch on
    # one was invisible to the other and a reader caught an earlier version proving the
    # property one name over. The map is retired and the directory is `gr2/python_cli`, so
    # there is one module object and the patch site is every caller's. This row's own
    # canary asked for the comment to be updated, not the row deleted: the spy below still
    # proves `emit` reads through the module-level `_current_seq`.
    from gr2.python_cli import events

    seen: list[Path] = []
    real = events._current_seq

    def _spy(outbox: Path) -> int:
        seen.append(outbox)
        return real(outbox)

    events._current_seq = _spy
    try:
        with tempfile.TemporaryDirectory(prefix="gr2-seq-seam-") as tmp:
            workspace = Path(tmp)
            (workspace / ".grip").mkdir()
            events.emit(
                event_type=events.EventType.LANE_ENTERED,
                workspace_root=workspace,
                actor="seam-probe",
                owner_unit="event-stress",
                payload={},
            )
    finally:
        events._current_seq = real

    assert seen, (
        "emit did not call the module-level `_current_seq`; a wrapper installed by "
        "the harness would be dead code and the forced row would be proving nothing"
    )
    assert len(seen) == 1, f"emit should read the sequence exactly once, got {len(seen)}"


def test_emit_wraps_a_read_failure_so_the_cause_chain_is_the_diagnosis(tmp_path: Path) -> None:
    """WHY THE RECORD CARRIES A CHAIN: `emit` wraps whatever the read raises.

    A barrier timeout does NOT arrive at the worker as `BrokenBarrierError`. It
    arrives as `EventEmitError("event emit failed for <path>")` with the real
    failure only in `__cause__` -- so a record built from `repr(exc)` names the
    WRAPPER. Three claims said otherwise until a reader ran it: the comment beside
    the timeout, the PR body, and an assertion message in the stress row.

    One process, no writers, no load.
    """
    from gr2.python_cli import events

    real = events._current_seq

    def _boom(outbox: Path) -> int:
        raise RuntimeError("barrier-probe")

    events._current_seq = _boom
    try:
        workspace = tmp_path / "wrap"
        (workspace / ".grip").mkdir(parents=True)
        with pytest.raises(BaseException) as caught:
            events.emit(
                event_type=events.EventType.LANE_ENTERED,
                workspace_root=workspace,
                actor="wrap-probe",
                owner_unit="event-stress",
                payload={},
            )
    finally:
        events._current_seq = real

    exc = caught.value
    assert type(exc).__name__ == "EventEmitError", (
        f"emit no longer wraps a read failure, so this row and the chain it "
        f"justifies are both stale -- got {type(exc).__name__}"
    )
    assert isinstance(exc.__cause__, RuntimeError) and not isinstance(exc, type(None)), (
        "the real failure must survive as __cause__; that is what makes the chain "
        "the diagnosis rather than repr(exc)"
    )
    assert "barrier-probe" in str(exc.__cause__)


def test_the_error_chain_names_the_cause_and_not_only_the_wrapper() -> None:
    """THE FIX ITSELF: the record reports the wrapper AND the cause under it."""
    from gr2.prototypes.concurrent_event_stress import _error_chain

    try:
        try:
            raise ValueError("inner-probe")
        except ValueError as inner:
            raise RuntimeError("outer-probe") from inner
    except RuntimeError as outer:
        chain = _error_chain(outer)

    assert chain.startswith("RuntimeError: outer-probe"), chain
    assert "ValueError: inner-probe" in chain, (
        f"the cause must appear in the record, or a barrier timeout reports as a "
        f"generic emit failure again; got {chain!r}"
    )


def test_spawned_worker_rejects_gr2_import_outside_expected_worktree(
    tmp_path: Path,
) -> None:
    """Removing the worker-side provenance guard turns this positive control RED."""
    from gr2.prototypes.concurrent_event_stress import _worker

    (tmp_path / ".grip").mkdir()
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    queue = ctx.Queue()
    process = ctx.Process(
        target=_worker,
        args=(
            str(tmp_path),
            "provenance-control",
            start,
            queue,
            False,
            str(tmp_path / "deliberately-wrong-worktree"),
        ),
    )
    process.start()
    start.set()
    process.join(timeout=10)

    assert not process.is_alive(), "provenance-control worker hung"
    assert process.exitcode == 0
    outcome = queue.get(timeout=2)
    assert outcome["ok"] is False
    assert "outside expected worktree" in outcome["error"]
    assert not (tmp_path / ".grip" / "events" / "outbox.jsonl").exists()


def test_cross_mode_child_failure_preserves_exit_and_output() -> None:
    """Captured child failure must be loud rather than indistinguishable from silence."""
    from gr2.prototypes.cross_mode_lane_stress import HarnessCommandError, run

    with pytest.raises(HarnessCommandError) as exc_info:
        run(
            [
                sys.executable,
                "-c",
                "import sys; print('child-out'); "
                "print('child-err', file=sys.stderr); raise SystemExit(7)",
            ],
            capture=True,
        )

    message = str(exc_info.value)
    assert "exit 7" in message
    assert "child-out" in message
    assert "child-err" in message


def test_lease_corruption_detector_has_a_positive_control() -> None:
    """A zero corruption count is proven only if known corruption is detected."""
    from gr2.prototypes.concurrent_lease_stress import prove_corruption_detector

    receipt = prove_corruption_detector()
    assert receipt == {
        "proven": True,
        "fixture": "malformed-json",
        "detected_as_corrupt": True,
    }


def test_lease_stress_report_carries_and_enforces_the_positive_control(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Deleting the control from the report or ignoring a failed control turns RED."""
    from gr2.prototypes import concurrent_lease_stress as stress

    monkeypatch.setattr(stress, "parse_args", lambda: Namespace(rounds=0, json=True))
    monkeypatch.setattr(
        stress,
        "run_phase",
        lambda disable_locking, rounds: {
            "locking": "disabled" if disable_locking else "enabled",
            "rounds": rounds,
        },
    )
    monkeypatch.setattr(
        stress,
        "prove_corruption_detector",
        lambda: {
            "proven": False,
            "fixture": "malformed-json",
            "detected_as_corrupt": False,
        },
    )

    assert stress.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["corruption_detector_control"]["proven"] is False


def test_event_corruption_detector_has_a_positive_control() -> None:
    """The event stress report must prove its own corruption counter."""
    from gr2.prototypes.concurrent_event_stress import prove_corruption_detector

    assert prove_corruption_detector() == {
        "proven": True,
        "fixture": "malformed-json",
        "detected_count": 1,
    }
