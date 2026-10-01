"""Witnesses for the event outbox's torn-line contract and its unreadable-line count.

Fix 4 of the torn-line sweep. Two parts, and they are separate claims:

  1. TERMINATOR REPAIR -- an append onto a file whose last line lost its "\n"
     must not GLUE two records together. The damage runs FORWARD from the tear:
     the torn record and THE NEXT HEALTHY APPEND fuse into one unparseable
     line, while the record before the tear is untouched. So a torn write costs
     that record and the next one written after it, permanently, because every
     later append builds on the glued line.

  2. THE COUNT -- a line the reader cannot use is a LOST EVENT, and for the
     channel bridge it is a message that never reaches a channel. It is
     reported on EVERY read, not once: the cursor filter applies only to lines
     that PARSE, so a line with no usable seq can never be advanced past,
     wherever it sits -- position is irrelevant, and mid-file lines repeat
     exactly as trailing ones do. It is reported from read_events_detailed()
     only: _current_seq() runs once per emit inside the write lock, where a
     count would be per-APPEND and aimed at whoever happened to be writing.

Both statements above were WRONG in an earlier version of this file, in the
same words, while every test here passed. Prose is not covered by the suite
unless something pins the behavior it describes, so the witnesses at the end of
this file exist to pin exactly these two claims: which record the glue destroys,
and how long an unreadable line keeps being reported.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from gr2.python_cli.channel_bridge import run_bridge
from gr2.python_cli.events import (
    EventType,
    _current_seq,
    _outbox_path,
    emit,
    read_events,
    ack_events,
    read_events_detailed,
    warn_unreadable,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / ".grip" / "events").mkdir(parents=True)
    return tmp_path


def _emit(workspace: Path, **payload) -> None:
    emit(
        EventType.PROPAGATION_RECEIPT,
        workspace,
        actor="witness",
        owner_unit="unit",
        payload=payload or {"note": "n"},
    )


def _lines(workspace: Path) -> list[bytes]:
    raw = _outbox_path(workspace).read_bytes()
    return [ln for ln in raw.split(b"\n") if ln.strip()]


# --- W1: terminator repair --------------------------------------------------

def test_append_onto_torn_line_does_not_glue_records(workspace: Path):
    _emit(workspace, note="first")
    outbox = _outbox_path(workspace)
    # Tear the file exactly as a process killed mid-write leaves it.
    blob = outbox.read_bytes()
    assert blob.endswith(b"\n")
    outbox.write_bytes(blob[:-1])

    _emit(workspace, note="second")

    lines = _lines(workspace)
    assert len(lines) == 2, "the append glued two records into one line"
    for ln in lines:
        json.loads(ln)  # both must still parse


def test_torn_line_repair_survives_a_second_tear(workspace: Path):
    """One repair is not a fix if the next tear re-breaks it."""
    _emit(workspace, note="a")
    outbox = _outbox_path(workspace)
    outbox.write_bytes(outbox.read_bytes()[:-1])
    _emit(workspace, note="b")
    outbox.write_bytes(outbox.read_bytes()[:-1])
    _emit(workspace, note="c")

    lines = _lines(workspace)
    assert len(lines) == 3
    notes = [json.loads(ln)["note"] for ln in lines]
    assert notes == ["a", "b", "c"]


def test_no_spurious_terminator_on_a_fresh_outbox(workspace: Path):
    """The repair must not write a leading blank line into an empty file."""
    _emit(workspace, note="only")
    assert _outbox_path(workspace).read_bytes().count(b"\n") == 1


def test_torn_line_does_not_lose_the_earlier_event_to_a_reader(workspace: Path):
    """The point of the repair, stated as the consumer sees it."""
    _emit(workspace, note="earlier")
    outbox = _outbox_path(workspace)
    outbox.write_bytes(outbox.read_bytes()[:-1])
    _emit(workspace, note="later")

    read = read_events_detailed(workspace, "c")
    notes = [e.get("note") for e in read.events]
    assert notes == ["earlier", "later"]
    assert read.malformed == ()


# --- W2: undecodable bytes --------------------------------------------------

def test_invalid_utf8_does_not_brick_emit(workspace: Path):
    """A single bad byte must not make the outbox permanently unwritable.

    _current_seq() runs inside emit(); if it raises, EVERY future emit fails.
    That is worse than a crash -- it is a permanent denial with no recovery
    path short of deleting the file.
    """
    outbox = _outbox_path(workspace)
    outbox.write_bytes(b'{"seq":1,"note":"ok"}\n\xff\xfe not utf-8\n')
    _emit(workspace, note="after")  # must not raise
    assert any(b"after" in ln for ln in _lines(workspace))


def test_invalid_utf8_is_counted_not_raised(workspace: Path):
    outbox = _outbox_path(workspace)
    outbox.write_bytes(b'{"seq":1,"note":"ok"}\n\xff\xfe\n{"seq":2,"note":"two"}\n')
    read = read_events_detailed(workspace, "c")
    assert [e["note"] for e in read.events] == ["ok", "two"]
    assert len(read.malformed) == 1
    assert "utf-8" in read.malformed[0].reason.lower()


def test_seq_allocation_survives_an_undecodable_line(workspace: Path):
    outbox = _outbox_path(workspace)
    outbox.write_bytes(b'{"seq":7,"note":"ok"}\n\xff\n')
    assert _current_seq(outbox) == 7


# --- W3: hostile but syntactically valid content ----------------------------

@pytest.mark.parametrize(
    "raw,why",
    [
        (b'{"seq":1e999,"note":"inf"}', "float seq becomes inf and serializes as Infinity"),
        (b'{"seq":"1","note":"str"}', "string seq cannot be ordered against the cursor"),
        (b'{"seq":true,"note":"bool"}', "bool is an int subclass and max() accepts it silently"),
        (b'{"seq":null,"note":"none"}', "null seq"),
        (b'[1,2,3]', "a JSON array is not an event"),
        (b'"just a string"', "a JSON string is not an event"),
        (b'{"seq":1,', "truncated object"),
        (b'not json at all', "not JSON"),
    ],
)
def test_hostile_line_is_counted_and_never_raises(workspace: Path, raw: bytes, why: str):
    outbox = _outbox_path(workspace)
    outbox.write_bytes(b'{"seq":1,"note":"good"}\n' + raw + b"\n")
    read = read_events_detailed(workspace, "c")
    assert [e["note"] for e in read.events] == ["good"], why
    assert len(read.malformed) == 1, why
    assert _current_seq(outbox) == 1, why


def test_deeply_nested_json_is_counted_not_raised(workspace: Path):
    """RecursionError is raised by json.loads and is NOT a ValueError.

    The previous guard caught (JSONDecodeError, TypeError) and would have let
    this escape -- a denylist over untrusted input leaks by construction.
    """
    outbox = _outbox_path(workspace)
    bomb = b"[" * 20000 + b"]" * 20000
    outbox.write_bytes(b'{"seq":1,"note":"good"}\n' + bomb + b"\n")
    read = read_events_detailed(workspace, "c")
    assert [e["note"] for e in read.events] == ["good"]
    assert len(read.malformed) == 1


def test_emit_still_allocates_a_usable_seq_beside_hostile_lines(workspace: Path):
    outbox = _outbox_path(workspace)
    outbox.write_bytes(b'{"seq":3,"note":"good"}\n{"seq":1e999,"note":"inf"}\n')
    _emit(workspace, note="next")
    last = json.loads(_lines(workspace)[-1])
    assert last["seq"] == 4, "an unusable seq must not poison allocation"
    json.dumps(last)  # must remain serializable -- inf would emit `Infinity`


# --- W4: the count reaches a consumer ---------------------------------------

def test_warn_unreadable_writes_to_the_given_stream(workspace: Path):
    outbox = _outbox_path(workspace)
    outbox.write_bytes(b'{"seq":1,"note":"ok"}\n\xff\n')
    read = read_events_detailed(workspace, "c")
    buf = io.StringIO()
    assert warn_unreadable(read, stream=buf) is True
    assert "skipped 1 unreadable line" in buf.getvalue()


def test_warn_unreadable_is_silent_when_clean(workspace: Path):
    """The negative case. Without it, a reporter that always warns passes."""
    _emit(workspace, note="fine")
    read = read_events_detailed(workspace, "c")
    buf = io.StringIO()
    assert warn_unreadable(read, stream=buf) is False
    assert buf.getvalue() == ""


def test_bridge_reports_on_stderr_and_still_posts_good_events(workspace: Path, capsys):
    outbox = _outbox_path(workspace)
    outbox.write_bytes(
        b'{"seq":1,"type":"propagation.receipt","note":"ok"}\n\xff\n'
    )
    posted: list[str] = []
    count = run_bridge(workspace, post_fn=posted.append)
    captured = capsys.readouterr()
    assert "unreadable" in captured.err, "the count never reached a consumer"
    assert captured.out == "", "a health warning on stdout breaks --json callers"
    assert count == len(posted)


def test_bridge_is_silent_on_a_clean_outbox(workspace: Path, capsys):
    _emit(workspace, note="clean")
    run_bridge(workspace, post_fn=lambda _m: None)
    assert "unreadable" not in capsys.readouterr().err


# --- W5: the cursor is untrusted input too ----------------------------------

def test_a_corrupt_cursor_does_not_brick_reads(workspace: Path):
    """The cursor is a file on disk; a truncated write makes last_seq a non-int.

    Comparing seq against it would raise from inside a reader whose contract is
    to not raise on file content.
    """
    _emit(workspace, note="one")
    cursors = workspace / ".grip" / "events" / "cursors"
    cursors.mkdir(parents=True, exist_ok=True)
    (cursors / "c.json").write_text(json.dumps({"consumer": "c", "last_seq": "not-an-int"}))
    read = read_events_detailed(workspace, "c")
    assert [e["note"] for e in read.events] == ["one"]


# --- the wrapper keeps its shape -------------------------------------------

def test_read_events_wrapper_still_returns_a_plain_list(workspace: Path):
    """Eleven call sites index and len() this. The shape is the contract.

    All of them are tests: no production caller of read_events() remains once the
    bridge moves to read_events_detailed(). The wrapper is a test-compatibility
    surface, not a load-bearing API.
    """
    _emit(workspace, note="a")
    events = read_events(workspace, "c")
    assert isinstance(events, list)
    assert len(events) == 1
    assert events[0]["note"] == "a"


# --- W6: an I/O failure is NOT a malformed line -----------------------------
#
# This boundary was NOT found by any witness above. It was found by an existing
# test in test_events.py whose docstring stands guard over it, after the first
# version of this fix swallowed OSError inside the line iterator. Content errors
# are data and get skipped-and-counted; an I/O error is not knowing what the file
# holds, and on the WRITE path that becomes a duplicate sequence number.

def test_emit_fails_closed_when_the_outbox_cannot_be_read(workspace: Path, monkeypatch):
    """Swallowing this returns seq 0, and emit then reuses live sequence numbers."""
    from gr2.python_cli.events import EventEmitError

    outbox = _outbox_path(workspace)
    outbox.write_bytes(b'{"seq":41}\n')
    original = Path.read_bytes

    def boom(path: Path, *a, **k):
        if path == outbox:
            raise OSError("forced sequence-read failure")
        return original(path, *a, **k)

    monkeypatch.setattr(Path, "read_bytes", boom)
    with pytest.raises(EventEmitError) as exc:
        _emit(workspace, note="must not land")
    assert isinstance(exc.value.__cause__, OSError)
    # The read-back must not travel the sabotaged path -- my first version of
    # this witness verified the file using the very method it had broken, and
    # failed for that reason rather than for anything about emit().
    monkeypatch.undo()
    assert outbox.read_bytes() == b'{"seq":41}\n', "a failed emit must not mutate the log"


def test_reader_tolerates_the_outbox_vanishing_mid_read(workspace: Path, monkeypatch):
    """_maybe_rotate() renames the outbox, so this race is real and benign."""
    outbox = _outbox_path(workspace)
    _emit(workspace, note="a")
    original = Path.read_bytes

    def vanish(path: Path, *a, **k):
        if path == outbox:
            raise FileNotFoundError("rotated away")
        return original(path, *a, **k)

    monkeypatch.setattr(Path, "read_bytes", vanish)
    read = read_events_detailed(workspace, "c")
    assert read.events == []
    assert read.malformed == ()


def test_reader_propagates_a_real_io_error(workspace: Path, monkeypatch):
    """The discriminating control for the case above.

    Without this, catching FileNotFoundError could widen to OSError and a reader
    would report 'no new events' forever while the disk was failing.
    """
    outbox = _outbox_path(workspace)
    _emit(workspace, note="a")
    original = Path.read_bytes

    def eio(path: Path, *a, **k):
        if path == outbox:
            raise OSError("EIO")
        return original(path, *a, **k)

    monkeypatch.setattr(Path, "read_bytes", eio)
    with pytest.raises(OSError):
        read_events_detailed(workspace, "c")


# --- W7: the report must not amplify file content into logs -----------------

def test_default_report_does_not_echo_line_content(workspace: Path):
    """stderr gets copied -- into CI logs, scrollback, pasted transcripts.

    Found by asking what this new output path could carry, not by a failing
    test. `reason` is structural; the excerpt is content, and it is available on
    the data object for callers that genuinely need it.
    """
    outbox = _outbox_path(workspace)
    outbox.write_bytes(b'{"seq":1,"note":"ok"}\n{"token":"sk-NOTREAL-abcdef","broken\n')
    read = read_events_detailed(workspace, "c")
    buf = io.StringIO()
    warn_unreadable(read, stream=buf)
    assert "NOTREAL" not in buf.getvalue()
    assert "line 2" in buf.getvalue(), "the line must still be identified"
    assert "NOTREAL" in read.malformed[0].excerpt, "fidelity kept on the data object"


def test_show_content_opt_in_still_works(workspace: Path):
    """The discriminating control: without it, an always-redacting reporter passes."""
    outbox = _outbox_path(workspace)
    outbox.write_bytes(b'{"seq":1,"note":"ok"}\n{"token":"sk-NOTREAL-abcdef","broken\n')
    read = read_events_detailed(workspace, "c")
    buf = io.StringIO()
    warn_unreadable(read, stream=buf, show_content=True)
    assert "NOTREAL" in buf.getvalue()


# --- W8: this module must import WITHOUT sys.modules registration -----------

def test_events_module_loads_under_an_out_of_tree_loader(tmp_path: Path):
    """Spawned workers load events.py by path, without registering it.

    importlib.util.spec_from_file_location() + exec_module() leaves the module
    OUT of sys.modules, and @dataclass resolves its field types through
    sys.modules[cls.__module__].__dict__ -- so a dataclass here raises
    AttributeError AT IMPORT and every worker dies before running its own code.

    That is not hypothetical: adding @dataclass to this module killed both
    writers in the concurrent-emit integrity test before either reached
    sequence allocation. This witness exists so the next person to reach for a
    dataclass here finds out in one second instead of in a concurrency test
    whose failure looks like flakiness.
    """
    import importlib.util

    events_path = Path(__file__).resolve().parents[1] / "python_cli" / "events.py"
    spec = importlib.util.spec_from_file_location("events_out_of_tree_probe", events_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # DELIBERATELY NOT registered in sys.modules -- that is the whole point.
    spec.loader.exec_module(module)

    assert hasattr(module, "read_events_detailed")
    assert module.MalformedLine(1, "why", "x").ordinal == 1
    assert module.EventRead([], ()).malformed == ()


# --- W9: how long an unreadable line keeps being reported --------------------
#
# These exist because a REVIEWER measured this and the PR body claimed the
# opposite, with the whole suite green. Nothing pinned the real behavior, so
# prose and code were free to disagree. The reviewer found the TERMINAL case;
# measuring the mid-file case showed the same thing, so the rule is simpler and
# broader than the finding that produced it.

def test_unreadable_line_is_reported_on_every_read_terminal(workspace: Path):
    """A trailing unreadable line is counted again on the next read."""
    _emit(workspace, note="good")
    outbox = _outbox_path(workspace)
    outbox.write_bytes(outbox.read_bytes() + b"\xff not utf-8\n")

    first = read_events_detailed(workspace, "c")
    ack_events(workspace, "c", first.events)  # reading alone consumes nothing
    second = read_events_detailed(workspace, "c")
    assert [e["note"] for e in first.events] == ["good"]
    assert len(first.malformed) == 1
    assert second.events == [], "the good event was acknowledged, as expected"
    assert len(second.malformed) == 1, "the unreadable line is reported again"


def test_unreadable_line_is_reported_on_every_read_midfile(workspace: Path):
    """MID-FILE too, which is the part the terminal framing would hide.

    The cursor filter `seq <= last_seq` only applies to lines that PARSE. A line
    with no usable seq can never be advanced past, wherever it sits -- so
    position is irrelevant and "trailing" is not the distinguishing property.
    """
    outbox = _outbox_path(workspace)
    outbox.write_bytes(b'{"seq":1,"note":"a"}\n\xff\n{"seq":2,"note":"b"}\n')

    first = read_events_detailed(workspace, "c")
    ack_events(workspace, "c", first.events)
    second = read_events_detailed(workspace, "c")
    assert [e["note"] for e in first.events] == ["a", "b"]
    assert len(first.malformed) == 1
    assert second.events == [], "both good events were acknowledged"
    assert len(second.malformed) == 1, "a mid-file unreadable line repeats too"


def test_glue_destroys_the_next_append_not_the_preceding_record(workspace: Path):
    """The measured direction of the damage, which the body stated backwards.

    Tear after record B, then append C WITHOUT the repair. A is untouched; B and
    C fuse into one unparseable line. The damage runs FORWARD from the tear.
    """
    _emit(workspace, note="A")
    _emit(workspace, note="B")
    outbox = _outbox_path(workspace)
    outbox.write_bytes(outbox.read_bytes()[:-1])  # tear after B
    # bypass emit() to reproduce the pre-fix append
    outbox.write_bytes(
        outbox.read_bytes() + json.dumps({"seq": 3, "note": "C"}).encode("utf-8") + b"\n"
    )

    lines = _lines(workspace)
    assert len(lines) == 2, "B and C fused into one line"
    assert json.loads(lines[0])["note"] == "A", "the record BEFORE the tear survives"
    with pytest.raises(ValueError):
        json.loads(lines[1])  # B+C, unparseable


# --- W10: the TRUNCATED tear, which no fixture ABOVE this line exercises -----
#
# "TORN" IS TWO DIFFERENT FAILURES WITH OPPOSITE OUTCOMES, AND USING THE BARE
# WORD IS WHAT MADE FOUR SEPARATE DESCRIPTIONS OF THIS CODE WRONG:
#
#   UNTERMINATED -- the record is COMPLETE and only its "\n" was lost. Seam
#     repair ends the line and it parses. RECOVERS FULLY, zero malformed.
#   TRUNCATED -- the write stopped mid-record. Seam repair stops the next append
#     being glued on, and the record itself NEVER becomes readable, because its
#     bytes were never written.
#
# Every terminator fixture ABOVE this line uses read_bytes()[:-1] -- the
# UNTERMINATED case, and the lucky one. The fixtures below are the truncated
# case. That gap is why prose describing this code kept being wrong: the only
# tear ever exercised was the one that recovers.
#
# Measured: unterminated -> 3 events, 0 malformed. Truncated -> 2 events, 1
# malformed, and still 1 on the second and third read.
#
# What the repair actually buys, stated so it holds for BOTH cases: it cannot
# recover a record that was never fully written, and it saves the NEXT one.
# Without repair you lose two records; with it you lose the one the crash
# truncated -- and if the crash truncated nothing, you lose none.

def _tear_mid_record(outbox: Path) -> None:
    """Truncate the last record mid-JSON, as a partial write leaves it."""
    lines = outbox.read_bytes().split(b"\n")
    body = [ln for ln in lines if ln.strip()]
    body[-1] = body[-1][:20]
    outbox.write_bytes(b"\n".join(body))  # no trailing newline either


def test_realistic_tear_saves_the_next_append_but_not_the_torn_record(workspace: Path):
    _emit(workspace, note="A")
    _emit(workspace, note="B")
    outbox = _outbox_path(workspace)
    _tear_mid_record(outbox)

    _emit(workspace, note="C")

    read = read_events_detailed(workspace, "c")
    notes = [e.get("note") for e in read.events]
    assert notes == ["A", "C"], "the NEXT append survives; the truncated record cannot"
    assert len(read.malformed) == 1, "the truncated record is unreadable, permanently"


def test_a_truncated_record_never_heals(workspace: Path):
    """A TRUNCATED record is reported on every read, forever; no emit repairs it.

    THE SEAM HEALS; A TRUNCATED RECORD DOES NOT. The next append is no longer
    glued on, which is the whole of what the repair achieves for this case --
    the truncated record's bytes were never written and nothing can reconstruct
    them. An UNTERMINATED record is the other case entirely and does recover;
    its witness is directly below and the two are each other's control.
    """
    _emit(workspace, note="A")
    _emit(workspace, note="B")
    outbox = _outbox_path(workspace)
    _tear_mid_record(outbox)
    _emit(workspace, note="C")

    counts = [len(read_events_detailed(workspace, "c").malformed) for _ in range(3)]
    assert counts == [1, 1, 1], f"expected a permanent malformed line, got {counts}"


def test_unterminated_tear_DOES_fully_recover(workspace: Path):
    """The discriminating control, and it has already earned its keep twice.

    Without it the two witnesses above would also pass against an implementation
    that simply never recovers anything, and the truncated case would read as the
    norm. It then caught its own author: a draft of the PR body claimed a torn
    record never becomes readable, and THIS witness disproves that. An
    UNTERMINATED record -- complete, only its newline lost -- must come back with
    zero malformed lines.
    """
    _emit(workspace, note="A")
    _emit(workspace, note="B")
    outbox = _outbox_path(workspace)
    outbox.write_bytes(outbox.read_bytes()[:-1])  # newline only

    _emit(workspace, note="C")

    read = read_events_detailed(workspace, "c")
    assert [e.get("note") for e in read.events] == ["A", "B", "C"]
    assert read.malformed == ()


# --- W11: the READER against a torn file, with NO intervening emit ----------
#
# Every tear fixture above emits after tearing, so until this pair nothing ever
# asked what the READER alone does with a torn file. That gap is exactly how a
# false sentence survived into the bridge comment: it said an unterminated
# record is reported and then heals at the next emit, implying a window in
# which it is unreadable. There is no such window. _iter_outbox() splits on
# b"\n", so a COMPLETE record whose only loss was its terminator is simply the
# final chunk, and it parses -- repair or no repair, emit or no emit.
#
# The truncated case below is the discriminating control: without it this pair
# would also pass against a reader that never reported anything at all.


def test_unterminated_record_is_never_reported_even_without_an_emit(workspace: Path):
    """A complete record missing only its "\n" is readable immediately.

    Not "recovers at the next emit" -- never unreadable in the first place.
    Read twice, because a claim of transience would show as a difference
    between the reads.
    """
    _emit(workspace, note="A")
    _emit(workspace, note="B")
    outbox = _outbox_path(workspace)
    outbox.write_bytes(outbox.read_bytes()[:-1])  # newline only; no emit follows

    for i in (1, 2):
        read = read_events_detailed(workspace, f"c{i}")
        assert [e.get("note") for e in read.events] == ["A", "B"], f"read {i}"
        assert read.malformed == (), f"read {i}: expected nothing reported, got {read.malformed}"


def test_truncated_record_IS_reported_without_an_emit(workspace: Path):
    """The discriminating control for the witness above.

    Byte-identical setup except the tear cuts mid-record instead of at the
    terminator. If this one also came back empty, the witness above would be
    measuring a reader that reports nothing rather than a record that is
    readable.
    """
    _emit(workspace, note="A")
    _emit(workspace, note="B")
    outbox = _outbox_path(workspace)
    _tear_mid_record(outbox)  # no emit follows

    for i in (1, 2):
        read = read_events_detailed(workspace, f"c{i}")
        assert [e.get("note") for e in read.events] == ["A"], f"read {i}"
        assert len(read.malformed) == 1, f"read {i}: expected the truncated record reported"
