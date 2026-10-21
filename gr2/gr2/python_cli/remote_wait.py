"""Wait for a remote branch tip to move: poll ``git ls-remote`` until the exact ``refs/heads/<branch>`` row names an
object other than the one it named at the start (or the caller's ``from_sha``).

Placement-neutral: no verb calls this yet. Every value ls-remote answers is data, compared and returned, never an
argument to another command. Every refusal is one RemoteWaitError carrying a nonzero code.
"""
from __future__ import annotations

import math
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

from . import gitops

#: A caller value that cannot be waited on (option-shaped, an invalid refname, a bad sha, a bad bound).
CODE_REFUSED = 2
#: The remote cannot answer the wait: ls-remote failed, answered a malformed line, or the ref is absent or deleted.
CODE_REMOTE = 3
#: The tip did not move before the timeout.
CODE_TIMEOUT = 4

_FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")


class RemoteWaitError(RuntimeError):
    """A wait that cannot report a move. ``code`` is nonzero: CODE_REFUSED, CODE_REMOTE or CODE_TIMEOUT."""

    def __init__(self, message: str, code: int = CODE_REFUSED):
        super().__init__(message)
        self.code = code


def _bound(value: float, kind: str) -> float:
    """A positive, finite number of seconds; zero, a negative, nan and inf are refused (nan compares false with
    every deadline, so a nan timeout would never end and a nan interval would never sleep)."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise RemoteWaitError(f"{kind} must be a number of seconds, not {value!r}", CODE_REFUSED) from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise RemoteWaitError(f"{kind} must be a positive, finite number of seconds, not {value!r}", CODE_REFUSED)
    return seconds


def _ls_remote(repo: Path, remote: str, ref: str, timeout: float) -> str:
    """The raw ls-remote answer for ``ref``. git matches the pattern by tail, so the caller keeps only the exact row.
    The call is bounded by ``timeout``, the time the wait has left, so a remote that does not answer within it ends
    the wait with CODE_TIMEOUT: the only deadline in play is the wait's own."""
    try:
        proc = gitops.run(repo, "ls-remote", "--", remote, ref, timeout=timeout, raise_timeout=True)
    except subprocess.TimeoutExpired:
        raise RemoteWaitError(
            f"{gitops._shown_arg(remote)} {ref}: no answer within {timeout:g}s, the time the wait had left", CODE_TIMEOUT
        ) from None
    if proc.returncode != 0:
        args = ("ls-remote", "--", remote, ref)
        raise RemoteWaitError(
            f"ls-remote {gitops._shown_arg(remote)} failed: {gitops.shown_git_text(args, proc.stderr or '', repo).strip()[:200]}",
            CODE_REMOTE,
        )
    return proc.stdout


def _tip(repo: Path, remote: str, ref: str, timeout: float) -> str | None:
    """The oid the exact ``ref`` row names, or None when no row names it exactly."""
    listing = _ls_remote(repo, remote, ref, timeout)
    try:
        return gitops.ls_remote_oid(listing, ref)
    except ValueError as exc:  # LsRemoteMalformed, or the ref advertised twice
        raise RemoteWaitError(f"ls-remote {gitops._shown_arg(remote)} {ref}: {exc}", CODE_REMOTE) from None


def wait_for_tip(
    repo: Path, remote: str, branch: str, *, from_sha: str | None = None, interval: float = 5.0,
    timeout: float = 600.0, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, str]:
    """(old, new) once ``refs/heads/<branch>`` on ``remote`` names an object other than ``old``: ``from_sha`` when
    given (a full 40-hex sha, compared lowercased), else the tip read at the start. A tip already off ``from_sha``
    returns at once. A ref absent at the start, or deleted while waiting, is refused: deleted is not moved."""
    gitops.positional(remote, "remote", RemoteWaitError)
    gitops.positional(branch, "branch", RemoteWaitError)
    interval = _bound(interval, "interval")
    timeout = _bound(timeout, "timeout")
    ref = f"refs/heads/{branch}"
    if gitops.run(repo, "check-ref-format", ref).returncode != 0:
        raise RemoteWaitError(f"not a valid branch name: {branch!r}", CODE_REFUSED)
    if from_sha is not None and not (isinstance(from_sha, str) and _FULL_SHA.fullmatch(from_sha)):
        raise RemoteWaitError(f"from must be a full 40-hex sha, not {from_sha!r}", CODE_REFUSED)
    shown = gitops._shown_arg(remote)
    deadline = clock() + timeout
    start = _tip(repo, remote, ref, max(1.0, deadline - clock()))
    if start is None:
        raise RemoteWaitError(f"{shown} has no {ref}", CODE_REMOTE)
    old = from_sha.lower() if from_sha is not None else start
    if start != old:
        return old, start
    while True:
        left = deadline - clock()
        if left <= 0:
            raise RemoteWaitError(f"{shown} {ref} still at {old} after {timeout:g}s", CODE_TIMEOUT)
        sleep(min(interval, left))
        new = _tip(repo, remote, ref, max(1.0, deadline - clock()))
        if new is None:
            raise RemoteWaitError(f"{shown} {ref} was deleted while waiting (it was {old})", CODE_REMOTE)
        if new != old:
            return old, new
