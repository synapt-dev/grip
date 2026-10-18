"""One canonical key per kind. A value of a kind is compared, looked up and stored as a key only after it passes
through that kind's function here, so two spellings of the same thing can never be two keys."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath


def member_path_key(path: object) -> str:
    """A member path as git spells its gitlink: `./alpha`, `alpha/` and `alpha//` are all `alpha`. It does not fold
    case or resolve `..`; git does neither, so `Alpha` and `x/../alpha` stay distinct keys."""
    return str(PurePosixPath(str(path)))


def remote_url_key(url: str, base: Path) -> tuple[str, str]:
    """One repository, however its URL is spelled: (host, path) for a network URL, ("", realpath) for a local one.

    A `file://` scheme, a trailing '/', and a trailing '.git' on a network path do not change the repository, and
    neither does the user or port in front of the host, so an SSH spelling (`git@host:org/x.git`, `ssh://...`) and
    an HTTPS one (`https://host/org/x`) of the same repository give the same key. A relative local path resolves
    against `base`, the clone, which is where git resolves it."""
    u = url.strip()
    if u.startswith("file://"):
        u = u[len("file://"):]
    if "://" in u:
        hostpart, _, path = u.split("://", 1)[1].partition("/")
        return hostpart.rsplit("@", 1)[-1].split(":", 1)[0].lower(), path.strip("/").removesuffix(".git")
    head, sep, tail = u.partition(":")
    if sep and head and "/" not in head:  # git's scp-like form: a colon before any slash
        return head.rsplit("@", 1)[-1].lower(), tail.strip("/").removesuffix(".git")
    return "", os.path.realpath(u if os.path.isabs(u) else base / u)
