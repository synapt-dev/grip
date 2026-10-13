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

from . import gitops
from . import tracking

PINS = "pins"


class Dangling(ValueError):
    """A member with no pushed STOOK or SHADOW on the remote; nothing was written."""


class ShadowMoved(RuntimeError):
    """The SHADOW ref moved while this cut was being made (a second cutter); nothing was written."""


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


def _tree_with_pins(repo: Path, base_tree: str, pins: dict[str, str]) -> str:
    """base_tree plus a ``pins`` tree of gitlinks, nested by path component."""
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
        return _run(repo, "mktree", data="".join(line + "\n" for line in sorted(lines, key=lambda l: l.split("\t")[1]))).strip()

    top = [line for line in _run(repo, "ls-tree", base_tree).splitlines() if line]
    if any(line.split("\t", 1)[1] == PINS for line in top):
        raise ValueError(f"the record already has a top-level {PINS!r} entry; a shadow cannot pin beside it")
    top.append(f"040000 tree {build(pins)}\t{PINS}")
    return _run(repo, "mktree", data="".join(line + "\n" for line in sorted(top, key=lambda l: l.split("\t")[1]))).strip()


def cut(repo: Path, stook: str, members: list[str], *, org: str, remote: str = "origin",
        message: str = "shadow") -> str:
    """Cut the SHADOW beside ``stook`` over ``members`` (STOOK refs in this repository); returns its commit."""
    shadow = shadow_of(stook)
    tracking.tracking_ref(stook, org=org, remote=remote)       # the namespace and names are checked here
    paths = [_member_path(m, org) for m in members]
    if len(set(paths)) != len(paths):
        raise ValueError("a member is named twice")
    pins = {p: resolve(repo, m, org=org, remote=remote) for p, m in zip(paths, members)}
    tip = _value(repo, stook)
    if tip is None:
        raise ValueError(f"no STOOK at {stook}")
    old = _value(repo, shadow)
    tree = _tree_with_pins(repo, _run(repo, "rev-parse", f"{tip}^{{tree}}").strip(), pins)
    parents = ([old] if old else []) + [tip] + [s for s in dict.fromkeys(pins.values()) if s not in (old, tip)]
    args = ["commit-tree", tree, "-m", message]
    for p in parents:
        args += ["-p", p]
    sha = _run(repo, *args).strip()
    proc = gitops.run(repo, "update-ref", shadow, sha, old or "0" * 40)
    if proc.returncode:
        raise ShadowMoved(f"{shadow} moved during the cut: {proc.stderr.strip()[:200]}")
    return sha
