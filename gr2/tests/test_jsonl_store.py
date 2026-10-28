"""Witnesses for the shared torn-line-safe JSONL primitives.

Both lane prototypes carried a BYTE-IDENTICAL ``append_jsonl`` and a reader with
the same body under two names. This is one defect with two addresses, not a class
with two instances, so it has one fix, one home, and one witness set.

* W1  a writer killed mid-write leaves a remnant: the append point survives it,
      the NEXT record is not GLUED onto it, and the remnant is skipped AND COUNTED
* W2  hostile line CONTENT neither bricks nor vanishes (validate, never coerce)
* W3  undecodable BYTES neither brick nor vanish, and a bad byte is CONFINED to
      its own line rather than destroying every record around it
* W4  the count has a REAL CONSUMER: warn_unreadable reports it, because a count
      nobody surfaces is the same silence as no count at all
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest
from gr2.prototypes.jsonl_store import (
    MAX_JSON_DEPTH,
    JsonlRead,
    append_jsonl,
    read_jsonl,
    warn_unreadable,
)


def _tear(path: Path, remnant: bytes) -> None:
    """A writer killed between its record and its terminator: bytes, no newline."""
    with path.open("ab") as handle:
        handle.write(remnant)
        handle.flush()
        os.fsync(handle.fileno())


# --------------------------------------------------------------------------- W1


def test_w1_a_torn_remnant_does_not_brick_the_append_point(tmp_path: Path) -> None:
    path = tmp_path / "log" / "events.jsonl"
    append_jsonl(path, {"n": 1})
    _tear(path, b'{"n": 2, "part')

    append_jsonl(path, {"n": 3})  # must not raise
    append_jsonl(path, {"n": 4})  # and must not raise AGAIN: not a once-survivable event

    read = read_jsonl(path)
    assert [row["n"] for row in read.rows] == [1, 3, 4]
    assert len(read.malformed) == 1


def test_w1_the_next_record_is_not_glued_onto_the_remnant(tmp_path: Path) -> None:
    """Terminator repair. Without it the record after a torn write is swallowed."""
    path = tmp_path / "log" / "events.jsonl"
    append_jsonl(path, {"n": 1})
    _tear(path, b'{"n": 2, "part')

    append_jsonl(path, {"n": 3})

    lines = path.read_bytes().split(b"\n")
    assert lines[1] == b'{"n": 2, "part'  # the remnant, still on its own line
    assert json.loads(lines[2])["n"] == 3  # the new record survived WHOLE
    assert len(read_jsonl(path).rows) == 2


def test_w1_a_missing_file_reads_empty_and_reports_nothing(tmp_path: Path) -> None:
    read = read_jsonl(tmp_path / "never" / "written.jsonl")
    assert read == JsonlRead(rows=[], malformed=())


def test_w1_the_write_is_fsynced(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Pins that fsync is CALLED on the appended handle. Deliberately weaker than
    it sounds, and labelled so nobody reads more into it than it proves.

    It does NOT prove durability — that needs a real crash, which is unwitnessable
    in-process. What it does prove is that the call is present and reached, which
    is the difference between a guard and a guard-shaped comment: without it,
    deleting the fsync kills no test at all, and a guard nothing checks is not a
    guard. Durability against power loss remains ASSERTED, not demonstrated.
    """
    synced: list[int] = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real(fd))[1])

    path = tmp_path / "log" / "events.jsonl"
    append_jsonl(path, {"n": 1})
    append_jsonl(path, {"n": 2})

    assert len(synced) == 2, "each append must fsync its own write"


# --------------------------------------------------------------------------- W2

HOSTILE_LINES = {
    "deeply nested (json.loads raises RecursionError)": '{"n": '
    + "[" * 100000
    + "]" * 100000
    + "}",
    "integer literal past the conversion limit": '{"n": ' + "9" * 6000 + "}",
    "truncated object": '{"n": 1',
    "line is an array, not an object": "[1, 2, 3]",
    "line is a bare string": '"just a string"',
    "line is a bare number": "42",
}


