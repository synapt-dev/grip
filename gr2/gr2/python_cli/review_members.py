"""The members a `review bind` can bind without being told: the ones whose checkout is ahead of its pin.

Reads the workspace spec and the root's ``grip.toml`` through the loader downstream uses (a malformed file refuses
there, by name), takes every member's pin through the one validator, and compares it to the head of the member's own
checkout. Nothing here binds anything; it only chooses rows, and the caller prints them before it binds.
"""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from . import gitops, lane_downstream


class Member(NamedTuple):
    name: str
    path: str
    url: str | None
    ref: str | None
    pin: str | None  # a full 40-hex commit, or None when the workspace records none


class Choice(NamedTuple):
    rows: list[dict]
    skipped: list[str]  # "name (reason)" for every member not chosen, so a short list never reads as a complete one


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def workspace_members(workspace_root: Path) -> list[Member]:
    return [
        Member(m["name"], _text(m.get("path")) or m["name"], _text(m.get("url")),
               _text(m.get("ref")), lane_downstream.pin_of(m))
        for m in (lane_downstream.load_members(workspace_root) or [])
    ]


def changed_member_rows(workspace_root: Path, only: list[str] | None = None) -> Choice:
    """One bind row per member whose pin is an ancestor of its distinct checkout head. A member with no pin, no url, no checkout
    or an unreadable head is not chosen and is named with the reason. ``only`` narrows to the named members and
    names any that the workspace does not declare."""
    members = workspace_members(workspace_root)
    declared = {m.name for m in members}
    rows: list[dict] = []
    skipped: list[str] = [f"{n} (not a member of this workspace)" for n in (only or []) if n not in declared]
    for m in members:
        if only is not None and m.name not in only:
            skipped.append(f"{m.name} (excluded by --members)")
            continue
        checkout = workspace_root / m.path
        if m.pin is None:
            skipped.append(f"{m.name} (no usable pin recorded)")
            continue
        if m.url is None:
            skipped.append(f"{m.name} (no remote url recorded)")
            continue
        head = gitops.git(checkout, "rev-parse", "--verify", "HEAD") if checkout.is_dir() else None
        if head is None or head.returncode != 0:
            skipped.append(f"{m.name} (no checkout at {checkout})")
            continue
        sha = head.stdout.strip()
        if sha == m.pin:
            skipped.append(f"{m.name} (checkout is at its pin)")
            continue
        ancestry = gitops.git(checkout, "merge-base", "--is-ancestor", m.pin, sha)
        if ancestry.returncode == 1:
            skipped.append(f"{m.name} (checkout does not descend from its pin)")
            continue
        if ancestry.returncode != 0:
            skipped.append(f"{m.name} (ancestry could not be read)")
            continue
        rows.append({
            "key": m.name, "remote": m.url, "path": m.path, "head": sha, "base": m.pin,
            "ref": m.ref if m.ref and m.ref.startswith("refs/") else f"refs/heads/{m.ref or 'main'}", "title": "", "body": "", "source": str(checkout.resolve()),
        })
    return Choice(rows, skipped)
