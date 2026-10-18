"""`gr2 sync wait`: wait until any or all of the workspace's members move on their remote.

One `git ls-remote` per member per poll, and none for a member already seen to move. A member's baseline is what the
caller last saw: its checkout's ``refs/remotes/origin/<ref>`` when that exists, otherwise the remote's oid at the first
poll. Measured against a starting "now" alone, a move between the caller's last look and the start would never be seen.

Exit codes (``exit_codes.EXIT_CODES["sync"]``): 0 the condition holds; 6 the timeout came first (the same code as
`store wait`); 3 a member cannot be waited on (no url, or its remote unreadable at the start), named; 7 a member's
remote could not be read on two polls running, named with git's first stderr line. A failed read is never counted as
"not moved": one failure is retried at the next poll, and a second ends the wait, because under --until all a remote
that has gone away would otherwise run out the whole timeout and report a silence as a fact.

Every read is `ls-remote -- <url> <ref>`, bounded by min(30 s, the time left): the `--` keeps a member url such as
``--upload-pack=<cmd>`` a repository name rather than an option git runs, and the bound
keeps a remote that accepts and never answers inside --timeout instead of gitops' 600 s default; a git timeout is an
ordinary failed read. A poll starts at most --interval after the last, and early enough that a poll as long as the
last one ends by the deadline; a read the deadline cut short is not a failed read, and the timeout line names, for each
member not moved, when its last read was. --timeout must be finite and >= 0 and --interval finite and >= 1, refused (exit 3) before any
read: nan never ends, and an interval of 0 would poll without pause. Refusals and the
unreadable line go to ``err`` (stderr from the CLI) with git's reason.
"""
from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Callable

from . import gitops
from .review_members import Member, workspace_members

EXIT_MET = 0
EXIT_REFUSED = 3
EXIT_TIMEOUT = 6
EXIT_UNREADABLE = 7
READ_BOUND = 30.0  # seconds; one ls-remote never waits longer than this or the time left, whichever is less


def _remote_ref(m: Member) -> str:
    return f"refs/heads/{m.ref}" if m.ref else "HEAD"


def _read_remote(workspace_root: Path, m: Member, left: float) -> tuple[str | None, str]:
    """(oid, "") on a read, (None, why) on a failure: git's first stderr line, or the ref being absent."""
    proc = gitops.git(workspace_root, "ls-remote", "--", m.url or "", _remote_ref(m),
                      timeout=max(0.1, min(READ_BOUND, left)))
    if proc.returncode != 0:
        lines = (proc.stderr or "").strip().splitlines()
        return None, lines[0] if lines else f"git ls-remote exited {proc.returncode}"
    try:
        oid = gitops.ls_remote_oid(proc.stdout, _remote_ref(m))
    except ValueError as exc:
        return None, f"ls-remote answered a malformed listing: {exc}"
    if oid is None:
        return None, f"no {_remote_ref(m)} on the remote"
    return oid, ""


def _seen_oid(workspace_root: Path, m: Member) -> str | None:
    checkout = workspace_root / m.path
    if not m.ref or not (checkout / ".git").exists():
        return None
    proc = gitops.git(checkout, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{m.ref}")
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def wait(
    workspace_root: Path,
    *,
    until: str,
    timeout: float,
    interval: float,
    echo: Callable[[str], None] = print,
    err: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> int:
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    err = err or echo
    if not (math.isfinite(timeout) and timeout >= 0):
        err(f"refused: --timeout must be a finite number of seconds >= 0, not {timeout:g}")
        return EXIT_REFUSED
    if not (math.isfinite(interval) and interval >= 1):
        err(f"refused: --interval must be a finite number of seconds >= 1, not {interval:g}")
        return EXIT_REFUSED
    deadline = clock() + timeout
    members = workspace_members(workspace_root)
    if not members:
        err("refused: the workspace declares no members")
        return EXIT_REFUSED
    unusable = [m.name for m in members if not m.url]
    if unusable:
        err(f"refused: no url for: {', '.join(unusable)}")
        return EXIT_REFUSED
    baseline: dict[str, str] = {}
    first: dict[str, str] = {}
    for m in members:
        seen = _seen_oid(workspace_root, m)
        if seen:
            baseline[m.name] = seen
            continue
        oid, why = _read_remote(workspace_root, m, deadline - clock())
        if oid is None:
            if clock() >= deadline:
                err(f"timeout after {timeout:g}s reading {m.name} at the start: {why}")
                return EXIT_TIMEOUT
            err(f"refused: cannot read {_remote_ref(m)} of {m.name} at {m.url}: {why}")
            return EXIT_REFUSED
        baseline[m.name] = first[m.name] = oid
    echo(f"waiting for {until} of {len(members)} members to move: {', '.join(m.name for m in members)}")
    moved: dict[str, str] = {}
    t0, read_at = deadline - timeout, {m.name: clock() for m in members if m.name in first}
    failed: dict[str, str] = {}  # members whose last read failed, with git's reason; a second failure in a row ends the wait
    dur, last, cut = max(clock() - t0, 0.1), False, set()
    while True:
        began, need, opening = clock(), dur, bool(first)
        for m in members:
            if m.name in moved:
                continue
            if m.name not in first and clock() >= deadline:
                continue  # no time left to start a read: the member keeps its last read
            fresh, left = m.name not in first, deadline - clock()
            oid, why = _read_remote(workspace_root, m, left) if fresh else (first.pop(m.name), "")
            if oid is None and left < need:
                cut.add(m.name)  # started with less than a poll's time left, so the deadline cut it: not a failed read
                failed.pop(m.name, None)
                continue
            cut.discard(m.name)
            if oid is None:
                if m.name in failed:
                    err(f"unreadable: {m.name} {why}")
                    return EXIT_UNREADABLE
                failed[m.name] = why
                continue
            failed.pop(m.name, None)
            if fresh:
                read_at[m.name] = clock()
            if oid != baseline[m.name]:
                moved[m.name] = oid
                echo(f"moved: {m.name} {baseline[m.name][:12]} -> {oid[:12]}")
        if (until == "any" and moved) or len(moved) == len(members):
            echo(f"met: {until}: moved {', '.join(moved)}")
            return EXIT_MET
        # The next poll starts --interval after this one, or earlier, so that a poll as long as this one (or the start
        # reads) ends by the deadline; a poll at the deadline would read nothing.
        dur, left = max(dur if opening else 0.0, clock() - began, 0.1), deadline - clock()
        if last or left <= 0:
            still = [m.name for m in members if m.name not in moved and m.name not in failed]
            unread = [n for n in still if n in cut or n not in read_at]
            why = []
            if any(n not in unread for n in still):
                why.append("not moved as of the last read: " + ", ".join(
                    f"{n} at {round(read_at[n] - t0, 1):g}s" for n in still if n not in unread))
            if unread:
                why.append("not read before the deadline: " + ", ".join(unread))
            if failed:
                why.append("last read failed: " + ", ".join(f"{n} ({w})" for n, w in failed.items()))
            echo(f"timeout after {timeout:g}s: {until} not met; {'; '.join(why)}")
            return EXIT_TIMEOUT
        nap = min(interval, left - dur)
        last = nap < 0.1  # too close to the deadline for another nap: this poll is the last
        sleep(max(0.0, nap))
