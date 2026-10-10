"""Review records as field trees (package ``dev.synapt.grip.review.v1alpha1``).

A record is a Git tree with one entry per protobuf field, named ``NNN.W[r]_name``:
the field number zero-padded to three digits (Git sorts entry names bytewise, so
padding is what keeps field 10 after field 2), the wire type, ``r`` for a repeated
field written as separate occurrences (a tree of ``0000001``.. entries), and the
field name from the WRITER's schema. A single nested message is a plain tree.
Scalars are stored as their raw protobuf payload bytes. (Distinct from
``review_records``, which holds per-worktree safety records.)

The rules:

- Readers decode by NUMBER and never read the name.
- Unknown entries are carried byte for byte, name included.
- An edit replaces one entry and leaves every other object as written.
- Verify checks the objects as written; it never rebuilds names from the schema.
- The tree IS the record; protobuf binary is derived on request and never stored.

The schema has one owner outside gr2, one branch per package. gr2 carries a copy,
pinned to the commit of that branch it came from (SCHEMA_COMMIT) and to its
SHA-256, and refuses to load any other bytes.
"""
from __future__ import annotations

import hashlib
import importlib.resources
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from . import gitops

PACKAGE = "dev.synapt.grip.review.v1alpha1"
SCHEMA_COMMIT = "88babf4a0c55110a1e1badc53f07a6f0f8912f86"
SCHEMA_SOURCE = f"{PACKAGE}@{SCHEMA_COMMIT}"
_PROTO_RESOURCE = f"schemas/{PACKAGE}/review.proto"
_PROTO_SHA256 = "94e75f8ff703a20a2ab2cecd5226c24d25e9f8c1a1d295cdaf224ab0a2e73296"

MAX_FIELD_NUMBER = 999
# ASCII digits only and matched whole: \d admits other scripts' digits and $ admits a
# trailing newline, and either would give one field number two spellings.
_ENTRY = re.compile(r"([0-9]{3})\.([0125])(r?)(?:_([A-Za-z0-9_]+))?")
_BLOB_MODE, _TREE_MODE = "100644", "040000"

# proto3 scalar type -> wire type. Only the types the review package uses, plus the
# varint and fixed families so a newer schema's scalar decodes by number.
_WIRE_TYPE = {
    "string": 2, "bytes": 2,
    "int32": 0, "int64": 0, "uint32": 0, "uint64": 0, "bool": 0, "sint32": 0, "sint64": 0,
    "fixed64": 1, "sfixed64": 1, "double": 1, "fixed32": 5, "sfixed32": 5, "float": 5,
}


class ReviewRecordError(ValueError):
    """A record that cannot be written or read as a field tree."""


@dataclass(frozen=True)
class Field:
    number: int
    name: str
    type: str          # scalar type name, or the message name for a nested message
    repeated: bool

    @property
    def message(self) -> bool:
        return self.type not in _WIRE_TYPE

    @property
    def wire_type(self) -> int:
        return 2 if self.message else _WIRE_TYPE[self.type]


def _read_proto_bytes() -> bytes:
    """The installed resource, or the in-repo copy when pytest imports gr2 bare.
    Either way the bytes are hash-checked before use."""
    try:
        return (importlib.resources.files("gr2") / _PROTO_RESOURCE).read_bytes()
    except Exception:
        return (Path(__file__).resolve().parent.parent / _PROTO_RESOURCE).read_bytes()


def parse_proto(text: str) -> dict[str, dict[int, Field]]:
    """The messages and fields of a flat proto3 file: the only form the review package
    uses (no options, enums, maps, oneofs, nested declarations, or more than one
    declaration per line). Any other non-blank line is REFUSED rather than skipped, so a
    field cannot pass unseen; a field number outside 1..MAX_FIELD_NUMBER is refused,
    since its padded name would sort out of number order."""
    messages: dict[str, dict[int, Field]] = {}
    current: dict[int, Field] | None = None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if current is None:
            if re.fullmatch(r'syntax\s*=\s*"proto3"\s*;', line) or re.fullmatch(r"package\s+[A-Za-z0-9_.]+\s*;", line):
                continue
            m = re.fullmatch(r"message\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{", line)
            if m and m.group(1) not in messages:
                current = messages.setdefault(m.group(1), {})
                continue
        else:
            if line == "}":
                current = None
                continue
            m = re.fullmatch(r"(repeated\s+)?([A-Za-z_][A-Za-z0-9_]*)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([0-9]+)\s*;", line)
            if m:
                number = int(m.group(4))
                if not 1 <= number <= MAX_FIELD_NUMBER:
                    raise ReviewRecordError(f"field {m.group(3)} = {number}: review packages use 1..{MAX_FIELD_NUMBER}")
                if number in current:
                    raise ReviewRecordError(f"field number {number} declared twice")
                current[number] = Field(number, m.group(3), m.group(2), bool(m.group(1)))
                continue
        raise ReviewRecordError(f"schema line {lineno} is not a form this reader accepts: {raw.strip()!r}")
    if current is not None:
        raise ReviewRecordError("schema ends inside a message")
    return messages


