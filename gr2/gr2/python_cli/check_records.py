"""Exact-head check observations transported through an ordinary Git remote."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Mapping, Sequence

from . import gitops
from .review_field_tree import BatchReader, ReviewRecordError

CHECK_REF = "refs/dev.synapt.grip/__checks__/v1"

# Required read-adapter keys. Unknown observation metadata is preserved but
# does not become a closed schema promise in the public surface dump.
JSON_SHAPES = {"check show": (
    ".status  enum(pass,fail,absent)", ".record_id", ".snapshot_oid", ".head",
    ".member_key", ".records", ".records[].v  enum(1)", ".records[].head",
    ".records[].observed_head", ".records[].name",
    ".records[].result  enum(pass,fail,error)", ".records[].exit_code",
    ".records[].observation_id", ".reason",
)}


class CheckRefused(ValueError):
    pass


def _git(repo: Path, *args: str, data: bytes | None = None) -> bytes:
    return gitops.out_bytes(repo, *args, input=data, error=CheckRefused, timeout=30, raise_timeout=True)


class _Store:
    """One repository's reads of the check store for one invocation: the object format asked once and every blob
    read through one ``cat-file --batch``."""

    def __init__(self, repo: Path):
        self.repo = Path(repo)
        self._size: int | None = None
        self._reader = BatchReader(self.repo)

    def size(self) -> int:
        if self._size is None:
            self._size = _size(self.repo)
        return self._size

    def blob(self, oid: str) -> bytes:
        try:
            return self._reader.blob(oid)
        except ReviewRecordError as exc:
            raise CheckRefused(str(exc)) from exc

    def close(self) -> None:
        self._reader.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _size(repo: Path) -> int:
    return 64 if _git(repo, "rev-parse", "--show-object-format").strip() == b"sha256" else 40


def _oid(repo: Path, value: str, store: _Store | None = None) -> str:
    size = store.size() if store else _size(repo)
    if not re.fullmatch(r"[0-9a-f]{%d}" % size, value):
        raise CheckRefused("head_must_be_full_commit_id")
    if _git(repo, "cat-file", "-t", value).strip() != b"commit":
        raise CheckRefused("head_must_be_commit")
    return value


def _remote(remote: str) -> str:
    if not (remote.startswith("https://") or Path(remote).is_absolute()) or remote.startswith("-"):
        raise CheckRefused("remote_must_be_https_or_absolute_path")
    return remote


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CheckRefused("duplicate_record_field")
        result[key] = value
    return result


def _line(row: dict) -> bytes:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _records(blob: bytes, head: str) -> list[dict]:
    if not blob or not blob.endswith(b"\n"):
        raise CheckRefused("invalid_check_blob")
    lines = blob.splitlines()
    if lines != sorted(set(lines)):
        raise CheckRefused("check_lines_not_sorted_unique")
    rows = []
    for line in lines:
        row = json.loads(line.decode("utf-8"), object_pairs_hook=_pairs)
        if not isinstance(row, dict) or _line(row) != line:
            raise CheckRefused("noncanonical_check_record")
        if (type(row.get("v")) is not int or row["v"] != 1 or row.get("head") != head
                or not isinstance(row.get("name"), str) or not row["name"].strip()
                or row.get("result") not in ("pass", "fail", "error")
                or type(row.get("exit_code")) is not int
                or row.get("observed_head") != head
                or (row["result"] == "pass") != (row["exit_code"] == 0)):
            raise CheckRefused("invalid_check_record")
        rows.append({**row, "observation_id": hashlib.sha256(line).hexdigest()})
    return rows


def _snapshot(repo: Path, remote: str, store: _Store | None = None) -> str | None:
    answer = _git(repo, "ls-remote", "--refs", _remote(remote), CHECK_REF).decode().splitlines()
    if not answer:
        return None
    if len(answer) != 1 or len(answer[0].split()) != 2 or answer[0].split()[1] != CHECK_REF:
        raise CheckRefused("cannot_measure_check_ref")
    staging = f"refs/dev.synapt.grip/__check_transfers__/{uuid.uuid4().hex}"
    fetched = None
    try:
        _git(repo, "fetch", "--no-tags", "--no-write-fetch-head", remote, f"{CHECK_REF}:{staging}")
        fetched = _git(repo, "rev-parse", "--verify", staging).decode().strip()
        return _oid(repo, fetched, store)
    finally:
        if fetched:
            _git(repo, "update-ref", "-d", staging, fetched)


def _entries(repo: Path, snapshot: str | None, store: _Store | None = None) -> dict[str, str]:
    if snapshot is None:
        return {}
    if store is None:
        with _Store(repo) as own:
            return _entries(repo, snapshot, own)
    entries = {}
    seen = set()
    fanouts = []
    # Refuse unexpected tree shapes, including executable or symbolic-link blobs.
    for raw in _git(repo, "ls-tree", "-r", "-t", "-z", snapshot).split(b"\0"):
        if not raw:
            continue
        metadata, name = raw.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        path = name.decode("ascii")
        if path in seen:
            raise CheckRefused("duplicate_checks_tree_path")
        seen.add(path)
        if kind == "tree":
            if mode != "040000" or not re.fullmatch(r"[0-9a-f]{2}", path):
                raise CheckRefused("invalid_checks_tree")
            fanouts.append(path)
            continue
        head = path.replace("/", "")
        if not re.fullmatch(r"[0-9a-f]{%d}" % store.size(), head):
            raise CheckRefused("invalid_checks_head")
        if mode != "100644" or kind != "blob" or path != head[:2] + "/" + head[2:]:
            raise CheckRefused("invalid_checks_tree")
        _records(store.blob(oid), head)
        entries[path] = oid
    # A fanout tree lists its leaves under its own path; one with none is empty.
    if set(fanouts) - {path.split("/", 1)[0] for path in entries}:
        raise CheckRefused("empty_checks_fanout")
    return entries


def read_remote_check(remote: str, member: Mapping, head: str,
                      required_names: Sequence[str] = ("test",), store: _Store | None = None) -> dict:
    """member has path, key and remote; transport/format faults return fail, never absent."""
    if store is None:
        try:
            own = _Store(Path(member["path"]))
        except (KeyError, TypeError):
            own = None
        if own is not None:
            with own:
                return _read_remote_check(remote, member, head, required_names, own)
    return _read_remote_check(remote, member, head, required_names, store)


def _read_remote_check(remote: str, member: Mapping, head: str, required_names: Sequence[str],
                       store: _Store | None) -> dict:
    result = dict(status="fail", record_id=None, snapshot_oid=None, head=head,
                  member_key=member.get("key"), records=[], reason="cannot_measure_check_ref")
    try:
        repo = Path(member["path"])
        if member.get("remote") != remote:
            raise CheckRefused("member_remote_mismatch")
        _oid(repo, head, store)
        if not required_names or any(not isinstance(n, str) or not n.strip() for n in required_names):
            raise CheckRefused("invalid_required_checks")
        snapshot = _snapshot(repo, remote, store)
        result["snapshot_oid"] = snapshot
        entries = _entries(repo, snapshot, store)
        blob = entries.get(head[:2] + "/" + head[2:])
        result["record_id"] = blob
        rows = _records(store.blob(blob) if store else _git(repo, "cat-file", "blob", blob), head) if blob else []
        result["records"] = rows
        required = [r for r in rows if r["name"] in required_names]
        if any(r["result"] != "pass" for r in required):
            result.update(status="fail", reason="required_check_failed")
        elif any(not any(r["name"] == name for r in rows) for name in required_names):
            result.update(status="absent", reason="required_check_absent")
        else:
            result.update(status="pass", reason="required_checks_pass")
    except (CheckRefused, ValueError, KeyError, UnicodeError, TypeError, OSError, subprocess.SubprocessError) as exc:
        result["reason"] = str(exc)
    return result


def run_check(repo: Path, remote: str, head: str, name: str, argv: Sequence[str]) -> dict:
    """Run at an isolated checkout, then publish a verified observation using CAS."""
    repo = repo.resolve()
    _oid(repo, head)
    _remote(remote)
    if not name.strip() or not argv:
        raise CheckRefused("check_name_and_command_required")
    with tempfile.TemporaryDirectory(prefix="gr2-check-") as directory:
        checkout = Path(directory) / "checkout"
        cloned = gitops.clone("--quiet", "--no-hardlinks", "--no-checkout", str(repo), str(checkout), binary=True)
        if cloned.returncode:
            gitops._raise(cloned, ("clone",), CheckRefused)
        _git(checkout, "checkout", "--quiet", "--detach", head)
        before = _git(checkout, "rev-parse", "HEAD").decode().strip()
        execution = subprocess.run(list(argv), cwd=checkout, capture_output=True)
        after = _git(checkout, "rev-parse", "HEAD").decode().strip()
        if before != head or after != head:
            raise CheckRefused("check_execution_head_changed")
    row = dict(v=1, head=head, observed_head=after, name=name,
               result="pass" if execution.returncode == 0 else "fail", exit_code=execution.returncode)
    line = _line(row)
    _records(line + b"\n", head)
    return publish_observation(repo, remote, row)


def _local_snapshot(repo: Path, store: _Store | None = None) -> str | None:
    p = gitops.run(repo, "show-ref", "--verify", "--quiet", CHECK_REF, binary=True, timeout=30, raise_timeout=True)
    if p.returncode == 1:
        return None
    if p.returncode:
        raise CheckRefused("cannot_measure_local_checks")
    return _oid(repo, _git(repo, "rev-parse", "--verify", CHECK_REF).decode().strip(), store)


def _union(repo: Path, snapshots: Sequence[str | None], head: str, line: bytes,
           store: _Store | None = None) -> dict[str, str]:
    if store is None:
        with _Store(repo) as own:
            return _union(repo, snapshots, head, line, own)
    # A path whose snapshots all name one blob keeps that blob: a stored blob is already its sorted unique lines,
    # so rewriting it would hash the same bytes. Only a path the snapshots disagree on, or the new line, is read.
    named: dict[str, set[str]] = {}
    for snapshot in dict.fromkeys(snapshots):
        for path, oid in _entries(repo, snapshot, store).items():
            named.setdefault(path, set()).add(oid)
    key = head[:2] + "/" + head[2:]
    named.setdefault(key, set())
    result = {}
    for path, oids in sorted(named.items()):
        if path != key and len(oids) == 1:
            result[path] = next(iter(oids))
            continue
        lines: set[bytes] = set()
        for oid in sorted(oids):
            lines.update(store.blob(oid).splitlines())
        if path == key:
            lines.add(line)
        result[path] = _git(repo, "hash-object", "-w", "--stdin",
                            data=b"\n".join(sorted(lines)) + b"\n").decode().strip()
    return result


def _commit_union(repo: Path, entries: Mapping[str, str], parents: Sequence[str | None],
                  store: _Store | None = None) -> str:
    trees = {}
    for key, oid in sorted(entries.items()):
        prefix, suffix = key.split("/")
        trees.setdefault(prefix, []).append(f"100644 blob {oid}\t{suffix}\n")
    # Every fanout tree from one `mktree --batch`: a blank line ends each tree, one id is printed per tree.
    batch = "\n".join("".join(leaves) for leaves in trees.values()).encode() if trees else b""
    made = _git(repo, "mktree", "--batch", data=batch).decode().split() if trees else []
    if len(made) != len(trees):
        raise CheckRefused("checks_tree_write_failed")
    root = [f"040000 tree {oid}\t{prefix}\n" for prefix, oid in zip(trees, made)]
    tree = _git(repo, "mktree", data="".join(root).encode()).decode().strip()
    arguments = [arg for parent in dict.fromkeys(p for p in parents if p) for arg in ("-p", parent)]
    commit = _git(repo, "commit-tree", tree, *arguments, data=b"Record exact-head check\n").decode().strip()
    _entries(repo, commit, store)
    return commit


def publish_observation(repo: Path, remote: str, row: dict) -> dict:
    """Storage seam for a validated executor receipt, not a manual-result CLI."""
    head = _oid(repo, row["head"])
    _remote(remote)
    line = _line(row)
    _records(line + b"\n", head)
    identity = hashlib.sha256(line).hexdigest()
    with _Store(repo) as store:
        return _publish(repo, remote, row, head, line, identity, store)


def _publish(repo: Path, remote: str, row: dict, head: str, line: bytes, identity: str, store: _Store) -> dict:
    attempted = None
    last_error = "checks_conflict_exhausted"
    for _attempt in range(4):
        snapshot = _snapshot(repo, remote, store)
        local = _local_snapshot(repo, store)
        entries = _union(repo, (snapshot, local, attempted), head, line, store)
        commit = _commit_union(repo, entries, (snapshot, local, attempted), store)
        try:
            _git(repo, "update-ref", CHECK_REF, commit, local or "0" * len(head))
        except CheckRefused:
            attempted = commit
            continue  # Re-read local CAS winner and retain both histories.
        attempted = commit
        try:
            _git(repo, "push", f"--force-with-lease={CHECK_REF}:{snapshot or ''}", remote,
                 f"{commit}:{CHECK_REF}")
        except (CheckRefused, OSError, subprocess.SubprocessError) as exc:
            last_error = str(exc)
        # A failed acknowledgement can follow an applied push. A fresh read decides.
        observed = read_remote_check(remote, {"path": repo, "key": "repo", "remote": remote}, head,
                                     (row["name"],), store)
        if any(r["observation_id"] == identity for r in observed["records"]):
            return {"ref": CHECK_REF, "head": head, "snapshot_oid": observed["snapshot_oid"],
                    "record_id": observed["record_id"], "observation": {**row, "observation_id": identity}}
        if observed["status"] == "fail" and observed["reason"] != "required_check_failed":
            raise CheckRefused("check_publication_indeterminate: " + observed["reason"])
        # Confirmed missing observation: merge the new remote winner on the next attempt.
    raise CheckRefused("checks_conflict_exhausted: " + last_error)
