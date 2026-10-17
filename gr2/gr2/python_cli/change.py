"""`gr2 change`: one change across members in one command.

Phase 0 reads every member and writes nothing; any refusal stops the verb
before the first write, so a refused run leaves every member as it was.
Phase 1 then writes per member, in the order given: branch, stage, commit.
Two git calls per member to read, four to write.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .gitops import git


class ChangeRefused(Exception):
    def __init__(self, reason: str, member: str, observed: str) -> None:
        super().__init__(f"{reason}: {member}: {observed}")
        self.reason = reason
        self.member = member
        self.observed = observed


class ChangeFailed(Exception):
    pass


@dataclass
class Member:
    name: str
    path: Path
    changed: bool = False
    entries: list[str] = field(default_factory=list)
    head: str = ""


def _status_entries(path: Path) -> tuple[str, list[str]]:
    proc = git(path, "status", "--porcelain=v2", "--branch", "-z")
    if proc.returncode != 0:
        raise ChangeRefused("not_a_repo", path.name, proc.stderr.strip() or "git status failed")
    branch = ""
    entries: list[str] = []
    records = proc.stdout.split("\0")
    i = 0
    while i < len(records):
        rec = records[i]
        if rec.startswith("# branch.head "):
            branch = rec[len("# branch.head "):]
        elif rec.startswith("2 "):
            # A rename record carries its original path as the next NUL field.
            entries.append(rec)
            i += 1
        elif rec and not rec.startswith("#"):
            entries.append(rec)
        i += 1
    return branch, entries


def preflight(paths: list[Path], branch: str) -> list[Member]:
    """Read every member and refuse before anything is written."""
    members: list[Member] = []
    for path in paths:
        member = Member(name=path.name, path=path)
        head_branch, entries = _status_entries(path)
        if head_branch == "(detached)":
            raise ChangeRefused("detached_head", member.name, "HEAD is detached")
        for rec in entries:
            if rec.startswith("u "):
                raise ChangeRefused("unresolved", member.name, "unmerged paths")
            if rec[0] in "12" and rec[2] != ".":
                raise ChangeRefused("staged_index", member.name, "the index already differs from HEAD")
        exists = git(path, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        if exists.returncode == 0:
            raise ChangeRefused("branch_exists", member.name, f"{branch} at {exists.stdout.strip()[:7]}")
        member.entries = entries
        member.changed = bool(entries)
        members.append(member)
    if not any(m.changed for m in members):
        raise ChangeRefused("nothing_changed", ",".join(m.name for m in members), "no member has a change")
    return members


def write(members: list[Member], branch: str, message: str) -> None:
    for member in members:
        if not member.changed:
            continue
        for args in (
            ("checkout", "-q", "-b", branch),
            ("add", "-A"),
            ("commit", "-q", "-m", message),
        ):
            proc = git(member.path, *args)
            if proc.returncode != 0:
                raise ChangeFailed(f"{member.name}: git {args[0]} failed: {proc.stderr.strip()}")
        proc = git(member.path, "rev-parse", "HEAD")
        member.head = proc.stdout.strip()


def change(paths: list[Path], branch: str, message: str) -> list[Member]:
    members = preflight(paths, branch)
    write(members, branch, message)
    return members
