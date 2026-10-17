"""`gr2 sync wait`: wait until any or all of the workspace's members move on their remote.

One `git ls-remote` per member per poll, and none for a member already seen to move. A member's baseline is what the
caller last saw: its checkout's ``refs/remotes/origin/<ref>`` when that exists, otherwise the remote's oid at the first
poll. Measured against a starting "now" alone, a move between the caller's last look and the start would never be seen.

Exit codes (``exit_codes.EXIT_CODES["sync"]``): 0 the condition holds; 6 the timeout came first (the same code as
`store wait`); 3 a member cannot be waited on (no url, or its remote unreadable at the start), named; 7 a member's
remote could not be read on two polls running, named with git's first stderr line. A failed read is never counted as
"not moved": one failure is retried at the next poll, and a second ends the wait, because under --until all a remote
that has gone away would otherwise run out the whole timeout and report a silence as a fact.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from . import gitops
from .review_members import Member, workspace_members

EXIT_MET = 0
EXIT_REFUSED = 3
EXIT_TIMEOUT = 6
EXIT_UNREADABLE = 7


def _remote_ref(m: Member) -> str:
    return f"refs/heads/{m.ref}" if m.ref else "HEAD"


def _read_remote(workspace_root: Path, m: Member) -> tuple[str | None, str]:
    """(oid, "") on a read, (None, why) on a failure: git's first stderr line, or the ref being absent."""
    proc = gitops.git(workspace_root, "ls-remote", m.url or "", _remote_ref(m))
    if proc.returncode != 0:
        lines = (proc.stderr or "").strip().splitlines()
        return None, lines[0] if lines else f"git ls-remote exited {proc.returncode}"
    for line in proc.stdout.splitlines():
        oid, _, name = line.partition("\t")
        if name == _remote_ref(m):
            return oid, ""
    return None, f"no {_remote_ref(m)} on the remote"


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
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> int:
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    members = workspace_members(workspace_root)
    if not members:
        echo("refused: the workspace declares no members")
        return EXIT_REFUSED
    unusable = [m.name for m in members if not m.url]
    if unusable:
        echo(f"refused: no url for: {', '.join(unusable)}")
        return EXIT_REFUSED
    baseline: dict[str, str] = {}
    first: dict[str, str] = {}
    for m in members:
        seen = _seen_oid(workspace_root, m)
        if seen:
            baseline[m.name] = seen
            continue
        oid, _why = _read_remote(workspace_root, m)
        if oid is None:
            echo(f"refused: cannot read {_remote_ref(m)} of {m.name} at {m.url}")
            return EXIT_REFUSED
        baseline[m.name] = first[m.name] = oid
    echo(f"waiting for {until} of {len(members)} members to move: {', '.join(m.name for m in members)}")
    moved: dict[str, str] = {}
    failed: set[str] = set()  # members whose last read failed; a second failure in a row ends the wait
    deadline = clock() + timeout
    while True:
        for m in members:
            if m.name in moved:
                continue
            oid, why = (first.pop(m.name), "") if m.name in first else _read_remote(workspace_root, m)
            if oid is None:
                if m.name in failed:
                    echo(f"unreadable: {m.name} {why}")
                    return EXIT_UNREADABLE
                failed.add(m.name)
                continue
            failed.discard(m.name)
            if oid != baseline[m.name]:
                moved[m.name] = oid
                echo(f"moved: {m.name} {baseline[m.name][:12]} -> {oid[:12]}")
        if (until == "any" and moved) or len(moved) == len(members):
            echo(f"met: {until}: moved {', '.join(moved)}")
            return EXIT_MET
        if clock() >= deadline:
            still = [m.name for m in members if m.name not in moved]
            echo(f"timeout after {timeout:g}s: {until} not met; not moved: {', '.join(still)}")
            return EXIT_TIMEOUT
        sleep(max(0.0, min(interval, deadline - clock())))
