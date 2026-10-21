"""`gr2 change`: one change across members in one command.

Phase 0 reads every member and writes nothing; any refusal stops the verb
before the first write, so a refused run leaves every member as it was.
Phase 1 then writes per member, in the order given: branch, stage, commit.
Three git calls per member to read, three to write; the new head is read off
the commit's own summary line rather than asked for again.

A write that fails (a pre-commit hook refusing, say) is undone in every member
already touched, in reverse order: HEAD back to the original branch, the index
back to its HEAD, the new branch deleted. Three git calls per member, and none
of them touch the worktree, so the user's uncommitted change is still there.
"""

from __future__ import annotations

import re
import signal
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .gitops import git, run


class ChangeRefused(Exception):
    def __init__(self, reason: str, member: str, observed: str) -> None:
        super().__init__(f"{reason}: {member}: {observed}")
        self.reason = reason
        self.member = member
        self.observed = observed


class ChangeFailed(Exception):
    pass


class ChangeInterrupted(Exception):
    """A signal stopped the writes; every member touched was undone, or `stuck` says where it stopped."""

    def __init__(self, undone: list[str], stuck: list[str]) -> None:
        super().__init__("interrupted")
        self.undone = undone
        self.stuck = stuck


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


def _shown(name: str) -> str:
    """A path as printable text: bytes that are not UTF-8 (carried as surrogates) print as \\xNN, never raise."""
    return name.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")


def _read(path: Path, *args: str) -> tuple[int, str, str]:
    """One read as bytes, decoded strictly: the verb carries only UTF-8 paths, so a read that is not UTF-8 refuses.

    Text mode would raise UnicodeDecodeError on such a path, and would turn a "\\r" in one into "\\n"."""
    proc = run(path, *args, binary=True, via_cwd=True)
    try:
        return proc.returncode, proc.stdout.decode("utf-8"), proc.stderr.decode("utf-8", "backslashreplace")
    except UnicodeDecodeError:
        raise ChangeRefused("not_utf8", _shown(path.name), f"git {_shown(args[0])} printed a path that is not UTF-8") from None


def _status_entries(path: Path) -> tuple[str, list[str]]:
    code, out, err = _read(path, "status", "--porcelain=v2", "--branch", "-z")
    if code != 0:
        raise ChangeRefused("not_a_repo", _shown(path.name), err.strip() or "git status failed")
    branch = ""
    entries: list[str] = []
    records = out.split("\0")
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
        if path.name != _shown(path.name):
            # A path that is not UTF-8 refuses before any git call; carried on, it would commit and then fail to print.
            raise ChangeRefused("not_utf8", _shown(path.name), "the member path is not UTF-8")
        if not path.is_dir():
            raise ChangeRefused("not_a_repo", member.name, "not a directory")
        head_branch, entries = _status_entries(path)
        # One read for both: a member that is a subdirectory of a repo (its add -A takes the whole repo), and
        # the operation-in-progress markers below.
        code, out, err = _read(
            path, "rev-parse", "--show-toplevel",
            "--git-path", "MERGE_HEAD", "--git-path", "CHERRY_PICK_HEAD", "--git-path", "REVERT_HEAD",
            "--git-path", "rebase-apply", "--git-path", "rebase-merge", "--git-path", "sequencer",
        )
        if code != 0:
            raise ChangeRefused("not_a_repo", member.name, err.strip() or "git rev-parse failed")
        # One line per argument, split on "\n" alone: splitlines() also breaks at U+2028, "\f" or "\r", which git
        # prints inside a path. A path holding "\n" prints more lines; fewer or more would misread the markers.
        lines = out.split("\n")
        if lines[-1] == "":
            lines.pop()
        if len(lines) != 7:
            raise ChangeRefused(
                "not_a_repo", member.name, f"git rev-parse exited 0 but printed {len(lines)} lines for 7 paths"
            )
        top, *paths_pending = lines
        if Path(top).resolve() != path.resolve():
            # Only a top above the member is "the repo it is inside"; anything else is git naming another path.
            if path.resolve().is_relative_to(Path(top).resolve()):
                raise ChangeRefused("not_a_repo", member.name, f"inside the repo at {top}")
            raise ChangeRefused("not_a_repo", member.name, f"git rev-parse named {_shown(top)} as its top, not this path")
        if head_branch == "(detached)":
            raise ChangeRefused("detached_head", member.name, "HEAD is detached")
        for rec in entries:
            if rec.startswith("u "):
                raise ChangeRefused("unresolved", member.name, "unmerged paths")
            # An intent-to-add entry (`git add -N`) reads ".A": the undo's reset would drop the mark.
            if rec[0] in "12" and (rec[2] != "." or rec[3] == "A"):
                raise ChangeRefused("staged_index", member.name, "the index already differs from HEAD")
        # A merge, cherry-pick, revert, am or rebase in progress: `git switch` refuses, and checkout -b would drop it.
        if any((path / p).exists() for p in paths_pending if p):
            raise ChangeRefused(
                "operation_in_progress", member.name, "a merge, cherry-pick, revert, am or rebase is in progress"
            )
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


@contextmanager
def _shielded():
    """Ignore SIGINT and SIGTERM while an undo runs: a second Ctrl-C must not leave it half done."""
    old = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


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
    try:
        _write(members, branch, message, written)
    except KeyboardInterrupt:
        # A checkout cut short may have made the branch before `written` saw it; preflight
        # refused any member that already had it, so a branch here is ours to undo.
        with _shielded():
            touched = [m for m in members if m in written or (m.changed and _has_branch(m, branch))]
            errors = {m.name: undo(m, branch) for m in reversed(touched)}
        raise ChangeInterrupted(
            [name for name, err in errors.items() if not err],
            [f"{name}: {err}" for name, err in errors.items() if err],
        ) from None


def _has_branch(member: Member, branch: str) -> bool:
    return git(member.path, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0


def _write(members: list[Member], branch: str, message: str, written: list[Member]) -> None:
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
            with _shielded():
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