_schema: dict[str, dict[int, Field]] | None = None


def schema() -> dict[str, dict[int, Field]]:
    global _schema
    if _schema is None:
        raw = _read_proto_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        if actual != _PROTO_SHA256:
            raise ReviewRecordError(
                f"packaged {PACKAGE} schema hash mismatch: expected {_PROTO_SHA256}, got {actual}; "
                f"refusing a schema that is not {SCHEMA_SOURCE}")
        _schema = parse_proto(raw.decode("utf-8"))
    return _schema


# --- protobuf wire format -------------------------------------------------------

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = n = 0
    while True:
        if i >= len(buf):
            raise ReviewRecordError("truncated varint")
        b = buf[i]
        i += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n, i
        shift += 7


def _tag(number: int, wire_type: int) -> bytes:
    return _varint(number << 3 | wire_type)


def check_payload(wt: int, payload: bytes) -> None:
    """A scalar must be exactly ONE payload of its wire type: a varint of 1..10 bytes
    whose last byte alone ends it (a tenth byte of at most 1), or exactly 8 or 4 bytes
    for the fixed widths. Anything else would let one entry's bytes run into the next
    when protobuf is derived. Length-delimited payloads carry their own length."""
    if wt == 0:
        ok = (1 <= len(payload) <= 10 and not payload[-1] & 0x80
              and all(b & 0x80 for b in payload[:-1]) and not (len(payload) == 10 and payload[-1] > 1))
    elif wt == 1:
        ok = len(payload) == 8
    elif wt == 5:
        ok = len(payload) == 4
    else:
        ok = wt == 2
    if not ok:
        raise ReviewRecordError(f"payload {payload[:12].hex()} is not one wire type {wt} value")


def fields(buf: bytes) -> list[tuple[int, int, bytes]]:
    """(number, wire type, payload) in wire order. A varint payload keeps its varint
    bytes; a fixed payload its little-endian bytes; length-delimited drops the length."""
    out, i = [], 0
    while i < len(buf):
        key, i = _read_varint(buf, i)
        number, wt = key >> 3, key & 7
        if wt == 0:
            _, j = _read_varint(buf, i)
        elif wt == 1:
            j = i + 8
        elif wt == 5:
            j = i + 4
        elif wt == 2:
            length, i = _read_varint(buf, i)
            j = i + length
        else:
            raise ReviewRecordError(f"unsupported wire type {wt} for field {number}")
        if j > len(buf):
            raise ReviewRecordError(f"truncated field {number}")
        check_payload(wt, buf[i:j])
        out.append((number, wt, buf[i:j]))
        i = j
    return out


def _emit(number: int, wt: int, payload: bytes) -> bytes:
    return _tag(number, wt) + (_varint(len(payload)) if wt == 2 else b"") + payload


# --- record dict <-> protobuf ------------------------------------------------------

def encode(record: dict, message: str = "ReviewBind") -> bytes:
    """Canonical protobuf for a record dict: fields in number order, proto3 defaults
    (empty string/bytes, absent message) omitted, members sorted by key."""
    spec = schema()[message]
    unknown = set(record) - {f.name for f in spec.values()}
    if unknown:
        raise ReviewRecordError(f"{message} has no field(s) {sorted(unknown)}")
    out = b""
    for number in sorted(spec):
        f = spec[number]
        if f.name not in record or record[f.name] in (None, "", b"", [], {}):
            continue
        values = record[f.name]
        if f.repeated:
            if f.name == "members":
                values = sorted(values, key=lambda m: m["key"])
        else:
            values = [values]
        for v in values:
            if f.message:
                out += _emit(number, 2, encode(v, f.type))
            elif f.wire_type == 2:
                out += _emit(number, 2, v.encode("utf-8") if isinstance(v, str) else bytes(v))
            else:
                raise ReviewRecordError(f"{message}.{f.name}: scalar type {f.type} is not written by this package")
    return out


