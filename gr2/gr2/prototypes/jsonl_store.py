"""Torn-line-safe JSONL primitives, shared by the lane prototypes.

Both lane prototypes carried a BYTE-IDENTICAL ``append_jsonl`` and a reader with
the same body under two names. They were not two instances of a defect class;
they were one function, copied. Fixing them separately would have written the
same fix and the same witnesses twice and left the copies free to diverge again,
which is how the situation arose. One home, two consumers.

The defect, measured on the sibling surface this shape came from:

* the writer appended with no terminator repair and no ``fsync``, so a writer
  killed mid-write left a remnant and the NEXT record glued onto it — a record
  that had itself completed became part of one unparseable line and was lost;
* the reader parsed with a bare ``json.loads`` over ``read_text().splitlines()``,
  so the remnant raised, and the whole-file decode meant one invalid byte
  anywhere destroyed every record in the file rather than its own line.

A line passes through four layers — read, decode, parse, shape-check — and the
last three are each guarded where they happen. Types are VALIDATED rather than
coerced, because a coercion over untrusted bytes forces the caller to enumerate
the exceptions it might raise, which is a denylist, and a denylist leaks.
Unbounded line length is a resource limit on the READ layer and is deliberately
NOT defended here; it is named rather than left to be discovered.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from gr2.prototypes.propagation_state_machine import MalformedLine


@dataclass(frozen=True)
class JsonlRead:
    """Rows that could be read, and the lines that could not.

    The count travels WITH the data. These are module-level functions with no
    object to hang health on, and a module-level accumulator would be hidden
    state and wrong under concurrent readers — so the health of the scan is a
    return value instead. A caller that ignores ``malformed`` still gets correct
    rows; a caller that wants to report health already holds it. Skipping alone
    is silent, and silence is the defect: the remnant sits in the file while the
    caller sees a healthy-looking result.
    """

    rows: list[dict]
    malformed: tuple[MalformedLine, ...] = field(default=())


def _decode_line(raw: bytes) -> tuple[str | None, str]:
    """Bytes become text HERE, so the failure that can happen HERE is guarded here."""

    try:
        return raw.decode("utf-8"), ""
    except UnicodeDecodeError as exc:
        return None, str(exc)


def _read_object(line: str) -> tuple[dict | None, str]:
    """``json.loads`` is the only operation over a decoded line that can raise."""

    try:
        obj = json.loads(line)
    except (ValueError, RecursionError) as exc:
        return None, str(exc)
    if not isinstance(obj, dict):
        return None, f"line is a {type(obj).__name__}, not an object"
    return obj, ""


def append_jsonl(path: Path, payload: dict) -> None:
    """Append one record, repairing a missing terminator first, then fsync.

    Without the repair a remnant GLUES the next record onto itself and the next
    record is lost. Without the fsync the record is not durable against the very
    crash that produces remnants.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    unterminated = False
    try:
        with path.open("rb") as probe:
            if probe.seek(0, os.SEEK_END):
                probe.seek(-1, os.SEEK_END)
                unterminated = probe.read(1) != b"\n"
    except FileNotFoundError:
        pass
    with path.open("ab") as handle:
        if unterminated:
            handle.write(b"\n")
        handle.write((json.dumps(payload) + "\n").encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())


def read_jsonl(path: Path) -> JsonlRead:
    """Read every line that can be read, and report every line that cannot."""

    if not path.exists():
        return JsonlRead(rows=[])
    rows: list[dict] = []
    malformed: list[MalformedLine] = []
    for index, raw in enumerate(path.read_bytes().split(b"\n")):
        stripped = raw.strip()
        if not stripped:
            continue
        line, reason = _decode_line(stripped)
        if line is not None:
            obj, reason = _read_object(line)
            if obj is not None:
                rows.append(obj)
                continue
        malformed.append(
            MalformedLine(
                index=index,
                excerpt=stripped[:120].decode("utf-8", "replace"),
                reason=reason,
            )
        )
    return JsonlRead(rows=rows, malformed=tuple(malformed))


def warn_unreadable(read: JsonlRead, what: str, stream: object = None) -> bool:
    """Report unreadable lines on STDERR. Returns whether anything was reported.

    Lives HERE, beside the primitives it reports on, because the first version of
    this fix put a helper in one consumer and an inline copy of the same three
    lines in the other — which is byte-adjacent duplication of a reporting rule
    across two files, the same shape as the copied ``append_jsonl`` this module
    exists to eliminate. One home for the rule, or the message formats drift.

    STDERR specifically: a ``--json`` caller's stdout must stay machine-readable.
    And it is reported at ALL because a count nobody surfaces is the same silence
    as no count — the CLI is the layer that already looks.
    """

    if not read.malformed:
        return False
    first = read.malformed[0]
    print(
        f"warning: {len(read.malformed)} unreadable line(s) in {what}; "
        f"first at line {first.index}: {first.reason}",
        file=stream if stream is not None else sys.stderr,
    )
    return True


__all__ = [
    "JsonlRead",
    "MalformedLine",
    "append_jsonl",
    "read_jsonl",
    "warn_unreadable",
]
