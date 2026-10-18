"""`gr2 change`: one change across members in one command.

Phase 0 reads every member and writes nothing; any refusal stops the verb
before the first write, so a refused run leaves every member as it was.
Phase 1 then writes per member, in the order given: branch, stage, commit.
Two git calls per member to read, three to write; the new head is read off
the commit's own summary line rather than asked for again.

A write that fails (a pre-commit hook refusing, say) is undone in every member
already touched, in reverse order: HEAD back to the original branch, the index
back to its HEAD, the new branch deleted. Three git calls per member, and none
of them touch the worktree, so the user's uncommitted change is still there.
"""

from __future__ import annotations

import re
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
            # An intent-to-add entry (`git add -N`) reads ".A": the undo's reset would drop the mark.
            if rec[0] in "12" and (rec[2] != "." or rec[3] == "A"):
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


def _summary_head(stdout: str, branch: str) -> str:
    """The full sha from commit's "[<branch> <sha>] <subject>" line, or ""."""
    pattern = re.compile(rf"^\[{re.escape(branch)} ([0-9a-f]{{40,64}})\] ", re.M)
    found = pattern.findall(stdout)
    return found[-1] if found else ""


def write(members: list[Member], branch: str, message: str) -> None:
    written: list[Member] = []
    for member in members:
        if not member.changed:
            continue
        for args in (
            ("checkout", "-q", "-b", branch),
            ("add", "-A"),
            ("-c", "core.abbrev=no", "commit", "-m", message),
        ):
            proc = git(member.path, *args)
            if proc.returncode == 0:
                if args[0] == "checkout":
                    written.append(member)
                continue
            verb = args[2] if args[0] == "-c" else args[0]
            observed = proc.stderr.strip() or f"git {verb} exited {proc.returncode}"
            stuck = [f"{m.name}: {err}" for m in reversed(written) if (err := undo(m, branch))]
            if stuck:
                raise ChangeFailed(f"{member.name}: git {verb} failed: {observed}; NOT undone: {'; '.join(stuck)}")
            raise ChangeUndone(f"{verb}_failed", member.name, observed, [m.name for m in reversed(written)])
        member.head = _summary_head(proc.stdout, branch)
        if not member.head:
            # A summary we cannot read costs one more call, never a wrong sha.
            member.head = git(member.path, "rev-parse", "HEAD").stdout.strip()


def change(paths: list[Path], branch: str, message: str) -> list[Member]:
    members = preflight(paths, branch)
    write(members, branch, message)
    return members
