"""Declared sets: which members of a store must be merged together, named once in the tracked `grip.toml`.

    [[sets]]
    name = "core"
    members = ["alpha", "beta", "gamma"]

A review is judged one member at a time, so five single-member reviews of one change never form a multi-member review
and the merge gate had nothing to require a joint check of. A declared set is the coupling written down where every
workspace of the store reads the same bytes, and it cannot be left out by whoever splits the reviews.

The gate reads the MERGING workspace's committed `grip.toml` (HEAD), not its working file and not any history: a
member of a declared set merges only when a `declared_set` record, written by `check set --root-commit`, names a root
commit that pins EVERY member of the set at heads that include this review's head. The record stores the member list
it was made for, and an edited declaration does not ride an old record.
"""
from __future__ import annotations

import re
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from . import gitops
from .keys import member_path_key

DECLARED_SET_CHECK = "declared_set"
_HEX40 = re.compile(r"[0-9a-f]{40}")
# A full root's set name (spec 14.1): the writer (griplink._set_lines) and this reader take the same names.
SET_NAME = re.compile(r"[A-Za-z0-9._-]+")
# One rule for both shapes, so the store root (grip.toml) and the full root (.gitmodules) refuse in the same words.
ONE_SET_PER_MEMBER = "a member belongs to at most one set"


def _two_sets(member: str, first: str, second: str) -> str:
    return f"member {member!r} is in two sets ({first!r} and {second!r}); {ONE_SET_PER_MEMBER}"


class DeclaredSetError(ValueError):
    """A declaration that cannot be read, named so the author can fix grip.toml or .gitmodules."""


@dataclass(frozen=True)
class DeclaredSet:
    name: str
    members: tuple[str, ...]  # sorted member names


@dataclass(frozen=True)
class Declaration:
    sets: tuple[DeclaredSet, ...]
    members: Mapping[str, Mapping[str, str]]  # name -> {"path": ..., "remote": ...}
    mode: str = "compat"  # grip.toml `mode`: a full-mode entry is not a member commit, so this reader refuses it by name
    # Where the sets were read: a store root's grip.toml, or a full root's .gitmodules (spec 14.1), whose gitlink at a
    # member's path IS the member's commit, so mode_refusal does not apply to it.
    source: str = "grip.toml"

    def set_named(self, name: str) -> DeclaredSet | None:
        return next((s for s in self.sets if s.name == name), None)

    def member_name(self, key: str, path: str) -> str | None:
        """The store member a review member is: its key is a member's name, or its path is a member's path, both sides
        as member keys (``./alpha`` and ``alpha/`` are ``alpha``; ``a/../alpha`` is not)."""
        wanted = member_path_key(path) if path else path
        for name, member in self.members.items():
            if name == key or member_path_key(member["path"]) == wanted:
                return name
        return None

    def set_of(self, member: str) -> DeclaredSet | None:
        return next((s for s in self.sets if member in s.members), None)


