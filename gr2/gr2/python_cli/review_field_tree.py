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
- One stored form per message, declared by the schema (field tree by default; one
  canonical protobuf blob when the message declares `leaf` at its top); never convert
  a stored record between forms. `gr2 proto json` renders either form as JSON.

The schema has one owner outside gr2, one branch per package. gr2 carries a copy,
pinned to the commit of that branch it came from (SCHEMA_COMMIT) and to its
SHA-256, and refuses to load any other bytes.
"""
from __future__ import annotations

import hashlib
import os
import importlib.resources
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from . import gitops, next_steps

PACKAGE = "dev.synapt.grip.review.v1alpha1"
SCHEMA_COMMIT = "b723390999e6529849123263e703629908ff0706"
SCHEMA_SOURCE = f"{PACKAGE}@{SCHEMA_COMMIT}"
_PROTO_RESOURCE = f"schemas/{PACKAGE}/review.proto"
_PROTO_SHA256 = "7557a232e67cdf6b17e3e3b16a66fef6de1116d53550e078fad9fe6d1c8853d7"

MAX_FIELD_NUMBER = 999
# ASCII digits only and matched whole: \d admits other scripts' digits and $ admits a
# trailing newline, and either would give one field number two spellings.
# a full object id as ls-tree names it: 40 hex (sha1) or 64 (sha256)
_FULL_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
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


Schema = dict[str, dict[int, Field]]


def _spec(schema_: Schema | None, message: str) -> dict[int, Field]:
    """One message of ``schema_`` (a parse_proto result), or of the pinned review schema
    when none is given. A caller that passes its own schema owns its provenance."""
    return (schema() if schema_ is None else schema_)[message]


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
        if number == 0:
            raise ReviewRecordError("field number 0 is not a protobuf field")
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

def encode(record: dict, message: str = "ReviewBind", schema: Schema | None = None) -> bytes:
    """Canonical protobuf for a record dict: fields in number order, proto3 defaults
    (empty string/bytes, absent message) omitted, members sorted by key."""
    spec = _spec(schema, message)
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
                out += _emit(number, 2, encode(v, f.type, schema))
            elif f.wire_type == 2:
                # a bytes field carried as str (a range read with surrogateescape) goes back to the exact bytes
                errors = "surrogateescape" if f.type == "bytes" else "strict"
                out += _emit(number, 2, v.encode("utf-8", errors) if isinstance(v, str) else bytes(v))
            else:
                raise ReviewRecordError(f"{message}.{f.name}: scalar type {f.type} is not written by this package")
    return out


def decode(buf: bytes, message: str = "ReviewBind", schema: Schema | None = None) -> dict:
    """Known fields by number. An unknown number is ignored here; it is never lost from
    a stored record, because the tree, not this dict, is the record."""
    spec = _spec(schema, message)
    out: dict = {}
    for number, wt, payload in fields(buf):
        f = spec.get(number)
        if f is None or wt != f.wire_type:
            continue
        if f.message:
            v = decode(payload, f.type, schema)
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

def _git(repo: Path, *args: str, data: bytes | None = None, env: dict | None = None) -> bytes:
    proc = gitops.run(repo, *args, input=data, binary=True, env=env)
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


def _ls(repo: Path, tree: str, env: dict | None = None) -> list[Entry]:
    out = []
    for line in _git(repo, "ls-tree", "-z", tree, env=env).split(b"\0"):
        if not line:
            continue
        meta, name = line.split(b"\t", 1)
        mode, kind, oid = meta.decode().split()
        out.append((mode, kind, oid, name.decode("utf-8", errors="surrogateescape")))
    return out


# --- readers: how a stored record's objects are fetched ------------------------------

def _local() -> dict:
    """The environment for a review read: in a partial clone, an absent object answers missing at once rather
    than reaching the network (git before 2.44 ignores the variable)."""
    return {**os.environ, "GIT_NO_LAZY_FETCH": "1"}

def _promisor(repo: Path) -> str | None:
    """The remote a partial clone fetches absent objects from: extensions.partialclone (git's own name for
    it), else the first remote.<name>.promisor that is true (what a --filter clone writes); None when the repo
    has neither, since then git has nowhere to fetch an absent object from."""
    named = gitops.run(repo, "config", "--get", "extensions.partialclone", env=_local()).stdout.strip()
    if named:
        return named
    found = gitops.run(repo, "config", "-z", "--type=bool", "--get-regexp", r"^remote\..*\.promisor$", env=_local())
    for entry in found.stdout.split("\0"):
        key, _, value = entry.partition("\n")
        if value == "true":
            return key.removeprefix("remote.").removesuffix(".promisor")
    return None


def missing_locally(repo: Path, verb: str, oid: str) -> str:
    """The refusal for an object a partial clone does not hold: it names the fetch that brings it in."""
    return (f"git {verb} failed: {oid} is missing locally (partial clone?); bring it in with: git -C "
            f"{next_steps._shown(str(repo))} fetch -- {next_steps._shown(_promisor(repo))} {next_steps._shown(oid)}")

# A reader has ls(tree) -> entries, exactly as ls-tree reports them, and blob(oid) ->
# bytes. The readers differ only in how many git processes they start, never in what
# they return.

class GitReader:
    """One git process per object read: ``ls-tree`` per tree, ``cat-file`` per blob."""

    def __init__(self, repo: Path):
        self.repo = Path(repo)

    def ls(self, tree: str) -> list[Entry]:
        return _ls(self.repo, tree, env=_local())

    def blob(self, oid: str) -> bytes:
        return _git(self.repo, "cat-file", "blob", oid, env=_local())

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class _Deadlined:
    """The batch process's stdout with every silence bounded by ``deadline`` seconds. A read runs on a
    thread, not under select, so a stream with no file descriptor reads the same way. A read that
    outlasts the deadline is an answer git will never finish (a loose object whose header claims
    more bytes than its body holds keeps ``cat-file --batch`` alive with the read short): the
    process is killed, the reader closed, and the blob refused as truncated."""

    def __init__(self, stream, proc: subprocess.Popen, reader: "BatchReader", deadline: float):
        self._stream, self._proc, self._reader, self._deadline = stream, proc, reader, deadline

    def _within(self, op, *args) -> bytes:
        got: list = []

        def run() -> None:
            try:
                got.append(op(*args))
            except BaseException as exc:  # handed to the caller below
                got.append(exc)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(self._deadline)
        if t.is_alive():
            self._proc.kill()
            t.join(1)  # the kill ends the pipe; let the read return before the stream closes
            # a child of the process that still holds stdout keeps the read (and the stream's lock) past
            # the kill: closing stdout would wait on it, so the reader lets go of the stream unclosed, and
            # the daemon thread ends when the pipe does
            self._reader.close(stdout=not t.is_alive())
            raise ReviewRecordError(f"git cat-file blob failed: answer truncated, "
                                    f"the batch process sent nothing for {self._deadline:g}s")
        if isinstance(got[0], BaseException):
            raise got[0]
        return got[0]

    def readline(self) -> bytes:
        return self._within(self._stream.readline)

    def read(self, n: int = -1) -> bytes:
        # the deadline bounds silence, not size: a big body reads in chunks, each under the deadline
        if n < 0:
            return self._within(self._stream.read, n)
        parts: list[bytes] = []
        while n > 0:
            part = self._within(self._stream.read1, min(n, 1 << 20))
            if not part:
                break
            parts.append(part)
            n -= len(part)
        return b"".join(parts)

    def close(self) -> None:
        self._stream.close()


class _Bounded:
    """The batch process as the reader sees it: ``stdout`` reads under the deadline, everything else
    is the process's own. The process is left untouched, so a stand-in for it keeps its own stdout."""

    def __init__(self, proc: subprocess.Popen, reader: "BatchReader", deadline: float):
        self._proc = proc
        self.stdout = _Deadlined(proc.stdout, proc, reader, deadline)

    def __getattr__(self, name: str):
        return getattr(self._proc, name)


