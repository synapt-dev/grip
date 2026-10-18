"""One canonical key per kind. A value of a kind is compared, looked up and stored as a key only after it passes
through that kind's function here, so two spellings of the same thing can never be two keys.

Remote URLs have two keys, because readers ask two questions whose safe errors point opposite ways:

- `remote_url_key` answers "which repository does this URL name?" for a reader that matches or includes on equality
  (store init's checkout-origin check, the merge gate). Its safe error is a false DIFFERENT, so it folds only
  spellings that cannot name two repositories, and a local path keeps its `.git`.
- `may_be_same_repository` answers "could these two URLs be one repository?" for a reader that only REFUSES on a
  match (store push --atomic's aliased-member check). Its safe error is a false SAME, so it folds everything
  `remote_url_key` folds and also treats a local path with and without `.git` as one, since git resolves either."""
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


def may_be_same_repository(a: str, a_base: Path, b: str, b_base: Path) -> bool:
    """True when `a` and `b` could name one repository: `remote_url_key` agrees, or both are local paths that differ
    only by a trailing `.git` (git resolves `<dir>/root` to `<dir>/root.git` when only the latter exists). Use it
    only to refuse, never to include. Each URL brings its own base, the clone it is configured in, since git resolves
    a relative path there."""
    ka, kb = remote_url_key(a, a_base), remote_url_key(b, b_base)
    if not ka[0] and not kb[0]:
        return ka[1].removesuffix(".git") == kb[1].removesuffix(".git")
    return ka == kb
