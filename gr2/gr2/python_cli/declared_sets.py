"""Declared sets: which members of a store must be merged together, named once in the tracked `grip.toml`.

    [[sets]]
    name = "core"
    members = ["alpha", "beta", "gamma"]

A review is judged one member at a time, so five single-member reviews of one change never form a multi-member review
and the merge gate had nothing to require a joint check of. A declared set is the coupling written down where every
workspace of the store reads the same bytes, and it cannot be left out by whoever splits the reviews.

The gate reads the MERGING workspace's committed `grip.toml` (HEAD), not its working file and not any history: a
member of a declared set merges only when a `declared_set` record, written by `check set --root-commit`, names a root
commit that pins EVERY member of the set at heads that include this review's head. The record stores the member list
it was made for, and an edited declaration does not ride an old record.
"""
from __future__ import annotations

import posixpath
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from . import gitops

DECLARED_SET_CHECK = "declared_set"
_HEX40 = re.compile(r"[0-9a-f]{40}")


class DeclaredSetError(ValueError):
    """A declaration that cannot be read, named so the author can fix grip.toml."""


@dataclass(frozen=True)
class DeclaredSet:
    name: str
    members: tuple[str, ...]  # sorted member names


@dataclass(frozen=True)
class Declaration:
    sets: tuple[DeclaredSet, ...]
    members: Mapping[str, Mapping[str, str]]  # name -> {"path": ..., "remote": ...}

    def set_named(self, name: str) -> DeclaredSet | None:
        return next((s for s in self.sets if s.name == name), None)

    def member_name(self, key: str, path: str) -> str | None:
        """The store member a review member is: its key is a member's name, or its path is a member's path."""
        wanted = posixpath.normpath(path) if path else path
        for name, member in self.members.items():
            if name == key or member["path"] == wanted:
                return name
        return None

    def set_of(self, member: str) -> DeclaredSet | None:
        return next((s for s in self.sets if member in s.members), None)


def parse(document: Mapping) -> Declaration:
    """The declaration in a parsed grip.toml document. Refuses at read: a set naming an unknown member, a member in
    two sets (which would leave it unclear which record is required), a repeated or empty set, a malformed entry."""
    members: dict[str, dict[str, str]] = {}
    for member in document.get("members", []) or []:
        remotes = member.get("remotes", {}) if isinstance(member, Mapping) else {}
        if not isinstance(member, Mapping) or not isinstance(member.get("name"), str) or not isinstance(member.get("path"), str):
            continue  # the member reader owns the refusal for a malformed member
        origin = remotes.get("origin") if isinstance(remotes, Mapping) else None
        members[member["name"]] = {"path": posixpath.normpath(member["path"]), "remote": origin if isinstance(origin, str) else ""}
    raw = document.get("sets", [])
    if raw in (None, []):
        return Declaration((), members)
    if not isinstance(raw, list):
        raise DeclaredSetError("sets must be an array of [[sets]] tables")
    sets: list[DeclaredSet] = []
    owner: dict[str, str] = {}
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, Mapping):
            raise DeclaredSetError(f"[[sets]] entry {index} is not a table")
        name = entry.get("name")
        listed = entry.get("members")
        if not isinstance(name, str) or not name.strip():
            raise DeclaredSetError(f"[[sets]] entry {index} needs a name")
        if any(s.name == name for s in sets):
            raise DeclaredSetError(f"set {name!r} is declared twice")
        if not isinstance(listed, list) or not listed or any(not isinstance(m, str) or not m for m in listed):
            raise DeclaredSetError(f"set {name!r} needs members, a list of member names")
        if len(set(listed)) != len(listed):
            raise DeclaredSetError(f"set {name!r} lists a member twice")
        for member in listed:
            if member not in members:
                raise DeclaredSetError(f"set {name!r} names unknown member {member!r}")
            if member in owner:
                raise DeclaredSetError(
                    f"member {member!r} is in two sets ({owner[member]!r} and {name!r}); "
                    "a member belongs to at most one set"
                )
            owner[member] = name
        sets.append(DeclaredSet(name, tuple(sorted(listed))))
    return Declaration(tuple(sets), members)


def _blob(root: Path, revision: str) -> str | None:
    """The committed grip.toml at `revision`, or None when there is no such commit or no document in it."""
    found = gitops.run(root, "rev-parse", "--verify", "-q", f"{revision}^{{commit}}")
    if found.returncode:
        return None
    shown = gitops.run(root, "show", f"{revision}:grip.toml")
    return shown.stdout if shown.returncode == 0 else None


