"""The one name validator, in two profiles. A verb that takes a name checks it here before its first write.

STRICT is for the names gr2 mints in its own ref namespace (an agent record's org, agent and ref): one
conservative lowercase ref component. GIT-REF is for a name a user hands to their own git (a lane, a branch, a
store member, a PR target): what ``git check-ref-format --allow-onelevel`` accepts, and printable, with no
leading "-". Both raise ``ValueError`` naming the rule the name breaks.
"""
from __future__ import annotations

import re

from . import gitops

_COMPONENT = re.compile(r"[a-z0-9][a-z0-9._-]*")


def strict(name: str, what: str) -> str:
    """A caller's name only ever sits as ONE conservative ref component: lowercase letters,
    digits, ".", "_" and "-", starting with a letter or digit, never containing "..", never
    ending in "." and never ending in ".lock". A name with "/" would nest below another agent's
    ref (two records aliasing to one ref) or below the fold's ``*`` (which does not cross "/", so
    its writes would be on disk and silently left out of every fold). Lowercase only because a
    case-insensitive filesystem (macOS by default) folds "Alice" and "alice" into ONE loose ref
    under the files backend: the second agent's write would take the first one's tip as its
    parent, and the fold would read a fork as a write that saw the other. Every rule of ``git check-ref-format`` that a single
    component can break is refused here, before any object is written: the character class
    excludes control characters, space, "~", "^", ":", "?", "*", "[", "\\" and "@" (so "@{" and
    the single "@"), and a leading "."; "..", a trailing "." and a ".lock" suffix are checked
    after it. The one uppercase name is the default ref ``STOOK``; its lowercase twin
    ``stook`` is refused, since the two are one file on such a filesystem."""
    if what == "ref" and name == "STOOK":
        return name
    if (not isinstance(name, str) or not _COMPONENT.fullmatch(name) or ".." in name
            or name.endswith(".") or name.endswith(".lock") or (what == "ref" and name == "stook")):
        raise ValueError(f"{what} must be one lowercase ref component ([a-z0-9][a-z0-9._-]*, "
                         f"no '..', no trailing '.', no '.lock' suffix): "
                         f"{gitops._shown_arg(name) if isinstance(name, str) else name!r}")
    return name


_FORBIDDEN = set(" ~^:?*[\\")


def _git_ref_problem(name: object) -> str | None:
    """The first rule ``name`` breaks, or None. git's rules (check-ref-format, one level allowed) come first, then
    the three of ours that git does not make: printable only, no leading "-", and no leading or trailing space,
    which git's space rule already covers but is named here so the message says what the user typed."""
    if not isinstance(name, str) or not name:
        return "it is empty"
    if name != name.strip():
        return "it has a leading or trailing space"
    if not name.isprintable():
        return "it holds a character that is not printable"
    if name.startswith("-"):
        return "it starts with '-'"
    if name == "@":
        return "it is the single character '@'"
    if any(ord(c) < 0x20 or ord(c) == 0x7F or c in _FORBIDDEN for c in name):
        return "it holds a space, a control character, or one of ~ ^ : ? * [ \\"
    if "@{" in name:
        return "it holds '@{'"
    if ".." in name:
        return "it holds '..'"
    if name.startswith("/") or name.endswith("/") or "//" in name:
        return "it starts or ends with '/', or holds '//'"
    if name.endswith("."):
        return "it ends with '.'"
    for part in name.split("/"):
        if part.startswith("."):
            return "a '/'-separated part starts with '.'"
        if part.endswith(".lock"):
            return "a '/'-separated part ends with '.lock'"
    return None


def git_ref(name: str, what: str) -> str:
    """``name`` when git can hold it as a ref name and a person can read it; otherwise ValueError naming the rule."""
    problem = _git_ref_problem(name)
    if problem is not None:
        raise ValueError(f"{what} {name!r} is not a usable git name: {problem}")
    return name
