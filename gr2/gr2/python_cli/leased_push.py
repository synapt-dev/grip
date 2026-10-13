"""A leased push of records under a caller's namespace: every ref leased on the tracking tree's value.

``push`` sends local refs under ``refs/<org>/`` to the same names on a remote. Before anything is sent:
- each ref must have a local value (a delete is not a push);
- the remote's current value of each ref (one ``ls-remote``) must equal what the tracking tree recorded at the
  last fetch: a stale tracking ref is refused by name and both values, and nothing is pushed;
- each tracked ref must fast-forward to its local value; a rewind is refused by name and both values unless
  ``allow_rewind``.
The push itself leases every ref on the tracked value (``--force-with-lease=<ref>:<value>``, empty when the ref
is new), so a writer that moves a ref between the check and the push still turns the push away, and with
``atomic`` (the default) no ref moves unless all do. After it lands, git itself moves the tracking refs to the
pushed values through the remote's fetch line (``tracking.ensure_fetch_lines``), as it does for refs/remotes/."""
from __future__ import annotations

from pathlib import Path

from . import gitops
from . import tracking


class PushRefused(RuntimeError):
    """Refused before anything was sent; ``ref`` names the first ref that failed, ``why`` the rule."""

    def __init__(self, ref: str, why: str):
        super().__init__(f"{ref}: {why}")
        self.ref, self.why = ref, why


class PushRejected(RuntimeError):
    """The remote turned the push away (a lease that no longer held); with atomic, no ref moved."""


def _value(repo: Path, ref: str) -> str | None:
    proc = gitops.run(repo, "rev-parse", "-q", "--verify", ref)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _remote_values(repo: Path, remote: str, refs: list[str]) -> dict[str, str]:
    proc = gitops.run(repo, "ls-remote", remote, *refs)
    if proc.returncode:
        raise PushRefused(refs[0], f"ls-remote {remote} failed: {proc.stderr.strip()[:200]}")
    out = {}
    for line in proc.stdout.splitlines():
        sha, name = line.split("\t", 1)
        if not name.endswith("^{}"):
            out[name] = sha
    return out


def push(repo: Path, refs: list[str], *, org: str, remote: str = "origin", atomic: bool = True,
         allow_rewind: bool = False) -> dict[str, str]:
    """Push ``refs`` (full names under ``refs/<org>/``) leased on the tracking tree; returns {ref: pushed value}."""
    if not refs:
        raise ValueError("nothing to push")
    tracked = {r: tracking.tracked(repo, r, org=org, remote=remote) for r in refs}   # names checked here
    local = {}
    for r in refs:
        local[r] = _value(repo, r)
        if local[r] is None:
            raise PushRefused(r, "no local value (a delete is not a push)")
    now = _remote_values(repo, remote, refs)
    for r in refs:
        if now.get(r) != tracked[r]:
            raise PushRefused(r, f"stale tracking: the remote has {now.get(r) or 'nothing'}, "
                                 f"the last fetch recorded {tracked[r] or 'nothing'}; fetch first")
    for r in refs:
        old = tracked[r]
        if old and old != local[r] and not allow_rewind and \
                gitops.run(repo, "merge-base", "--is-ancestor", old, local[r]).returncode != 0:
            kind = "rewind" if gitops.run(repo, "merge-base", "--is-ancestor", local[r], old).returncode == 0 else "rewrite"
            raise PushRefused(r, f"{kind}: {local[r]} does not descend from the remote's {old} (allow_rewind to take it)")
    args = ["push", "--porcelain"] + (["--atomic"] if atomic else [])
    args += [f"--force-with-lease={r}:{tracked[r] or ''}" for r in refs]
    args += [remote] + [f"{r}:{r}" for r in refs]
    proc = gitops.run(repo, *args)
    if proc.returncode:
        raise PushRejected(f"push to {remote} turned away: {(proc.stdout + proc.stderr).strip()[-400:]}")
    return local