def _blob_id(body: bytes, like: str) -> str:
    """The blob id of ``body`` in the hash of ``like`` (40 hex: sha1, 64: sha256)."""
    algo = hashlib.sha1 if len(like) == 40 else hashlib.sha256
    return algo(b"blob %d\0" % len(body) + body).hexdigest()


# no field of a review record comes near this; a header naming more is refused before its body is read,
# so a hostile or broken object cannot make the reader hold it in memory
MAX_BLOB_BYTES = 16 * 1024 * 1024


class BatchReader(GitReader):
    """Two git processes for a whole record: one ``ls-tree -r -t`` per root tree, indexed
    by tree id so every subtree it names is listed without another process, and one
    long-lived ``cat-file --batch`` for every blob. Close it (or use it as a context
    manager) to end the batch process."""

    def __init__(self, repo: Path):
        super().__init__(repo)
        self._trees: dict[str, list[Entry]] = {}
        self._cat: subprocess.Popen | None = None

    # local git answers a blob in milliseconds and streams it without pause: 30 s of silence is room
    # for a cold disk or a loaded runner, and a stall still ends the verb
    DEADLINE = 30.0

    @property
    def _cat(self) -> _Bounded | None:
        return self.__cat

    @_cat.setter
    def _cat(self, proc: subprocess.Popen | None) -> None:
        # every process the reader starts reads under the deadline, whichever line reads it
        self.__cat = None if proc is None else _Bounded(proc, self, self.DEADLINE)

    def ls(self, tree: str) -> list[Entry]:
        if tree not in self._trees:
            self._index(tree)
        return list(self._trees[tree])

    def _index(self, root: str) -> None:
        tree_at = {"": root}
        rows: list[tuple[str, Entry]] = []
        for line in _git(self.repo, "ls-tree", "-r", "-t", "-z", root, env=_local()).split(b"\0"):
            if not line:
                continue
            meta, raw = line.split(b"\t", 1)
            mode, kind, oid = meta.decode().split()
            parent, _, name = raw.decode("utf-8", errors="surrogateescape").rpartition("/")
            rows.append((parent, (mode, kind, oid, name)))
            if kind == "tree":
                tree_at[f"{parent}/{name}" if parent else name] = oid
        listed: dict[str, list[Entry]] = {path: [] for path in tree_at}
        for parent, entry in rows:
            listed[parent].append(entry)
        # One tree id at two paths has the same entries at both: either listing serves.
        for path, oid in tree_at.items():
            self._trees[oid] = listed[path]

    def blob(self, oid: str) -> bytes:
        # every caller asks by an id ls-tree named, so the header must echo it byte for byte
        if not _FULL_OID.fullmatch(oid):
            raise ReviewRecordError(f"git cat-file blob failed: {oid!r} is not a full object id")
        if self._cat is None:
            # a review read is local: an absent blob answers "missing" rather than fetching under the deadline
            self._cat = gitops.start(self.repo, "cat-file", "--batch", env=_local())
        self._cat.stdin.write(oid.encode() + b"\n")
        self._cat.stdin.flush()
        try:
            header = self._cat.stdout.readline().split()
        except BaseException:
            # the same as a body read: git may be blocked writing an answer a swallowed raise never reads
            if self._cat is not None:
                self._abandon()
            raise
        if len(header) == 3 and header[0] != oid.encode():
            self._abandon()  # an answer for another object, refused before any of its body is read
            raise ReviewRecordError(f"git cat-file blob failed: {oid} answered for another object")
        if len(header) == 2 and header[0] != oid.encode():
            self._abandon()  # "missing" for another object: the stream is out of step, not the object absent
            raise ReviewRecordError(f"git cat-file blob failed: {oid} answered for another object")
        if len(header) == 3 and header[1] != b"blob":
            # only a blob's body can be checked against the id asked, so another kind's is not read off the
            # stream: draining it would trust a size nothing verifies, and the next answer would start there
            self._abandon()
            raise ReviewRecordError(f"git cat-file blob failed: {oid} is "
                                    f"{header[1].decode(errors='replace')}, not a blob; its body is left unread, "
                                    f"so the stream is out of step")
        if len(header) == 3 and not header[2].isdigit():
            self._abandon()  # a size that is not ASCII digits: the header is not git's
            raise ReviewRecordError(f"git cat-file blob failed: {oid} answered with an unreadable size")
        remote = self._promisor() if header[1:] == [b"missing"] else None
        if remote is not None:
            raise ReviewRecordError(missing_locally(self.repo, "cat-file blob", oid))
        if len(header) != 3:
            if header[1:] != [b"missing"]:
                self._abandon()  # an empty or unreadable header: the stream is gone or out of step
            raise ReviewRecordError(f"git cat-file blob failed: {oid} is "
                                    f"{header[1].decode(errors='replace') if len(header) > 1 else 'unreadable'}, not a blob")
        size = int(header[2])
        if size > MAX_BLOB_BYTES:
            self._abandon()  # its body is left unread, so the stream is out of step
            raise ReviewRecordError(f"git cat-file blob failed: {oid} is {size} bytes, over the {MAX_BLOB_BYTES} byte limit")
        try:
            data = self._cat.stdout.read(size + 1)
        except BaseException:
            # a caller may swallow this raise and close() cleanly: end git here, while it may be blocked writing
            if self._cat is not None:
                self._abandon()
            raise
        body = data[:size]
        # no length or newline check proves where an answer ends: only the body hashing to the id asked does
        if data[size:] != b"\n" or _blob_id(body, oid) != oid:
            self._abandon()  # nothing after it can be trusted
            raise ReviewRecordError(f"git cat-file blob failed: {oid} truncated, or answered with another object's bytes")
        return body

    def _abandon(self) -> None:
        # kill before close: git may be blocked writing the rest of an overlong body into a full pipe,
        # and close() waits for it to exit
        self._cat.kill()
        self.close()

    def _promisor(self) -> str | None:
        return _promisor(self.repo)

    def __exit__(self, kind, *exc) -> None:
        # a raise can land with a body still on the pipe: git blocks writing it, and close() would wait forever
        if kind is not None and self._cat is not None:
            self._abandon()
        else:
            self.close()

    def close(self, stdout: bool = True) -> None:
        if self._cat is not None:
            self._cat.stdin.close()
            self._cat.wait()
            if stdout:
                self._cat.stdout.close()
            self._cat = None


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


