"""Wait for a remote branch tip to move: poll ``git ls-remote`` until the exact ``refs/heads/<branch>`` row names an
object other than the one it named at the start (or the caller's ``from_sha``).

Prior art is `gr2 sync wait` (sync_wait.py), and this keeps its contract: the same exit codes
(``exit_codes.EXIT_CODES["sync"]``, repeated as the "review wait" table), the same bounds, the same read. 0 moved; 3
refused, before any read for a bad value and at the start for a remote that cannot be read or has no such ref; 6 the
timeout came first, including a read the deadline cut short; 7 unreadable, two failed reads in a row. A read cut
by its own 30 s bound with more time left is an ordinary failed read, not the deadline. A failed read is
never "not moved": one is retried at the next poll, a second ends the wait. A ref that the remote stops advertising
counts as a failed read with its own line, so a deletion seen twice ends in 7 saying the branch was deleted (often:
it merged and the branch was removed); whether a commit landed is a ``--contains`` question this wait does not ask.

Every read is ``ls-remote -- <remote> <ref>`` parsed by ``gitops.ls_remote_oid`` (the exact row, never a tail match),
bounded by min(30 s, the time left) so a remote that never answers cannot outlast --timeout. --timeout must be finite
and >= 0, --interval finite and >= 1, refused before any read. Every value ls-remote answers is data, compared and
returned, never an argument to another command. Every refusal is one RemoteWaitError carrying a nonzero code.
"""
from __future__ import annotations

import math
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

from . import gitops

#: A caller value that cannot be waited on, or a remote that cannot be read at the start.
CODE_REFUSED = 3
#: Kept for callers of the first version: a remote unreadable at the start is a refusal, as in sync wait.
CODE_REMOTE = CODE_REFUSED
#: The tip did not move before the timeout.
CODE_TIMEOUT = 6
#: Two reads in a row failed (or found the ref deleted).
CODE_UNREADABLE = 7

READ_BOUND = 30.0  # seconds; one ls-remote never waits longer than this or the time left, whichever is less

_FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")


class RemoteWaitError(RuntimeError):
    """A wait that cannot report a move. ``code`` is CODE_REFUSED, CODE_TIMEOUT or CODE_UNREADABLE."""

    def __init__(self, message: str, code: int = CODE_REFUSED):
        super().__init__(message)
        self.code = code


def _bound(value: float, kind: str, least: float) -> float:
    """A finite number of seconds >= ``least``; nan never compares true with a deadline, so it would never end."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise RemoteWaitError(f"--{kind} must be a number of seconds, not {value!r}", CODE_REFUSED) from None
    if not math.isfinite(seconds) or seconds < least:
        raise RemoteWaitError(f"--{kind} must be a finite number of seconds >= {least:g}, not {value!r}", CODE_REFUSED)
    return seconds


class _ReadFailed(Exception):
    """ls-remote exited nonzero (the message is git's reason, credentials redacted) or hit its own 30 s bound."""


def _ls_remote(repo: Path, remote: str, ref: str, bound: float) -> str:
    """The raw ls-remote answer for ``ref``; git matches the pattern by tail, so the caller keeps only the exact row."""
    args = ("ls-remote", "--", remote, ref)
    try:
        proc = gitops.run(repo, *args, timeout=bound, raise_timeout=True)
    except subprocess.TimeoutExpired:
        if bound < READ_BOUND:  # the bound was the time left: the wait's own deadline cut this read short
            raise RemoteWaitError(
                f"{gitops._shown_arg(remote)} {ref}: no answer within {bound:g}s, the time the wait had left",
                CODE_TIMEOUT,
            ) from None
        raise _ReadFailed(f"no answer within {bound:g}s") from None
    if proc.returncode != 0:
        raise _ReadFailed(f"ls-remote failed: {gitops.shown_git_text(args, proc.stderr or '', repo).strip()[:200]}")
    return proc.stdout


def _read(repo: Path, remote: str, ref: str, left: float) -> tuple[str | None, str]:
    """(oid, "") on a read, (None, why) on a failure: git's reason, a read cut by its own 30 s bound, a malformed
    listing, or the ref absent. A read the deadline cut short raises CODE_TIMEOUT: that is the wait's own deadline."""
    try:
        listing = _ls_remote(repo, remote, ref, max(1.0, min(READ_BOUND, left)))
    except _ReadFailed as exc:
        return None, str(exc)
    try:
        oid = gitops.ls_remote_oid(listing, ref)
    except ValueError as exc:  # LsRemoteMalformed, or the ref advertised twice
        return None, f"ls-remote answered a malformed listing: {exc}"
    return (oid, "") if oid else (None, "")


def wait_for_tip(
    repo: Path, remote: str, branch: str, *, from_sha: str | None = None, interval: float = 10.0,
    timeout: float = 600.0, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, str]:
    """(old, new) once ``refs/heads/<branch>`` on ``remote`` names an object other than ``old``: ``from_sha`` when
    given (a full 40-hex sha, compared lowercased), else the tip read at the start. A tip already off ``from_sha``
    returns at once. Deleted is not moved: see the module docstring for the codes."""
    interval = _bound(interval, "interval", 1.0)
    timeout = _bound(timeout, "timeout", 0.0)
    gitops.positional(remote, "remote", RemoteWaitError)
    gitops.positional(branch, "branch", RemoteWaitError)
    ref = f"refs/heads/{branch}"
    if gitops.run(repo, "check-ref-format", ref).returncode != 0:
        raise RemoteWaitError(f"not a valid branch name: {branch!r}", CODE_REFUSED)
    if from_sha is not None and not (isinstance(from_sha, str) and _FULL_SHA.fullmatch(from_sha)):
        raise RemoteWaitError(f"--since must be a full 40-hex sha, not {from_sha!r}", CODE_REFUSED)
    shown = gitops._shown_arg(remote)
    deadline = clock() + timeout
    start, why = _read(repo, remote, ref, deadline - clock())
    if start is None:
        raise RemoteWaitError(f"refused: {shown} {why}" if why else f"refused: {shown} has no {ref}", CODE_REFUSED)
    old = from_sha.lower() if from_sha is not None else start
    if start != old:
        return old, start
    failed = False
    while True:
        left = deadline - clock()
        if left <= 0:
            raise RemoteWaitError(f"timeout: {shown} {ref} still at {old} after {timeout:g}s", CODE_TIMEOUT)
        sleep(min(interval, left))
        new, why = _read(repo, remote, ref, deadline - clock())
        if new is None:
            if failed:
                if not why:
                    raise RemoteWaitError(
                        f"unreadable: {shown} {ref} was deleted while waiting (it was {old}); often it merged and the "
                        "branch was removed; whether a commit landed is a --contains question, a later step",
                        CODE_UNREADABLE,
                    )
                raise RemoteWaitError(f"unreadable: {shown} {ref}: {why}", CODE_UNREADABLE)
            failed = True
            continue
        failed = False
        if new != old:
            return old, new
