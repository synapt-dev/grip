"""The tracking tree: a clone's view of a remote's records, kept by fetch and read with no network.

Git keeps its remote-tracking refs under ``refs/remotes/``. Records under a caller's namespace keep theirs
inside that namespace, at ``refs/<org>/__remotes__/<remote>/<org>/...``: the remote's name, then the
remote's own path mirrored. Two fetch lines in a clone fill the tree. The first mirrors the namespace; the
second is a negative refspec that keeps a remote's own ``__remotes__`` refs out, since a remote holds no
remotes tree and a tree fetched from one would nest inside this one. A read resolves a name on the tree and
never touches the network. The value it returns is the ref's own object id, unpeeled (an annotated tag
stays its tag object), because that is the value a push leases against."""
from __future__ import annotations

from pathlib import Path

from . import gitops
from .agent_record import _leaf

REMOTES = "__remotes__"


def fetch_lines(org: str, remote: str = "origin") -> tuple[str, str]:
    """The two fetch lines that keep ``remote``'s refs under ``refs/<org>/`` in this clone's tracking tree."""
    o, r = _leaf(org, "org"), _leaf(remote, "remote")
    return f"+refs/{o}/*:refs/{o}/{REMOTES}/{r}/{o}/*", f"^refs/{o}/{REMOTES}/*"


def ensure_fetch_lines(repo: Path, org: str, remote: str = "origin") -> bool:
    """Add the fetch lines to ``remote.<remote>.fetch`` where they are missing; True when a line was written.
    Running it again writes nothing."""
    lines = fetch_lines(org, remote)
    key = f"remote.{remote}.fetch"
    have = gitops.run(repo, "config", "--get-all", key).stdout.splitlines()
    wrote = False
    for line in lines:
        if line not in have:
            proc = gitops.run(repo, "config", "--add", key, line)
            if proc.returncode:
                raise RuntimeError(f"git config --add {key} failed: {proc.stderr.strip()[:200]}")
            wrote = True
    return wrote


def tracking_ref(ref: str, *, org: str, remote: str = "origin") -> str:
    """The tracking ref in this clone for ``ref`` (a full name under ``refs/<org>/`` on the remote)."""
    o, r = _leaf(org, "org"), _leaf(remote, "remote")
    head = f"refs/{o}/"
    if not ref.startswith(head) or ref[len(head):] == "" or ref.startswith(f"{head}{REMOTES}/"):
        raise ValueError(f"not a ref of namespace {o!r} on a remote (under {head}, never {head}{REMOTES}/): {ref!r}")
    return f"{head}{REMOTES}/{r}/{o}/{ref[len(head):]}"


def tracked(repo: Path, ref: str, *, org: str, remote: str = "origin") -> str | None:
    """The object id the last fetch saw for ``ref`` on ``remote``, read locally; None when it is not tracked."""
    proc = gitops.run(repo, "rev-parse", "-q", "--verify", tracking_ref(ref, org=org, remote=remote))
    return proc.stdout.strip() if proc.returncode == 0 else None