def decode(buf: bytes, message: str = "ReviewBind") -> dict:
    """Known fields by number. An unknown number is ignored here; it is never lost from
    a stored record, because the tree, not this dict, is the record."""
    spec = schema()[message]
    out: dict = {}
    for number, wt, payload in fields(buf):
        f = spec.get(number)
        if f is None or wt != f.wire_type:
            continue
        if f.message:
            v = decode(payload, f.type)
        elif f.type == "string":
            v = payload.decode("utf-8")
        else:
            v = payload
        if f.repeated:
            out.setdefault(f.name, []).append(v)
        else:
            out[f.name] = v
    return out


# --- field trees -----------------------------------------------------------------

def _git(repo: Path, *args: str, data: bytes | None = None) -> bytes:
    proc = gitops.run(repo, *args, input=data, binary=True)
    if proc.returncode != 0:
        raise ReviewRecordError(f"git {args[0]} failed: {proc.stderr.decode(errors='replace').strip()[:200]}")
    return proc.stdout


def _blob(repo: Path, payload: bytes) -> str:
    return _git(repo, "hash-object", "-w", "--stdin", data=payload).decode().strip()


Entry = tuple[str, str, str, str]  # (mode, kind, oid, name), as ls-tree reports it


def _mktree(repo: Path, entries: list[Entry]) -> str:
    # surrogateescape round-trips a name that is not UTF-8, as _ls decoded it
    data = b"".join(f"{mode} {kind} {oid}\t".encode() + name.encode("utf-8", "surrogateescape") + b"\0"
                    for mode, kind, oid, name in entries)
    return _git(repo, "mktree", "-z", data=data).decode().strip()


def _ls(repo: Path, tree: str) -> list[Entry]:
    out = []
    for line in _git(repo, "ls-tree", "-z", tree).split(b"\0"):
        if not line:
            continue
        meta, name = line.split(b"\t", 1)
        mode, kind, oid = meta.decode().split()
        out.append((mode, kind, oid, name.decode("utf-8", errors="surrogateescape")))
    return out


def entry_name(f: Field | None, number: int, wt: int, repeated: bool) -> str:
    if not 1 <= number <= MAX_FIELD_NUMBER:
        raise ReviewRecordError(f"field number {number} is outside 1..{MAX_FIELD_NUMBER}")
    return f"{number:03d}.{wt}{'r' if repeated else ''}" + (f"_{f.name}" if f else "")


def parse_entry(name: str) -> tuple[int, int, bool]:
    m = _ENTRY.fullmatch(name)
    if not m:
        raise ReviewRecordError(f"not a field tree entry name: {name!r}")
    number = int(m.group(1))
    if not 1 <= number <= MAX_FIELD_NUMBER:
        raise ReviewRecordError(f"entry {name!r}: field number outside 1..{MAX_FIELD_NUMBER}")
    return number, int(m.group(2)), bool(m.group(3))


def _occurrences(repo: Path, name: str, oid: str) -> list[Entry]:
    """A repeated entry's occurrences, which must be exactly 0000001..n in order."""
    kids = _ls(repo, oid)
    want = [f"{i:07d}" for i in range(1, len(kids) + 1)]
    if not kids or [k[3] for k in kids] != want:
        raise ReviewRecordError(f"{name}: occurrences must be exactly 0000001..n, got {[k[3] for k in kids]}")
    return kids


def write_tree(repo: Path, buf: bytes, message: str = "ReviewBind") -> str:
    """Store protobuf bytes as a field tree, names from the current schema."""
    spec = schema()[message]
    grouped: dict[tuple[int, int], list[bytes]] = {}
    for number, wt, payload in fields(buf):
        grouped.setdefault((number, wt), []).append(payload)
    if len({n for n, _ in grouped}) != len(grouped):
        raise ReviewRecordError("one field number arrives with two wire types")
    entries: list[Entry] = []
    for (number, wt), payloads in grouped.items():
        f = spec.get(number)
        nested = f is not None and f.message and wt == 2

        def leaf(p: bytes) -> tuple[str, str, str]:
            if nested:
                return _TREE_MODE, "tree", write_tree(repo, p, f.type)
            return _BLOB_MODE, "blob", _blob(repo, p)

        repeated = (f.repeated if f is not None else False) or len(payloads) > 1
        if repeated:
            occ = [(*leaf(p), f"{i:07d}") for i, p in enumerate(payloads, start=1)]
            entries.append((_TREE_MODE, "tree", _mktree(repo, occ), entry_name(f, number, wt, True)))
        else:
            entries.append((*leaf(payloads[0]), entry_name(f, number, wt, False)))
    return _mktree(repo, entries)


def write_record(repo: Path, record: dict) -> str:
    return write_tree(repo, encode(record))