def read(root: Path, revision: str = "HEAD") -> Declaration:
    """The declaration committed at `revision` (HEAD by default). No commit or no grip.toml means no sets."""
    text = _blob(Path(root), revision)
    if text is None:
        return Declaration((), {})
    try:
        return parse(tomllib.loads(text))
    except tomllib.TOMLDecodeError as exc:
        raise DeclaredSetError(f"grip.toml at {revision} is not valid TOML: {exc}") from exc


def gitlinks(root: Path, revision: str, declaration: Declaration, dset: DeclaredSet) -> tuple[dict[str, str], list[str]]:
    """({member: gitlink sha}, [members the commit does not pin]) for every member of `dset` at `revision`."""
    pins: dict[str, str] = {}
    missing: list[str] = []
    for name in dset.members:
        path = declaration.members[name]["path"]
        entry = gitops.run(Path(root), "ls-tree", revision, "--", path).stdout.split()
        if len(entry) >= 3 and entry[0] == "160000" and _HEX40.fullmatch(entry[2]):
            pins[name] = entry[2]
        else:
            missing.append(name)
    return pins, missing


def judge(root: Path, declaration: Declaration, dset: DeclaredSet, member: str, head: str,
          rows: Sequence[Mapping]) -> str | None:
    """None when a `declared_set` record covers this member at this head, else the named refusal.

    Judged against the workspace, never against the record's own claims: the root commit must exist here, pin every
    member of the set as it is declared NOW, and pin this review's member at this head."""
    named = [r for r in rows if r.get("name") == DECLARED_SET_CHECK and r.get("set_name") == dset.name]
    if not named:
        return (f"declared_set_absent: {member} belongs to set {dset.name!r}, which needs one record over all of "
                f"{list(dset.members)}; commit the workspace with every pin, run `check set --root-commit <sha>`, then merge")
    by_commit: dict[str, list[Mapping]] = {}
    for row in named:
        by_commit.setdefault(str(row.get("root_commit")), []).append(row)
    # A failure belongs to the PINS it was run over, not to the commit id that carried them: an empty commit gives the
    # same pins a new id, and a pass there must not outvote the failure (the per-member `test` check is sticky the same
    # way). A failure at pins that have since changed is outvotable: that is fixing the code and running the check again.
    failed_pins = {_pins_of(r) for r in named if r.get("result") != "pass"}
    reasons: list[str | None] = []
    for commit, group in by_commit.items():
        if any(r.get("result") != "pass" for r in group):
            reasons.append(f"declared_set_fail: the set check for {dset.name!r} failed at root commit {commit[:12]}")
        elif _pins_of(group[0]) in failed_pins:
            reasons.append(f"declared_set_fail: the set check for {dset.name!r} failed over these same pins at another "
                           f"root commit; a pass at {commit[:12]} does not outvote it, change a pin and run it again")
        else:
            reasons.append(_judge_one(Path(root), declaration, dset, member, head, group[0]))
    return None if None in reasons else reasons[0]


def _pins_of(row: Mapping) -> tuple | None:
    """The head list a record was made over, as a comparable key (None when the record carries no readable list)."""
    listed = row.get("heads")
    return tuple(listed) if isinstance(listed, list) else None


def _judge_one(root: Path, declaration: Declaration, dset: DeclaredSet, member: str, head: str, row: Mapping) -> str | None:
    recorded = row.get("declared_members")
    if not isinstance(recorded, list) or sorted(recorded) != list(dset.members):
        return (f"declared_set_changed: set {dset.name!r} is declared as {list(dset.members)} now and the record "
                f"was made for {recorded}; run `check set --root-commit` again")
    commit = row.get("root_commit")
    if not isinstance(commit, str) or not _HEX40.fullmatch(commit) \
            or gitops.run(root, "cat-file", "-e", f"{commit}^{{commit}}").returncode:
        return f"declared_set_root_unknown: root commit {commit!r} is not in this workspace"
    pins, missing = gitlinks(root, commit, declaration, dset)
    if missing:
        return f"declared_set_member_unpinned: root commit {commit[:12]} does not pin {missing} of set {dset.name!r}"
    listed = row.get("heads")
    if listed != sorted(f"{name}:{sha}" for name, sha in pins.items()):
        return f"declared_set_record_disagrees: the record's heads are not what root commit {commit[:12]} pins"
    if pins[member] != head:
        return (f"declared_set_head_mismatch: root commit {commit[:12]} pins {member} at {pins[member]}, "
                f"this review is at {head}")
    return None
