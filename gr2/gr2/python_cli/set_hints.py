"""A hint, printed and never recorded: a member in NO declared set still names something a set moved off.

The merge gate is exact over a declared set and, by design, silent about members outside every set: a set is
declared in grip.toml, never inferred from what a commit moves. So a consumer outside the set can merge with every
gate green and stay broken. This module says so out loud, beside the gate, without changing it.

What it looks for: identifiers on a set member's removed lines that none of its added lines keeps (its change runs
from the gitlink at the root commit's first parent, or, when that is absent or the same, from the merge-base with the
member clone's origin/HEAD, to the gitlink at the root commit), searched as whole words at the
pinned head of every member in no declared set. Identifier-shaped names (an underscore, a digit or an inner
capital) are always kept and listed first, so a real rename is not crowded out of the cap; a plain word the set
member still uses anywhere at its own head was not moved off (this drops the English of a deleted comment), which
also drops a plain-word rename whose old name survives in the set member. It is still lexical,
so it is wrong both ways: a unit or type change under the same name is invisible, and a common removed name can
hit everywhere. That is why it is printed and capped, never written into a receipt, and never changes an exit
code; any error yields no hint.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import declared_sets, gitops

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
MAX_LINES = 5
MAX_PATHS = 3


def _removed_names(repo: Path, base: str, head: str) -> set[str]:
    diff = gitops.run(repo, "diff", "-U0", "--no-color", "--no-ext-diff", base, head)
    if diff.returncode:
        return set()
    minus: set[str] = set()
    plus: set[str] = set()
    for line in diff.stdout.splitlines():
        if line.startswith("-") and not line.startswith("---"):
            minus.update(_IDENT.findall(line[1:]))
        elif line.startswith("+") and not line.startswith("+++"):
            plus.update(_IDENT.findall(line[1:]))
    return minus - plus


def _paths_by_name(repo: Path, names: list[str], head: str) -> dict[str, list[str]]:
    """Every path at `head` naming each of `names` as a whole word, in ONE git grep, not one per name. The names go
    in on stdin (`-f -`), so a large removal never meets the argument-length limit."""
    if not names:
        return {}
    found = gitops.run(repo, "grep", "-w", "-F", "-o", "-f", "-", head, input="".join(f"{n}\n" for n in names))
    if found.returncode:
        return {}
    by_name: dict[str, set[str]] = {}
    for line in found.stdout.splitlines():
        path, sep, name = line.removeprefix(f"{head}:").rpartition(":")
        if sep:
            by_name.setdefault(name, set()).add(path)
    return {name: sorted(paths) for name, paths in by_name.items()}


def _shaped(name: str) -> bool:
    """An identifier, not a word: an underscore, a digit or an inner capital."""
    return "_" in name or any(c.isdigit() for c in name) or any(c.isupper() for c in name[1:])


def _identifier_first(name: str) -> tuple[bool, str]:
    return (not _shaped(name), name)


def _merge_base_with_default(repo: Path, head: str) -> str | None:
    """Where the member's change starts when the root commit's parent does not say: its merge-base with the clone's
    record of the remote default branch (origin/HEAD, which `git clone` sets)."""
    base = gitops.run(repo, "merge-base", head, "refs/remotes/origin/HEAD")
    return base.stdout.strip() or None if base.returncode == 0 else None


def _pins(root: Path, revision: str, declaration: declared_sets.Declaration, names) -> dict[str, str]:
    pins, _missing = declared_sets.gitlinks(root, revision, declaration, declared_sets.DeclaredSet("", tuple(names)))
    return pins


def out_of_set_hints(workspace: Path, root_commit: str, set_name: str) -> list[str]:
    """Hint lines for set `set_name` at `root_commit`, at most MAX_LINES plus one line naming how many were left out."""
    try:
        root = Path(workspace).resolve()
        declaration = declared_sets.read(root, root_commit)
        dset = declaration.set_named(set_name)
        if dset is None:
            return []
        in_a_set = {m for s in declaration.sets for m in s.members}
        outside = sorted(m for m in declaration.members if m not in in_a_set)
        if not outside:
            return []
        heads = _pins(root, root_commit, declaration, dset.members)
        parents = _pins(root, f"{root_commit}^", declaration, dset.members)
        moved_off: dict[str, list[str]] = {}
        for member in dset.members:
            head = heads.get(member)
            if not head:
                continue
            repo = root / declaration.members[member]["path"]
            base = parents.get(member)
            if not base or base == head:
                base = _merge_base_with_default(repo, head)
            if not base or base == head:
                continue
            removed = sorted(_removed_names(repo, base, head))
            still_named = _paths_by_name(repo, [n for n in removed if not _shaped(n)], head)
            for name in removed:
                if _shaped(name) or name not in still_named:
                    moved_off.setdefault(name, []).append(member)
        if not moved_off:
            return []
        reader_heads = _pins(root, root_commit, declaration, outside)
        hits = []
        for reader in outside:
            if reader not in reader_heads:
                continue
            repo = root / declaration.members[reader]["path"]
            named = _paths_by_name(repo, sorted(moved_off), reader_heads[reader])
            hits.extend((reader, name, paths) for name, paths in named.items())
        hits.sort(key=lambda hit: (*_identifier_first(hit[1]), hit[0]))
    except Exception:  # a hint never changes what the gate decided, so it never raises either
        return []
    lines = []
    for reader, name, paths in hits[:MAX_LINES]:
        shown = ", ".join(paths[:MAX_PATHS]) + (f" and {len(paths) - MAX_PATHS} more" if len(paths) > MAX_PATHS else "")
        lines.append(f"hint: {reader} is in no declared set and still names `{name}` in {shown}; "
                     f"set {set_name} moved off it ({', '.join(moved_off[name])}). Declare {reader} into the set "
                     f"if it reads what the set changes.")
    if len(hits) > MAX_LINES:
        lines.append(f"hint: {len(hits) - MAX_LINES} more out-of-set name(s) not shown")
    return lines