def to_protobuf(repo: Path, tree: str) -> bytes:
    """Derive protobuf from a stored tree, by number, in entry order. Needs no schema:
    a tree is a message, a blob is a payload. Unknown entries are re-emitted too."""
    out = b""
    for mode, kind, oid, name in _ls(repo, tree):
        number, wt, repeated = parse_entry(name)
        kids = _occurrences(repo, name, oid) if repeated else [(mode, kind, oid, name)]
        for _, k, o, _ in kids:
            payload = to_protobuf(repo, o) if k == "tree" else _git(repo, "cat-file", "blob", o)
            check_payload(wt, payload)
            out += _emit(number, wt, payload)
    return out


def read_record(repo: Path, tree: str) -> dict:
    """The known fields, as a VIEW. Never write this dict back as the record: it does not
    carry unknown entries, so write_record(read_record(t)) loses them. Edit a stored
    record with replace_entry."""
    return decode(to_protobuf(repo, tree))


def replace_entry(repo: Path, tree: str, path: list[str], payload: bytes) -> str:
    """Edit by entry: replace the blob at ``path`` (entries matched by their ``NNN.W``
    stem, occurrences by ordinal) and rebuild only the trees above it, keeping every
    other entry's mode, object and name as written. Exactly one entry must match."""
    head, rest = path[0], path[1:]
    entries = _ls(repo, tree)
    matches = [e for e in entries if e[3] == head or e[3].split("_", 1)[0].rstrip("r") == head]
    if len(matches) != 1:
        raise ReviewRecordError(f"{head!r} matches {len(matches)} entries; an edit names exactly one")
    out: list[Entry] = []
    for mode, kind, oid, name in entries:
        if (mode, kind, oid, name) == matches[0]:
            if rest:
                if kind != "tree":
                    raise ReviewRecordError(f"{name} is a blob; cannot descend to {rest}")
                oid = replace_entry(repo, oid, rest, payload)
            else:
                if kind != "blob":
                    raise ReviewRecordError(f"{name} is a tree; replace one of its entries")
                oid = _blob(repo, payload)
        out.append((mode, kind, oid, name))
    return _mktree(repo, out)


def _check_message(repo: Path, tree: str, message: str | None) -> None:
    spec = schema()[message] if message else {}
    seen: set[int] = set()
    for mode, kind, oid, name in _ls(repo, tree):
        number, wt, repeated = parse_entry(name)
        if number in seen:
            raise ReviewRecordError(f"field {number} has more than one entry")
        seen.add(number)
        f = spec.get(number)
        if f is not None and (wt != f.wire_type or repeated != f.repeated):
            raise ReviewRecordError(f"{name}: field {number} is {f.type}{' repeated' if f.repeated else ''} "
                                    f"(wire type {f.wire_type}) in {message}")
        if repeated:
            if kind != "tree":
                raise ReviewRecordError(f"{name}: a repeated entry is a tree of occurrences")
            kids = _occurrences(repo, name, oid)
        else:
            kids = [(mode, kind, oid, name)]
        for kmode, k, o, kname in kids:
            # Every stored object passes here: a single entry is its own one kid.
            if (kmode, k) not in ((_BLOB_MODE, "blob"), (_TREE_MODE, "tree")):
                raise ReviewRecordError(f"{name}/{kname}: mode {kmode} {k} is not a field tree entry")
            if k == "tree" and wt != 2:
                raise ReviewRecordError(f"{name}: a message is wire type 2")
            if f is not None and (k == "tree") != f.message:
                raise ReviewRecordError(f"{name}: {f.type} is stored as a {'tree' if f.message else 'blob'}")
            if k == "tree":
                _check_message(repo, o, f.type if f is not None else None)


def verify_tree(repo: Path, tree: str, message: str = "ReviewBind") -> None:
    """Check the record AS WRITTEN: every object re-hashes from its own content
    (``git fsck --strict``); every entry is a plain blob or tree with a field tree name,
    one per field number, its wire type and repetition agreeing with any field the
    schema knows; occurrences run 0000001..n; and the record decodes by number. Names
    are never regenerated from the schema and compared. This checks a tree, not WHICH
    tree: binding a record to its identity is the ref writer's job."""
    proc = gitops.run(repo, "fsck", "--strict", "--no-dangling", tree, binary=True)
    if proc.returncode != 0:
        raise ReviewRecordError(f"fsck refused record {tree}: {proc.stderr.decode(errors='replace').strip()[:200]}")
    _check_message(repo, tree, message)
    decode(to_protobuf(repo, tree), message)
