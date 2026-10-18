"""SHADOW: the resolved snapshot beside a STOOK, for members that live in the same repository.

A STOOK holds names. Its SHADOW (``.../SHADOW`` beside ``.../STOOK``) is the same record with every member
name resolved to the value the remote had at the last fetch: the STOOK's tree plus one gitlink per member under
``pins/<member path>``. The SHADOW's parents are its own previous tip (its first-parent chain), the STOOK's
tip, then each member's resolved commit. Because the members live in this repository, those parent edges make
the SHADOW self-contained: pushing it alone carries every member it pins.

Resolution reads the tracking tree (``tracking.tracked``), never a local ref, so a member's unpushed writes
are never pinned. A member that has a pushed SHADOW contributes that SHADOW (the recursion runs through
members' pushed shadows); otherwise its pushed STOOK. A member with neither is dangling and refused by name
before anything is written.

Members in their OWN repositories are not handled here: a parent edge is reachability, and reachability across
a repository boundary would carry one repository's history into another; they keep full mode's carrier."""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from . import gitops
from . import tracking

PINS = "pins"


class Dangling(ValueError):
    """A member with no pushed STOOK or SHADOW on the remote; nothing was written."""


class ShadowMoved(RuntimeError):
    """The SHADOW ref moved while this cut was being made (a second cutter); nothing was written."""


class CannotVerify(RuntimeError):
    """A member's ancestry could not be walked because an object is not held here, even after a fetch (a
    partial clone, or offline); never treated as a pass. Nothing was written."""


class MemberRewound(RuntimeError):
    """A member's new pin does not descend from the pin the previous SHADOW recorded; nothing was written."""

    def __init__(self, member: str, kind: str, old: str, new: str):
        super().__init__(f"{member}: {kind}: {new} does not descend from the previous shadow's {old} "
                         f"(allow_rewind to take it; it is recorded in the shadow)")
        self.member, self.kind, self.old, self.new = member, kind, old, new


class Cut(NamedTuple):
    commit: str
    added: list[str]       # members the previous shadow did not pin
    removed: list[str]     # members the previous shadow pinned and this one does not
    rewound: list[str]     # members taken backwards or sideways under allow_rewind (recorded in the message)


def _run(repo: Path, *args: str, data: str | None = None):
    proc = gitops.run(repo, *args, input=data)
    if proc.returncode:
        raise RuntimeError(f"git {args[0]} failed: {proc.stderr.strip()[:200]}")
    return proc.stdout


def _value(repo: Path, ref: str) -> str | None:
    proc = gitops.run(repo, "rev-parse", "-q", "--verify", f"{ref}^{{commit}}")
    return proc.stdout.strip() if proc.returncode == 0 else None


def shadow_of(stook: str) -> str:
    if not stook.endswith("/STOOK"):
        raise ValueError(f"not a STOOK ref: {stook!r}")
    return stook[: -len("STOOK")] + "SHADOW"


def _member_path(member: str, org: str) -> str:
    """refs/<org>/__agents__/a/STOOK -> __agents__/a"""
    head = f"refs/{org}/"
    if not member.startswith(head) or not member.endswith("/STOOK"):
        raise ValueError(f"a member is a STOOK under {head}: {member!r}")
    return member[len(head): -len("/STOOK")]


def resolve(repo: Path, member: str, *, org: str, remote: str = "origin") -> str:
    """The commit a member contributes: its pushed SHADOW, else its pushed STOOK, read on the tracking tree."""
    for ref in (shadow_of(member), member):
        tip = tracking.tracked(repo, ref, org=org, remote=remote)
        if tip:
            return _run(repo, "rev-parse", f"{tip}^{{commit}}").strip()
    raise Dangling(f"member {member} has no pushed SHADOW or STOOK on {remote}; fetch, or push it first")


