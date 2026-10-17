"""Protobuf as JSON, for people and for ``git diff``.

``gr2 proto json <path|oid|rev:path>`` renders one stored record: a file of protobuf
bytes (what git hands a textconv), a blob, or a field tree (see
``review_field_tree``). Install it as a textconv and ``git diff`` shows JSON:

    *.pb diff=protoblob                                  # .gitattributes
    git config diff.protoblob.textconv "gr2 proto json --textconv"

Without a schema the keys are field numbers, in wire order. A varint or fixed value
prints as an unsigned integer. A length-delimited value prints as a string when it
decodes as UTF-8, else as ``{"bytes_b64": ...}``; nesting is never guessed. Bytes that are
not protobuf are refused; under ``--textconv`` they print a one-line marker instead and exit 0. A field
that occurs more than once prints as a list. With ``message`` the bundled review
schema supplies names, nesting and scalar types; a number the schema does not know,
or one that arrives with another wire type, prints by number as above.
"""
from __future__ import annotations

import base64
import hashlib
import json
import struct
from pathlib import Path

from .review_field_tree import (
    Field, GitReader, ReviewRecordError, Schema, _git, _read_varint, _spec, fields, to_protobuf,
)


def _raw(wt: int, payload: bytes):
    if wt == 0:
        return _read_varint(payload, 0)[0]
    if wt in (1, 5):
        return int.from_bytes(payload, "little")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return {"bytes_b64": base64.b64encode(payload).decode("ascii")}


def _typed(f: Field, payload: bytes, schema: Schema | None):
    t = f.type
    if f.message:
        return render(payload, t, schema)
    if t == "string":
        return _raw(2, payload)
    if t == "bytes":
        return {"bytes_b64": base64.b64encode(payload).decode("ascii")}
    if t == "bool":
        return _read_varint(payload, 0)[0] != 0
    if t in ("sint32", "sint64"):
        n = _read_varint(payload, 0)[0]
        return (n >> 1) ^ -(n & 1)
    if t in ("int32", "int64"):
        n = _read_varint(payload, 0)[0]
        return n - (1 << 64) if n >= 1 << 63 else n
    if t == "double":
        return struct.unpack("<d", payload)[0]
    if t == "float":
        return struct.unpack("<f", payload)[0]
    if t == "sfixed64":
        return struct.unpack("<q", payload)[0]
    if t == "sfixed32":
        return struct.unpack("<i", payload)[0]
    return _raw(f.wire_type, payload)


def render(buf: bytes, message: str | None = None, schema: Schema | None = None) -> dict:
    """Protobuf bytes as a JSON-ready dict; see the module docstring for the rules."""
    try:
        spec = _spec(schema, message) if message else {}
    except KeyError:
        raise ReviewRecordError(f"no message {message!r} in the review schema") from None
    out: dict = {}
    seen: dict[str, int] = {}
    for number, wt, payload in fields(buf):
        f = spec.get(number)
        if f is not None and f.wire_type == wt:
            key, value, repeated = f.name, _typed(f, payload, schema), f.repeated
        else:
            key, value, repeated = str(number), _raw(wt, payload), False
        seen[key] = seen.get(key, 0) + 1
        if repeated:
            out.setdefault(key, []).append(value)
        elif seen[key] == 1:
            out[key] = value
        elif seen[key] == 2:
            out[key] = [out[key], value]
        else:
            out[key].append(value)
    return out


def load(target: str, repo: Path) -> bytes:
    """Protobuf bytes for ``target``: an existing file's bytes (a file wins over a
    revision of the same spelling), else a blob or a field tree named in ``repo``."""
    p = Path(target)
    if p.exists() and not p.is_dir():
        return p.read_bytes()
    try:
        kind = _git(repo, "cat-file", "-t", target).decode().strip()
    except ReviewRecordError:
        raise ReviewRecordError(f"{target!r} is neither a file nor an object in {repo}") from None
    if kind == "blob":
        return _git(repo, "cat-file", "blob", target)
    if kind == "tree":
        return to_protobuf(repo, target, GitReader(repo))
    raise ReviewRecordError(f"{target!r} is a {kind}; give a blob, a field tree, or a file")


def textconv_marker(target: str, error: Exception) -> str:
    """What --textconv prints for bytes it cannot render: one line, so one bad file never
    stops a whole ``git diff``, and its sha256, so a change to that file still shows."""
    p = Path(target)
    digest = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "unread"
    return f"# gr2 proto json: not rendered ({error}); sha256 {digest}\n"


def dumps(record: dict) -> str:
    # ASCII only: a textconv's output lands in a terminal, so no raw control or bidi text.
    return json.dumps(record, indent=2, ensure_ascii=True) + "\n"
