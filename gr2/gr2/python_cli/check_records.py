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

CHECK_REF = "refs/dev.synapt.grip/__checks__/v1"


class CheckRefused(ValueError):
    pass


def _git(repo: Path, *args: str, data: bytes | None = None) -> bytes:
    p = subprocess.run(["git", "-C", str(repo), *args], input=data, capture_output=True)
    if p.returncode:
        raise CheckRefused(p.stderr.decode("utf-8", errors="replace").strip())
    return p.stdout


def _oid(repo: Path, value: str) -> str:
    size = 64 if _git(repo, "rev-parse", "--show-object-format").strip() == b"sha256" else 40
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


def _snapshot(repo: Path, remote: str) -> str | None:
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
        return _oid(repo, fetched)
    finally:
        if fetched:
            _git(repo, "update-ref", "-d", staging, fetched)


def _entries(repo: Path, snapshot: str | None) -> dict[str, str]:
    if snapshot is None:
        return {}
    entries = {}
    # Refuse unexpected tree shapes, including executable or symbolic-link blobs.
    for raw in _git(repo, "ls-tree", "-r", "-z", snapshot).split(b"\0"):
        if not raw:
            continue
        metadata, name = raw.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        path = name.decode("ascii")
        head = path.replace("/", "")
        if mode != "100644" or kind != "blob" or path != head[:2] + "/" + head[2:]:
            raise CheckRefused("invalid_checks_tree")
        _oid(repo, head)
        _records(_git(repo, "cat-file", "blob", oid), head)
        entries[path] = oid
    return entries


def read_remote_check(remote: str, member: Mapping, head: str,
                      required_names: Sequence[str] = ("test",)) -> dict:
    """member has path, key and remote; transport/format faults return fail, never absent."""
    result = dict(status="fail", record_id=None, snapshot_oid=None, head=head,
                  member_key=member.get("key"), records=[], reason="cannot_measure_check_ref")
    try:
        repo = Path(member["path"])
        if member.get("remote") != remote:
            raise CheckRefused("member_remote_mismatch")
        _oid(repo, head)
        if not required_names or any(not isinstance(n, str) or not n.strip() for n in required_names):
            raise CheckRefused("invalid_required_checks")
        snapshot = _snapshot(repo, remote)
        result["snapshot_oid"] = snapshot
        entries = _entries(repo, snapshot)
        blob = entries.get(head[:2] + "/" + head[2:])
        result["record_id"] = blob
        rows = _records(_git(repo, "cat-file", "blob", blob), head) if blob else []
        result["records"] = rows
        required = [r for r in rows if r["name"] in required_names]
        if any(r["result"] != "pass" for r in required):
            result.update(status="fail", reason="required_check_failed")
        elif any(not any(r["name"] == name for r in rows) for name in required_names):
            result.update(status="absent", reason="required_check_absent")
        else:
            result.update(status="pass", reason="required_checks_pass")
    except (CheckRefused, ValueError, KeyError, UnicodeError, TypeError) as exc:
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
        subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", "--no-checkout", str(repo), str(checkout)],
                       check=True, capture_output=True)
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
    snapshot = _snapshot(repo, remote)
    entries = _entries(repo, snapshot)
    path = head[:2] + "/" + head[2:]
    old_blob = entries.get(path)
    old_lines = _git(repo, "cat-file", "blob", old_blob).splitlines() if old_blob else []
    blob = b"\n".join(sorted(set(old_lines + [line]))) + b"\n"
    entries[path] = _git(repo, "hash-object", "-w", "--stdin", data=blob).decode().strip()
    trees = {}
    for key, oid in sorted(entries.items()):
        prefix, suffix = key.split("/")
        trees.setdefault(prefix, []).append(f"100644 blob {oid}\t{suffix}\n")
    root = []
    for prefix, leaves in sorted(trees.items()):
        oid = _git(repo, "mktree", data="".join(leaves).encode()).decode().strip()
        root.append(f"040000 tree {oid}\t{prefix}\n")
    tree = _git(repo, "mktree", data="".join(root).encode()).decode().strip()
    parents = ["-p", snapshot] if snapshot else []
    commit = _git(repo, "commit-tree", tree, *parents, data=b"Record exact-head check\n").decode().strip()
    _entries(repo, commit)  # Verify before either local or remote publication.
    current = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", CHECK_REF], capture_output=True)
    expected = current.stdout.decode().strip() if current.returncode == 0 else "0" * len(head)
    if current.returncode == 0 and expected != snapshot:
        raise CheckRefused("local_checks_diverged")
    _git(repo, "update-ref", CHECK_REF, commit, expected)
    # A remote race refuses without overwriting the winner. Union retry is a later hardening step.
    _git(repo, "push", f"--force-with-lease={CHECK_REF}:{snapshot or ''}", remote, f"{commit}:{CHECK_REF}")
    observed = read_remote_check(remote, {"path": repo, "key": "repo", "remote": remote}, head, (name,))
    identity = hashlib.sha256(line).hexdigest()
    if not any(r["observation_id"] == identity for r in observed["records"]):
        raise CheckRefused("check_publication_unconfirmed")
    return {"ref": CHECK_REF, "head": head, "snapshot_oid": observed["snapshot_oid"],
            "record_id": observed["record_id"], "observation": {**row, "observation_id": identity}}