def _held(repo: Path, sha: str) -> bool:
    return gitops.run(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def _walk(repo: Path, member: str, old: str, new: str, remote: str) -> str:
    """same, ff, rewind or rewrite, by a LOCAL ancestry walk (lease 2). A commit not
    held is fetched from the remote first; if it is still not held the walk refuses, offline included."""
    if old == new:
        return "same"
    for sha in (old, new):
        if not _held(repo, sha):
            got = gitops.run(repo, "fetch", "-q", "--no-tags", remote, sha)
            if not _held(repo, sha):
                raise CannotVerify(f"cannot verify {member}: {sha} not held"
                                   f" (fetch from {remote}: {(got.stderr or 'nothing fetched').strip()[:160]})")
    def descends(a: str, b: str) -> bool:
        proc = gitops.run(repo, "merge-base", "--is-ancestor", a, b)
        if proc.returncode not in (0, 1):
            raise CannotVerify(f"cannot verify {member}: the walk from {a} to {b} stopped: {proc.stderr.strip()[:160]}")
        return proc.returncode == 0
    if descends(old, new):
        return "ff"
    return "rewind" if descends(new, old) else "rewrite"


def _pins_of(repo: Path, commit: str) -> dict[str, str]:
    out = gitops.run(repo, "ls-tree", "-r", "-z", f"{commit}:{PINS}")
    if out.returncode:
        return {}
    pins = {}
    for line in out.stdout.split("\0"):
        if not line:
            continue
        meta, path = line.split("\t", 1)
        pins[path] = meta.split()[2]
    return pins


def _check_pins(pins: dict[str, str]) -> None:
    """Each pin path becomes nested tree entries, one per component; a component git fsck rejects is refused
    here, by pin, before any tree is written. So is a pin under another pin: one name would be both a gitlink and a
    tree in the same tree, the duplicate entry fsck's verify_ordered rejects."""
    for path in pins:
        for part in path.split("/"):
            bad = gitops.fsck_bad_name(part)
            if bad:
                raise ValueError(f"pin {path!r} has an entry git fsck rejects ({bad}): {part!r}")
        parts = path.split("/")
        for k in range(1, len(parts)):
            if "/".join(parts[:k]) in pins:
                raise ValueError(f"pin {path!r} is under pin {'/'.join(parts[:k])!r}; a name cannot be both")


def _tree_with_pins(repo: Path, base_tree: str, pins: dict[str, str]) -> str:
    """base_tree plus a ``pins`` tree of gitlinks, nested by path component."""
    _check_pins(pins)
    def build(entries: dict[str, str]) -> str:
        children: dict[str, dict[str, str]] = {}
        lines = []
        for path, sha in entries.items():
            first, _, rest = path.partition("/")
            if rest:
                children.setdefault(first, {})[rest] = sha
            else:
                lines.append(f"160000 commit {sha}\t{first}")
        for name, sub in children.items():
            lines.append(f"040000 tree {build(sub)}\t{name}")
        return _run(repo, "mktree", "-z",
                    data="".join(line + "\0" for line in sorted(lines, key=lambda l: l.split("\t", 1)[1]))).strip()

    top = [line for line in _run(repo, "ls-tree", "-z", base_tree).split("\0") if line]
    if any(line.split("\t", 1)[1] == PINS for line in top):
        raise ValueError(f"the record already has a top-level {PINS!r} entry; a shadow cannot pin beside it")
    top.append(f"040000 tree {build(pins)}\t{PINS}")
    return _run(repo, "mktree", "-z",
                data="".join(line + "\0" for line in sorted(top, key=lambda l: l.split("\t", 1)[1]))).strip()


def cut(repo: Path, stook: str, members: list[str], *, org: str, remote: str = "origin",
        message: str = "shadow", allow_rewind: bool = False, local: frozenset[str] = frozenset()) -> Cut:
    """Cut the SHADOW beside ``stook`` over ``members`` (STOOK refs in this repository).

    Lease 2: every member the previous SHADOW pinned must fast-forward to its new pin, checked by a local walk;
    a rewind or a rewrite is refused by member and both shas unless ``allow_rewind``, which records each one in
    the new SHADOW's message as an ``Allow-Rewind:`` line. New and removed members are allowed and named.
    A member in ``local`` (an on-demand close pushes it beside the SHADOW) is pinned at its local value, not the
    tracking tree's, so the SHADOW pins what the close lands (A10)."""
    shadow = shadow_of(stook)
    tracking.tracking_ref(stook, org=org, remote=remote)       # the namespace and names are checked here
    paths = [_member_path(m, org) for m in members]
    if len(set(paths)) != len(paths):
        raise ValueError("a member is named twice")
    pins = {p: _run(repo, "rev-parse", f"{m}^{{commit}}").strip() if m in local else
            resolve(repo, m, org=org, remote=remote) for p, m in zip(paths, members)}
    tip = _value(repo, stook)
    if tip is None:
        raise ValueError(f"no STOOK at {stook}")
    old = _value(repo, shadow)
    prev = _pins_of(repo, old) if old else {}
    rewound = []
    for path, new in pins.items():
        if path in prev:
            kind = _walk(repo, path, prev[path], new, remote)
            if kind in ("rewind", "rewrite"):
                if not allow_rewind:
                    raise MemberRewound(path, kind, prev[path], new)
                rewound.append(f"Allow-Rewind: {path} {kind} {prev[path]} -> {new}")
    if rewound:
        message = message.rstrip("\n") + "\n\n" + "\n".join(rewound) + "\n"
    tree = _tree_with_pins(repo, _run(repo, "rev-parse", f"{tip}^{{tree}}").strip(), pins)
    parents = ([old] if old else []) + [tip] + [s for s in dict.fromkeys(pins.values()) if s not in (old, tip)]
    args = ["commit-tree", tree, "-m", message]
    for p in parents:
        args += ["-p", p]
    sha = _run(repo, *args).strip()
    proc = gitops.run(repo, "update-ref", shadow, sha, old or "0" * 40)
    if proc.returncode:
        raise ShadowMoved(f"{shadow} moved during the cut: {proc.stderr.strip()[:200]}")
    return Cut(sha, sorted(set(pins) - set(prev)) if old else sorted(pins), sorted(set(prev) - set(pins)),
               [r.split()[1] for r in rewound])