def parse(document: Mapping) -> Declaration:
    """The declaration in a parsed grip.toml document. Refuses at read: a set naming an unknown member, a member in
    two sets (which would leave it unclear which record is required), a repeated or empty set, a malformed entry."""
    mode = document.get("mode", "compat")
    if mode not in ("compat", "full"):
        raise DeclaredSetError(f'mode must be "compat" or "full", got {mode!r}')
    members: dict[str, dict[str, str]] = {}
    for member in document.get("members", []) or []:
        remotes = member.get("remotes", {}) if isinstance(member, Mapping) else {}
        if not isinstance(member, Mapping) or not isinstance(member.get("name"), str) or not isinstance(member.get("path"), str):
            continue  # the member reader owns the refusal for a malformed member
        origin = remotes.get("origin") if isinstance(remotes, Mapping) else None
        members[member["name"]] = {"path": member_path_key(member["path"]), "remote": origin if isinstance(origin, str) else ""}
    raw = document.get("sets", [])
    if raw in (None, []):
        return Declaration((), members, mode)
    if not isinstance(raw, list):
        raise DeclaredSetError("sets must be an array of [[sets]] tables")
    sets: list[DeclaredSet] = []
    owner: dict[str, str] = {}
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, Mapping):
            raise DeclaredSetError(f"[[sets]] entry {index} is not a table")
        name = entry.get("name")
        listed = entry.get("members")
        if not isinstance(name, str) or not name.strip():
            raise DeclaredSetError(f"[[sets]] entry {index} needs a name")
        if any(s.name == name for s in sets):
            raise DeclaredSetError(f"set {name!r} is declared twice")
        if not isinstance(listed, list) or not listed or any(not isinstance(m, str) or not m for m in listed):
            raise DeclaredSetError(f"set {name!r} needs members, a list of member names")
        if len(set(listed)) != len(listed):
            raise DeclaredSetError(f"set {name!r} lists a member twice")
        for member in listed:
            if member not in members:
                raise DeclaredSetError(f"set {name!r} names unknown member {member!r}")
            if member in owner:
                raise DeclaredSetError(_two_sets(member, owner[member], name))
            owner[member] = name
        sets.append(DeclaredSet(name, tuple(sorted(listed))))
    return Declaration(tuple(sets), members, mode)


def _blob(root: Path, revision: str) -> str | None:
    """The committed grip.toml at `revision`, or None when there is no such commit or no document in it."""
    found = gitops.run(root, "rev-parse", "--verify", "-q", f"{revision}^{{commit}}")
    if found.returncode:
        return None
    shown = gitops.run(root, "show", f"{revision}:grip.toml")
    return shown.stdout if shown.returncode == 0 else None


def _is_full_root(root: Path) -> bool:
    from .workspace_guidance import is_full_mode_root  # lazy: the finder reads the disk shape, most readers never need it
    return is_full_mode_root(root)


def _gitmodules_parse_error(root: Path, blob: str) -> str | None:
    """The named refusal when the committed .gitmodules `blob` does not parse, else None. On a blob, --get-regexp skips
    a malformed line with a warning and still exits 0 or 1, so a broken .gitmodules would read as no sets; --list exits
    128 on the same blob, so it is the parse check."""
    parsed = gitops.run(root, "config", "--blob", blob, "--list")
    if parsed.returncode:
        return f"gitmodules_unreadable: git config --blob {blob} exited {parsed.returncode}: {parsed.stderr.strip()}"
    return None


def _gitmodules_rows(root: Path, blob: str, pattern: str) -> list[tuple[str, str]]:
    """(key, value) for every `pattern` key in the committed .gitmodules `blob`; rc 1 is "none", any other nonzero refuses
    (14.1 step 3: --get-regexp exits 1 for no match and the gate must not read a failed read as no sets). The parse check
    runs first. Read with -z: each record is "key\\nvalue" ended by NUL ("key" alone for a key with no value), because a
    section name may hold a space (git submodule add --name "my api") and a space-split line would cut the key there."""
    if (unreadable := _gitmodules_parse_error(root, blob)):
        raise DeclaredSetError(unreadable)
    got = gitops.run(root, "config", "--blob", blob, "-z", "--get-regexp", pattern)
    if got.returncode == 1:
        return []
    if got.returncode:
        raise DeclaredSetError(f"gitmodules_unreadable: git config --blob {blob} exited {got.returncode}: {got.stderr.strip()}")
    return [(key, value) for key, _, value in (record.partition("\n") for record in got.stdout.split("\0") if record)]


MEMBER_REF = "refs/dev.synapt.grip/__members__/{name}/STOOK"


def _member_name_refusal(root: Path, name: str) -> str | None:
    """Why `name` cannot be a full root's member, else None. Two halves, each with its own words: git's submodule name
    rule (ported), then git check-ref-format on the member ref the name would make (run, never ported)."""
    if gitops.submodule_name_bad(name):
        return "is not a name git allows for a submodule (empty, or a '..' part)"
    ref = MEMBER_REF.format(name=name)
    checked = gitops.run(root, "check-ref-format", ref)
    if checked.returncode:
        return f"does not make a valid member ref (git check-ref-format {ref} exited {checked.returncode})"
    return None


