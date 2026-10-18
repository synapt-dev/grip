"""One canonical key per kind. A compare that passes both sides through a kind's function here sees two spellings
of one thing as one key. Not every compare calls these yet: `member_path_key` is used by review_allocation's
member check and by store_hooks; other member-path compares (declared_sets among them) still compare raw strings
and can see two spellings as two keys.

Remote URLs have two keys, because readers ask two questions whose safe errors point opposite ways:

- `remote_url_key` answers "which repository does this URL name?" for a reader that matches or includes on equality
  (store init's checkout-origin check, the merge gate). Its safe error is a false DIFFERENT, so it folds only
  spellings that cannot name two repositories, and a local path keeps its `.git`.
- `may_be_same_repository` answers "could these two URLs be one repository?" for a reader that only REFUSES on a
  match (store push --atomic's aliased-member check). Its safe error is a false SAME, so it folds everything
  `remote_url_key` folds, also treats a local path with and without `.git` as one (git resolves either), and also
  folds any user and any port on a network host, which `remote_url_key` keeps."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath


def member_path_key(path: object) -> str:
    """A member path as git spells its gitlink: `./alpha`, `alpha/` and `alpha//` are all `alpha`. It does not fold
    case or resolve `..`; git does neither, so `Alpha` and `x/../alpha` stay distinct keys."""
    return str(PurePosixPath(str(path)))


_DEFAULT_PORTS = {"ssh": "22", "git+ssh": "22", "ssh+git": "22", "https": "443", "http": "80", "git": "9418"}


def _host_key(hostpart: str, default_port: str | None) -> str:
    """The host, lowercased, with the user kept unless it is `git` and the port kept unless it is the default."""
    user, at, hostport = hostpart.rpartition("@")
    host, colon, port = hostport.partition(":")
    key = host.lower()
    if colon and port and port != default_port:
        key = f"{key}:{port}"
    if at and user != "git":
        key = f"{user}@{key}"
    return key


def remote_url_key(url: str, base: Path) -> tuple[str, str]:
    """One repository, however its URL is spelled: (host, path) for a network URL, ("", realpath) for a local one.

    A `file://` scheme, a trailing '/', and a trailing '.git' on a network path do not change the repository. The
    user `git` (the forge convention) and the scheme's default port do not either, so an SSH spelling
    (`git@host:org/x.git`, `ssh://git@host:22/...`) and an HTTPS one (`https://host/org/x`) of the same repository
    give the same key. Any other user or port stays in the key: on a plain ssh server `alice@host:repo` and
    `bob@host:repo` are two repositories (a path with no leading '/' is relative to the login's home), and so are
    two ports on one host. A relative local path resolves against `base`, the clone, which is where git resolves
    it. The '.git' fold on a network path assumes a forge; a plain ssh server can hold both."""
    u = url.strip()
    if u.startswith("file://"):
        u = u[len("file://"):]
    if "://" in u:
        scheme, rest = u.split("://", 1)
        hostpart, _, path = rest.partition("/")
        return _host_key(hostpart, _DEFAULT_PORTS.get(scheme.lower())), path.strip("/").removesuffix(".git")
    head, sep, tail = u.partition(":")
    if sep and head and "/" not in head:  # git's scp-like form: a colon before any slash
        return _host_key(head, None), tail.strip("/").removesuffix(".git")
    return "", os.path.realpath(u if os.path.isabs(u) else base / u)


def _loose(key: tuple[str, str]) -> tuple[str, str]:
    """A `remote_url_key` with any user and port dropped from the host: the fold a refuse-only reader may make."""
    host, path = key
    return host.rpartition("@")[2].partition(":")[0], path


def may_be_same_repository(a: str, a_base: Path, b: str, b_base: Path) -> bool:
    """True when `a` and `b` could name one repository: their `remote_url_key`s agree once any user and port are
    dropped from the host, or both are local paths that differ only by a trailing `.git` (git resolves `<dir>/root`
    to `<dir>/root.git` when only the latter exists). Use it
    only to refuse, never to include. Each URL brings its own base, the clone it is configured in, since git resolves
    a relative path there."""
    ka, kb = _loose(remote_url_key(a, a_base)), _loose(remote_url_key(b, b_base))
    if not ka[0] and not kb[0]:
        return ka[1].removesuffix(".git") == kb[1].removesuffix(".git")
    return ka == kb