# Deliberately NOT hostile here: this module validates STRUCTURE (is the line a
# JSON object?) and never SCHEMA (does it carry my fields?), because its rows are
# generic and the consumers own interpretation. Nothing coerces, so nothing raises.
# The sibling AppendSurface DOES have a record schema and rejects these — the
# difference is real and is pinned below rather than assumed.
STRUCTURALLY_VALID_BUT_ODD = {
    "float infinity": ('{"n": 1e999}', float("inf")),
    "not-a-number": ('{"n": NaN}', None),  # NaN != NaN, checked by isnan
}


@pytest.mark.parametrize("label", sorted(STRUCTURALLY_VALID_BUT_ODD))
def test_w2_structure_is_validated_but_schema_is_not(label: str, tmp_path: Path) -> None:
    """These ARE accepted, deliberately. A generic reader has no schema to check.

    Found by a witness failing against my own expectation rather than against the
    code: I copied a hostile-content table from a surface that HAS a record schema.
    The row is well-formed JSON and a well-formed object, so it is a row.
    """
    import math

    line, expected = STRUCTURALLY_VALID_BUT_ODD[label]
    path = tmp_path / "log" / "events.jsonl"
    append_jsonl(path, {"n": 1})
    with path.open("ab") as handle:
        handle.write(line.encode("utf-8") + b"\n")
    append_jsonl(path, {"n": 2})

    read = read_jsonl(path)
    assert len(read.rows) == 3
    assert read.malformed == ()  # NOT malformed: structurally fine
    value = read.rows[1]["n"]
    if expected is None:
        assert math.isnan(value)
    else:
        assert value == expected


@pytest.mark.parametrize("label", sorted(HOSTILE_LINES))
def test_w2_hostile_line_content_neither_bricks_nor_vanishes(label: str, tmp_path: Path) -> None:
    """A guard that LISTS exception types is a denylist over untrusted input, and a
    denylist leaks: it passes the case you thought of and bricks on the next one."""
    path = tmp_path / "log" / "events.jsonl"
    append_jsonl(path, {"n": 1})
    with path.open("ab") as handle:
        handle.write(HOSTILE_LINES[label].encode("utf-8") + b"\n")

    append_jsonl(path, {"n": 2})  # the WRITE path must not brick

    read = read_jsonl(path)
    assert [row["n"] for row in read.rows] == [1, 2]
    assert len(read.malformed) == 1
    assert read.malformed[0].reason


def _read_between_two_rows(tmp_path: Path, line: str) -> JsonlRead:
    path = tmp_path / "log" / "events.jsonl"
    append_jsonl(path, {"n": 1})
    with path.open("ab") as handle:
        handle.write(line.encode("utf-8") + b"\n")
    append_jsonl(path, {"n": 2})
    return read_jsonl(path)


def _nested(depth: int) -> str:
    """An object holding arrays, ``depth`` levels counting the object itself."""
    return '{"n": ' + "[" * (depth - 1) + "]" * (depth - 1) + "}"


def test_w2_a_line_nested_exactly_to_the_bound_is_kept(tmp_path: Path) -> None:
    read = _read_between_two_rows(tmp_path, _nested(MAX_JSON_DEPTH))
    assert len(read.rows) == 3
    assert read.malformed == ()


def test_w2_a_line_nested_past_the_bound_is_malformed_on_every_python(tmp_path: Path) -> None:
    """Some interpreters raise on deep nesting and some parse it, so the bound is
    checked before the parser runs and every interpreter gives the same answer."""
    read = _read_between_two_rows(tmp_path, _nested(MAX_JSON_DEPTH + 1))
    assert [row["n"] for row in read.rows] == [1, 2]
    assert len(read.malformed) == 1
    assert str(MAX_JSON_DEPTH) in read.malformed[0].reason


def test_w2_brackets_inside_a_string_do_not_count_as_nesting(tmp_path: Path) -> None:
    read = _read_between_two_rows(tmp_path, '{"n": "' + "[" * 100000 + '"}')
    assert read.malformed == ()
    assert read.rows[1]["n"] == "[" * 100000


