"""Hostile names for any gr2 verb that takes a member, lane, unit, branch or ref name, and a refuse-before-write check.

A verb refuses a name cleanly only when it exits non-zero AND leaves nothing behind. rc alone is not enough: a verb
that writes a ref or a lane file and then refuses has already done the damage the refusal was for. So
`classify` snapshots the workspace before and after the call:

- every ref, in the root and in every git repository under it (`git for-each-ref`);
- every file path under the root, with its size and mtime, outside `.git` directories;
- the loose and packed object counts of each repository (`git count-objects -v`), since a record written and then
  refused leaves its object even when no ref names it;
- the entries of the root's parent directory, since a verb that makes its own root may make it and then refuse.

and returns one of `accepted`, `refused-clean` or `refused-dirty`, plus what changed.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Each class a reader would try first. A list of (label, name) so a test id never carries the raw bytes.
HOSTILE_NAMES: list[tuple[str, str]] = [
    ("slash", "/"),
    ("nested", "a/b"),
    ("dotdot", ".."),
    ("dot", "."),
    ("empty", ""),
    ("lead-space", " lead"),
    ("trail-space", "trail "),
    ("inner-space", "a b"),
    ("dash", "-flag"),
    ("at", "@"),
    ("reflog-at", "a@{x}"),
    ("lock", "x.lock"),
    ("double-dot", "a..b"),
    ("tilde", "~x"),
    ("colon", "x:y"),
    ("line-separator", "x y"),  # not printable, and outside C0/C1: isprintable() is False
]


@dataclass
class Snapshot:
    refs: dict[str, str] = field(default_factory=dict)
    files: dict[str, tuple[int, int]] = field(default_factory=dict)
    objects: dict[str, str] = field(default_factory=dict)
    siblings: set[str] = field(default_factory=set)


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
    return done.stdout if done.returncode == 0 else ""


def _repos(root: Path) -> list[Path]:
    if not root.exists():
        return []
    found = [root] if (root / ".git").exists() else []
    for dirpath, dirnames, _ in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for d in dirnames:
            if (Path(dirpath) / d / ".git").exists():
                found.append(Path(dirpath) / d)
    return found


def snapshot(root: Path) -> Snapshot:
    snap = Snapshot()
    for repo in _repos(root):
        key = str(repo.relative_to(root)) if repo != root else "."
        for line in _git(repo, "for-each-ref", "--format=%(refname) %(objectname)").splitlines():
            ref, _, sha = line.partition(" ")
            snap.refs[f"{key}:{ref}"] = sha
        counts = {k: v for k, _, v in (l.partition(": ") for l in _git(repo, "count-objects", "-v").splitlines())}
        snap.objects[key] = f"loose={counts.get('count')} packed={counts.get('in-pack')}"
    if root.exists():
        for dirpath, dirnames, filenames in os.walk(root):
            if ".git" in dirnames:
                dirnames.remove(".git")
            for name in filenames:
                path = Path(dirpath) / name
                st = path.lstat()
                snap.files[str(path.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    if root.parent.exists():
        snap.siblings = {p.name for p in root.parent.iterdir()}
    return snap


def changes(before: Snapshot, after: Snapshot) -> list[str]:
    out = []
    for kind in ("refs", "files", "objects"):
        a, b = getattr(before, kind), getattr(after, kind)
        out += [f"{kind} + {k}" for k in sorted(set(b) - set(a))]
        out += [f"{kind} - {k}" for k in sorted(set(a) - set(b))]
        out += [f"{kind} ~ {k}" for k in sorted(set(a) & set(b)) if a[k] != b[k]]
    out += [f"sibling + {s}" for s in sorted(after.siblings - before.siblings)]
    return out


@dataclass
class Outcome:
    verdict: str  # accepted | refused-clean | refused-dirty
    rc: int
    output: str
    changed: list[str]


def classify(root: Path, run: Callable[[], tuple[int, str]]) -> Outcome:
    """Run `run()` (it returns rc and its output) between two snapshots of `root`."""
    before = snapshot(root)
    rc, output = run()
    changed = changes(before, snapshot(root))
    if rc == 0:
        return Outcome("accepted", rc, output, changed)
    return Outcome("refused-dirty" if changed else "refused-clean", rc, output, changed)
