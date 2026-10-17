"""`gr2 change`: one change across members in one command.

Phase 0 reads every member and writes nothing; any refusal stops the verb
before the first write, so a refused run leaves every member as it was.
Phase 1 then writes per member, in the order given: branch, stage, commit.
Two git calls per member to read, four to write.

A write that fails (a pre-commit hook refusing, say) is undone in every member
already touched, in reverse order: HEAD back to the original branch, the index
back to its HEAD, the new branch deleted. Three git calls per member, and none
of them touch the worktree, so the user's uncommitted change is still there.
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


class ChangeUndone(ChangeRefused):
    """A write failed and every member written was put back as it was."""

    def __init__(self, reason: str, member: str, observed: str, undone: list[str]) -> None:
        super().__init__(reason, member, observed)
        self.undone = undone


@dataclass
class Member:
    name: str
    path: Path
    changed: bool = False
    branch: str = ""
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
        member.branch = head_branch
        member.entries = entries
        member.changed = bool(entries)
        members.append(member)
    if not any(m.changed for m in members):
        raise ChangeRefused("nothing_changed", ",".join(m.name for m in members), "no member has a change")
    return members


def undo(member: Member, branch: str) -> str | None:
    """Put one member back on its original branch and index; None if it worked.

    No checkout and no reset --hard: either would rewrite the worktree, which
    still holds the user's change and is the one thing this verb never owned.
    """
    for args in (
        ("symbolic-ref", "HEAD", f"refs/heads/{member.branch}"),
        ("reset", "-q"),
        ("branch", "-q", "-D", branch),
    ):
        proc = git(member.path, *args)
        if proc.returncode != 0:
            return f"git {args[0]} failed: {proc.stderr.strip()}"
    return None


def write(members: list[Member], branch: str, message: str) -> None:
    written: list[Member] = []
    for member in members:
        if not member.changed:
            continue
        for args in (
            ("checkout", "-q", "-b", branch),
            ("add", "-A"),
            ("commit", "-q", "-m", message),
        ):
            proc = git(member.path, *args)
            if proc.returncode == 0:
                if args[0] == "checkout":
                    written.append(member)
                continue
            observed = proc.stderr.strip() or f"git {args[0]} exited {proc.returncode}"
            stuck = [f"{m.name}: {err}" for m in reversed(written) if (err := undo(m, branch))]
            if stuck:
                raise ChangeFailed(f"{member.name}: git {args[0]} failed: {observed}; NOT undone: {'; '.join(stuck)}")
            raise ChangeUndone(f"{args[0]}_failed", member.name, observed, [m.name for m in reversed(written)])
        proc = git(member.path, "rev-parse", "HEAD")
        member.head = proc.stdout.strip()


def change(paths: list[Path], branch: str, message: str) -> list[Member]:
    members = preflight(paths, branch)
    write(members, branch, message)
    return members