def _occurrences(repo: Path, name: str, oid: str, reader: GitReader | None = None) -> list[Entry]:
    """A repeated entry's occurrences, which must be exactly 0000001..n in order."""
    kids = (reader or GitReader(repo)).ls(oid)
    want = [f"{i:07d}" for i in range(1, len(kids) + 1)]
    if not kids or [k[3] for k in kids] != want:
        raise ReviewRecordError(f"{name}: occurrences must be exactly 0000001..n, got {[k[3] for k in kids]}")
    return kids


def write_tree(repo: Path, buf: bytes, message: str = "ReviewBind", schema: Schema | None = None) -> str:
    """Store protobuf bytes as a field tree, names from ``schema`` (default: the review schema)."""
    spec = _spec(schema, message)
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
                return _TREE_MODE, "tree", write_tree(repo, p, f.type, schema)
            return _BLOB_MODE, "blob", _blob(repo, p)

        repeated = (f.repeated if f is not None else False) or len(payloads) > 1
        if repeated:
            occ = [(*leaf(p), f"{i:07d}") for i, p in enumerate(payloads, start=1)]
            entries.append((_TREE_MODE, "tree", _mktree(repo, occ), entry_name(f, number, wt, True)))
        else:
            entries.append((*leaf(payloads[0]), entry_name(f, number, wt, False)))
    return _mktree(repo, entries)