def _refuse_member_name(root: Path, commit: str, name: str) -> None:
    """Refuses the WHOLE read on a disallowed section name, before any member is kept, so nothing is pinned or written."""
    if (reason := _member_name_refusal(root, name)):
        raise DeclaredSetError(
            f"submodule_name_disallowed: member {name!r} in .gitmodules at {commit[:12]} {reason}; {_REWRITE_STEP} "
            f"with git config -f .gitmodules --rename-section {shlex.quote('submodule.' + name)} 'submodule.<new>', "
            "then run the verb again")


_REWRITE_STEP = ("a commit on top does not recover: rewrite the commit that added the name (git commit --amend, or git "
                 "rebase), renaming the section")


def _refuse_name_in_history(root: Path, commit: str) -> None:
    """The ported submodule name rule over every root .gitmodules in `commit`'s history, so a name renamed away on top
    still refuses. Walks only the commits reachable from `commit` that touch .gitmodules, never the whole store, so a
    blob a rewrite left behind does not count. --full-history, because the default walk drops a side branch whose
    merge matches a parent and never reads the bad blob on it. A past blob that does not parse is skipped, as git skips
    it; the tip blob refuses through _gitmodules_rows (14.1 step 3)."""
    walk = gitops.run(root, "rev-list", "--full-history", commit, "--", ".gitmodules")
    if walk.returncode:
        raise DeclaredSetError(f"gitmodules_unreadable: git rev-list {commit[:12]} -- .gitmodules exited "
                               f"{walk.returncode}: {walk.stderr.strip()}")
    added: dict[str, str] = {}  # blob -> the oldest walked commit holding it, the one that added it
    for past in walk.stdout.split():
        held = gitops.run(root, "rev-parse", "--verify", "--quiet", f"{past}:.gitmodules")
        if held.returncode == 0:
            added[held.stdout.strip()] = past
    for blob, past in added.items():
        if _gitmodules_parse_error(root, blob):
            continue  # git's submodule config and fsck read names with the same parser, so neither reads one here
        listed = gitops.run(root, "config", "--blob", blob, "-z", "--name-only", "--list")
        for key in listed.stdout.split("\0"):
            name, dot, _ = key[len("submodule."):].rpartition(".")
            if key.startswith("submodule.") and dot and gitops.submodule_name_bad(name):
                raise DeclaredSetError(f"submodule_name_disallowed: member {name!r} in .gitmodules at {past[:12]}, in "
                                       f"the history of {commit[:12]}, is not a name git allows for a submodule "
                                       f"(empty, or a '..' part); {_REWRITE_STEP}, then run the verb again")


def _read_full_root(root: Path, commit: str) -> Declaration:
    """A full root's sets from its root COMMIT's .gitmodules, never the worktree (spec 14.1, at most ONE gr2-set line per
    member section). Members are the sections' path and url, so a member's pin is its gitlink at that path."""
    blob = f"{commit}:.gitmodules"
    if gitops.run(root, "cat-file", "-e", blob).returncode:
        return Declaration((), {}, source="gitmodules")  # 14.1 step 2: no .gitmodules, no members, no sets
    members: dict[str, dict[str, str]] = {}
    for key, value in _gitmodules_rows(root, blob, r"^submodule\..*\.(path|url)$"):
        name, field = key[len("submodule."):].rsplit(".", 1)
        if name not in members:
            _refuse_member_name(root, commit, name)
        members.setdefault(name, {"path": "", "remote": ""})["path" if field == "path" else "remote"] = (
            member_path_key(value) if field == "path" else value)
    owner: dict[str, str] = {}
    for key, value in _gitmodules_rows(root, blob, r"^submodule\..*\.gr2-set$"):
        member = key[len("submodule."):-len(".gr2-set")]
        if member not in members:
            _refuse_member_name(root, commit, member)
        if not SET_NAME.fullmatch(value):
            raise DeclaredSetError(f"set_name_invalid: member {member!r} names set {value!r} in .gitmodules at "
                                   f"{commit[:12]}; a set name is letters, digits, '.', '_' and '-'")
        if member in owner:
            raise DeclaredSetError(_two_sets(member, owner[member], value))
        if not members.get(member, {}).get("path"):
            raise DeclaredSetError(f"set_member_unknown: set {value!r} names {member!r}, which has no path in .gitmodules "
                                   f"at {commit[:12]}")
        owner[member] = value
    _refuse_name_in_history(root, commit)
    sets: dict[str, list[str]] = {}
    for member, name in owner.items():
        sets.setdefault(name, []).append(member)
    for name, listed in sets.items():
        if len(listed) < 2:  # 14.4: a set left with one member is named, never silently passed
            raise DeclaredSetError(f"set_with_one_member: set {name!r} names only {listed[0]!r} at {commit[:12]}; a set "
                                   "couples two or more members, so add a member to it or remove its line")
    return Declaration(tuple(DeclaredSet(n, tuple(sorted(ms))) for n, ms in sorted(sets.items())), members,
                       source="gitmodules")