def test_w2_an_escaped_quote_does_not_end_the_string(tmp_path: Path) -> None:
    """Past the bound, so a scanner that took the escaped quote as the end of the
    string would count every bracket after it and call the line malformed."""
    brackets = "[" * (MAX_JSON_DEPTH + 1)
    read = _read_between_two_rows(tmp_path, '{"n": "\\"' + brackets + '"}')
    assert read.malformed == ()
    assert read.rows[1]["n"] == '"' + brackets


def test_w2_a_line_that_cannot_pass_the_bound_is_not_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scan is a Python loop over every character, so it runs only on a line
    with more opening brackets than the bound. The count includes brackets inside
    strings, so such a line is still scanned and still kept."""
    from gr2.prototypes import jsonl_store

    scanned: list[str] = []
    scan = jsonl_store._nests_deeper_than

    def recording(line: str, bound: int) -> bool:
        scanned.append(line)
        return scan(line, bound)

    monkeypatch.setattr(jsonl_store, "_nests_deeper_than", recording)
    in_a_string = '{"n": "' + "[" * 100000 + '"}'
    read = _read_between_two_rows(tmp_path, in_a_string)
    assert [row["n"] for row in read.rows] == [1, "[" * 100000, 2]
    assert scanned == [in_a_string]


# --------------------------------------------------------------------------- W3

HOSTILE_BYTES = {
    "invalid byte 0xff": b'{"n": "\xff"}',
    "lone surrogate": b'{"n": "\xed\xa0\x80"}',
    "truncated multi-byte sequence": b'{"n": "\xe2\x82"}',
    "overlong encoding": b'{"n": "\xc0\xaf"}',
    "continuation byte with no lead": b'{"n": "\x80\x80"}',
    "line is pure binary": b"\x00\x01\xff\xfe\xfd",
}


@pytest.mark.parametrize("label", sorted(HOSTILE_BYTES))
def test_w3_undecodable_bytes_neither_brick_nor_vanish(label: str, tmp_path: Path) -> None:
    """The bytes-to-text boundary is content-dependent, so it is guarded there."""
    path = tmp_path / "log" / "events.jsonl"
    append_jsonl(path, {"n": 1})
    with path.open("ab") as handle:
        handle.write(HOSTILE_BYTES[label] + b"\n")

    append_jsonl(path, {"n": 2})

    read = read_jsonl(path)
    assert [row["n"] for row in read.rows] == [1, 2]
    assert len(read.malformed) == 1
    assert read.malformed[0].excerpt  # reportable even when it is not valid text
    assert "utf-8" in read.malformed[0].reason


def test_w3_an_undecodable_line_is_confined_to_itself(tmp_path: Path) -> None:
    """One bad byte costs ONE line. A whole-file decode costs every record."""
    path = tmp_path / "log" / "events.jsonl"
    for n in range(1, 6):
        append_jsonl(path, {"n": n})
    with path.open("ab") as handle:
        handle.write(b'{"n": "\xff"}\n')
    for n in range(6, 11):
        append_jsonl(path, {"n": n})

    read = read_jsonl(path)
    assert [row["n"] for row in read.rows] == list(range(1, 11))
    assert len(read.malformed) == 1


# --------------------------------------------------------------------------- W4


def test_w4_the_count_reaches_a_consumer(tmp_path: Path) -> None:
    """A count nobody surfaces is the same silence as no count. Verify by fruit."""
    path = tmp_path / "log" / "events.jsonl"
    append_jsonl(path, {"n": 1})
    _tear(path, b'{"n": 2, "part')
    append_jsonl(path, {"n": 3})

    out = io.StringIO()
    reported = warn_unreadable(read_jsonl(path), "the lane event log", stream=out)

    assert reported is True
    text = out.getvalue()
    assert "1 unreadable line(s)" in text
    assert "the lane event log" in text
    assert "first at line 1" in text  # names WHERE, so it can be acted on


def test_w4_a_healthy_log_reports_nothing(tmp_path: Path) -> None:
    """The negative case: silence when there is nothing to say, or the signal is noise."""
    path = tmp_path / "log" / "events.jsonl"
    append_jsonl(path, {"n": 1})

    out = io.StringIO()
    reported = warn_unreadable(read_jsonl(path), "the lane event log", stream=out)

    assert reported is False
    assert out.getvalue() == ""