def write_record(repo: Path, record: dict, message: str = "ReviewBind", schema: Schema | None = None) -> str:
    return write_tree(repo, encode(record, message, schema), message, schema)


def to_protobuf(repo: Path, tree: str, reader: GitReader | None = None) -> bytes:
    """Derive protobuf from a stored tree, by number, in entry order. Needs no schema:
    a tree is a message, a blob is a payload. Unknown entries are re-emitted too.
    ``reader`` fetches the objects (default: one git process per object)."""
    reader = reader or GitReader(repo)
    out = b""
    for mode, kind, oid, name in reader.ls(tree):
        number, wt, repeated = parse_entry(name)
        kids = _occurrences(repo, name, oid, reader) if repeated else [(mode, kind, oid, name)]
        for _, k, o, _ in kids:
            payload = to_protobuf(repo, o, reader) if k == "tree" else reader.blob(o)
            check_payload(wt, payload)
            out += _emit(number, wt, payload)
    return out


def read_record(repo: Path, tree: str, message: str = "ReviewBind", schema: Schema | None = None,
                reader: GitReader | None = None) -> dict:
    """The known fields, as a VIEW. Never write this dict back as the record: it does not
    carry unknown entries, so write_record(read_record(t)) loses them. Edit a stored
    record with replace_entry."""
    return decode(to_protobuf(repo, tree, reader), message, schema)


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