def read(root: Path, revision: str = "HEAD") -> Declaration:
    """The declaration committed at `revision` (HEAD by default): a store root's grip.toml, or a full root's .gitmodules
    (spec section 14). A full root whose root commit is not here REFUSES, never reads as no sets (14.1 step 1); a
    store root with no commit or no grip.toml still means no sets."""
    root = Path(root)
    found = gitops.run(root, "rev-parse", "--verify", "-q", f"{revision}^{{commit}}")
    if found.returncode or not found.stdout.strip():
        if _is_full_root(root):
            raise DeclaredSetError(f"root_commit_absent: {revision!r} is not a commit of this full root; fetch it, then "
                                   "read its sets")
        return Declaration((), {})
    commit = found.stdout.strip()
    text = _blob(root, commit)
    if text is None:
        return _read_full_root(root, commit)
    # A store root reads grip.toml; a .gitmodules beside it that does not parse cannot hold gr2-set lines the gate
    # would honour, so it is not read here and does not change what grip.toml declares.
    if gitops.run(root, "cat-file", "-e", f"{commit}:.gitmodules").returncode == 0 and \
            _gitmodules_parse_error(root, f"{commit}:.gitmodules") is None and \
            _gitmodules_rows(root, f"{commit}:.gitmodules", r"^submodule\..*\.gr2-set$"):
        raise DeclaredSetError(f"both_set_shapes: the commit {commit[:12]} holds grip.toml and gr2-set lines in .gitmodules; "
                               "a store root declares its sets in grip.toml only, so remove the gr2-set lines")
    try:
        return parse(tomllib.loads(text))
    except tomllib.TOMLDecodeError as exc:
        raise DeclaredSetError(f"grip.toml at {revision} is not valid TOML: {exc}") from exc


def gitlinks(root: Path, revision: str, declaration: Declaration, dset: DeclaredSet) -> tuple[dict[str, str], list[str]]:
    """({member: gitlink sha}, [members the commit does not pin]) for every member of `dset` at `revision`."""
    pins: dict[str, str] = {}
    missing: list[str] = []
    for name in dset.members:
        path = declaration.members[name]["path"]
        entry = gitops.run(Path(root), "ls-tree", revision, "--", path).stdout.split()
        if len(entry) >= 3 and entry[0] == "160000" and _HEX40.fullmatch(entry[2]):
            pins[name] = entry[2]
        else:
            missing.append(name)
    return pins, missing


def mode_refusal(declaration: Declaration) -> str | None:
    """The named refusal for a workspace whose grip.toml says `mode = "full"`, else None.

    Declared sets are judged in compat mode only. Whether the readers here can judge a full-mode tree is not yet
    measured, so a full-mode workspace refuses by name rather than being judged by a reader that was never checked
    against it."""
    if declaration.mode == "full":
        return 'declared_set_mode_full: grip.toml says mode = "full"; declared sets are judged in compat mode only'
    return None


def judge(root: Path, declaration: Declaration, dset: DeclaredSet, member: str, head: str,
          rows: Sequence[Mapping], tie: Callable[[str, str], str | None] | None = None) -> str | None:
    """None when a `declared_set` record covers this member at this head, else the named refusal.

    Judged against the workspace, never against the record's own claims: the root commit must exist here, pin every
    member of the set as it is declared NOW, and pin this review's member at this head. `tie`, when given, is asked
    about every OTHER member of the set and the head the root commit pins it at (member name, pin), and returns the
    named refusal for a pin nobody reviewed or merged, else None."""
    if (full := mode_refusal(declaration)):
        return full
    named =[r for r in rows if r.get("name") == DECLARED_SET_CHECK and r.get("set_name") == dset.name]
    if not named:
        return (f"declared_set_absent: {member} belongs to set {dset.name!r}, which needs one record over all of "
                f"{list(dset.members)}; commit the workspace with every pin, run `check set --root-commit <sha>`, then merge")
    by_commit: dict[str, list[Mapping]] = {}
    for row in named:
        by_commit.setdefault(str(row.get("root_commit")), []).append(row)
    # A failure belongs to the PINS it was run over, not to the commit id that carried them: an empty commit gives the
    # same pins a new id, and a pass there must not outvote the failure (the per-member `test` check is newest-wins at
    # the same heads instead; a declared set stays sticky by pins on purpose). A failure at pins
    # that have since changed is outvotable: that is fixing the code and running the check again.
    failed_pins = {_pins_of(r) for r in named if r.get("result") != "pass"}
    reasons: list[str | None] = []
    for commit, group in by_commit.items():
        if any(r.get("result") != "pass" for r in group):
            reasons.append(f"declared_set_fail: the set check for {dset.name!r} failed at root commit {commit[:12]}")
        elif _pins_of(group[0]) in failed_pins:
            reasons.append(f"declared_set_fail: the set check for {dset.name!r} failed over these same pins at another "
                           f"root commit; a pass at {commit[:12]} does not outvote it, change a pin and run it again")
        else:
            reasons.append(_judge_one(Path(root), declaration, dset, member, head, group[0], tie))
    return None if None in reasons else reasons[0]


def _pins_of(row: Mapping) -> tuple | None:
    """The head list a record was made over, as a comparable key (None when the record carries no readable list)."""
    listed = row.get("heads")
    return tuple(listed) if isinstance(listed, list) else None


def _judge_one(root: Path, declaration: Declaration, dset: DeclaredSet, member: str, head: str, row: Mapping,
               tie: Callable[[str, str], str | None] | None = None) -> str | None:
    recorded = row.get("declared_members")
    if not isinstance(recorded, list) or sorted(recorded) != list(dset.members):
        return (f"declared_set_changed: set {dset.name!r} is declared as {list(dset.members)} now and the record "
                f"was made for {recorded}; run `check set --root-commit` again")
    commit = row.get("root_commit")
    if not isinstance(commit, str) or not _HEX40.fullmatch(commit) \
            or gitops.run(root, "cat-file", "-e", f"{commit}^{{commit}}").returncode:
        return f"declared_set_root_unknown: root commit {commit!r} is not in this workspace"
    pins, missing = gitlinks(root, commit, declaration, dset)
    if missing:
        return f"declared_set_member_unpinned: root commit {commit[:12]} does not pin {missing} of set {dset.name!r}"
    listed = row.get("heads")
    if listed != sorted(f"{name}:{sha}" for name, sha in pins.items()):
        return f"declared_set_record_disagrees: the record's heads are not what root commit {commit[:12]} pins"
    if pins[member] != head:
        return (f"declared_set_head_mismatch: root commit {commit[:12]} pins {member} at {pins[member]}, "
                f"this review is at {head}")
    if tie is not None:
        for other in dset.members:
            if other != member and (why := tie(other, pins[other])):
                return why
    return None