def _check_message(repo: Path, tree: str, message: str | None, schema: Schema | None,
                   reader: GitReader) -> None:
    spec = _spec(schema, message) if message else {}
    seen: set[int] = set()
    for mode, kind, oid, name in reader.ls(tree):
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
            kids = _occurrences(repo, name, oid, reader)
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
                _check_message(repo, o, f.type if f is not None else None, schema, reader)


_OID = re.compile(r"\b[0-9a-f]{40}(?:[0-9a-f]{24})?\b")
_OID_PATH = re.compile(r"objects/([0-9a-f]{2})/([0-9a-f]{38}(?:[0-9a-f]{24})?)\b")


def _fsck_names_record(repo: Path, tree: str, stderr: str) -> bool:
    """fsck reads every object in the store, so an object the record never reaches (a blob a rewritten
    commit left behind, until reflog expire and gc) still fails it. Refuse when any error line names an
    object reachable from `tree`, or names no object at all (a line this reader cannot place). A
    hash-path mismatch names the object it was asked for only by its loose path (objects/xx/yyyy), and
    leads with the hash of the bytes it found, so the path is read as an object name too."""
    lines = [ln for ln in stderr.splitlines() if ln.strip()]
    if not lines:
        return True
    walk = gitops.run(repo, "rev-list", "--objects", "--no-object-names", tree, binary=True, env=_local())
    if walk.returncode != 0:
        return True
    reachable = set(walk.stdout.decode(errors="replace").split())
    for ln in lines:
        oids = _OID.findall(ln) + [a + b for a, b in _OID_PATH.findall(ln)]
        if not oids or reachable.intersection(oids):
            return True
    return False


def verify_tree(repo: Path, tree: str, message: str = "ReviewBind", schema: Schema | None = None,
                reader: GitReader | None = None) -> None:
    """Check the record AS WRITTEN: every object re-hashes from its own content
    (``git fsck --strict``); every entry is a plain blob or tree with a field tree name,
    one per field number, its wire type and repetition agreeing with any field the
    schema knows; occurrences run 0000001..n; and the record decodes by number. Names
    are never regenerated from the schema and compared. This checks a tree, not WHICH
    tree: binding a record to its identity is the ref writer's job."""
    proc = gitops.run(repo, "fsck", "--strict", "--no-dangling", tree, binary=True, env=_local())
    if proc.returncode != 0 and _fsck_names_record(repo, tree, proc.stderr.decode(errors="replace")):
        raise ReviewRecordError(f"fsck refused record {tree}: {proc.stderr.decode(errors='replace').strip()[:200]}")
    reader = reader or GitReader(repo)
    _check_message(repo, tree, message, schema, reader)
    decode(to_protobuf(repo, tree, reader), message, schema)
