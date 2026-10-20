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
    # "name (reason)" for every member whose checkout moved but could not be measured. The caller refuses the whole
    # set on any of these: binding the others would mint one review for part of a change set.
    refused: tuple[str, ...] = ()
    # names of the members whose base is the live remote head of their ref (the workspace records no pin for them)
    live: frozenset[str] = frozenset()


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def workspace_members(workspace_root: Path) -> list[Member]:
    return [
        Member(m["name"], _text(m.get("path")) or m["name"], _text(m.get("url")),
               _text(m.get("ref")), lane_downstream.pin_of(m))
        for m in (lane_downstream.load_members(workspace_root) or [])
    ]


def changed_member_rows(workspace_root: Path, only: list[str] | None = None) -> Choice:
    """One bind row per member whose pin is an ancestor of its distinct checkout head. A member with no pin takes the live
    remote head of its ref as base instead, and is refused by name when its HEAD is detached, when that head is not an
    ancestor of its checkout (behind), or when such members sit on different branches. A member with no url, no checkout
    or an unreadable head is not chosen and is named with the reason. A member whose head differs from its pin but
    whose ancestry cannot be read (the pin is not in the checkout, say) is named in ``refused``: it may carry a change
    this function cannot measure, so the caller must not bind the rest without it. ``only`` narrows to the named members and
    names any that the workspace does not declare."""
    members = workspace_members(workspace_root)
    declared = {m.name for m in members}
    rows: list[dict] = []
    refused: list[str] = []
    branches: dict[str, str] = {}  # member -> branch, for the members whose base came from the live head
    skipped: list[str] = [f"{n} (not a member of this workspace)" for n in (only or []) if n not in declared]
    for m in members:
        if only is not None and m.name not in only:
            skipped.append(f"{m.name} (excluded by --members)")
            continue
        checkout = workspace_root / m.path
        if m.url is None:
            skipped.append(f"{m.name} (no remote url recorded)")
            continue
        head = gitops.git(checkout, "rev-parse", "--verify", "HEAD") if checkout.is_dir() else None
        if head is None or head.returncode != 0:
            skipped.append(f"{m.name} (no checkout at {checkout})")
            continue
        sha = head.stdout.strip()
        ref = m.ref if m.ref and m.ref.startswith("refs/") else f"refs/heads/{m.ref or 'main'}"
        if m.pin is None:
            # No pin recorded (no `store init`): the base is the live remote head of the member's ref, the one base
            # the bind accepts, read here so a member that cannot take it is named before any id is minted.
            branch = gitops.git(checkout, "symbolic-ref", "-q", "--short", "HEAD")
            if branch.returncode != 0:
                refused.append(f"{m.name}: detached HEAD and no workspace pin; check out the feature branch or name the row")
                continue
            live, why = _live_head(checkout, m.url, ref)
            if live is None:
                refused.append(f"{m.name} (no workspace pin, and the live head of {ref} could not be read: {why})")
                continue
            if sha == live:
                skipped.append(f"{m.name} (the workspace records no pin and its checkout is at the live head of {ref}; "
                               "`gr2 store init` records one)")
                continue
            ancestry = gitops.ancestry_proc(checkout, live, sha, via_cwd=True)
            if ancestry.returncode != 0:
                refused.append(f"{m.name} is behind {ref}; rebase onto it" if ancestry.returncode == 1 else
                               f"{m.name} (no workspace pin, and the live head of {ref} {live[:12]} is not in its "
                               "checkout; fetch it)")
                continue
            branches[m.name] = branch.stdout.strip()
            rows.append(_row(m, ref, sha, live, checkout))
            continue
        if sha == m.pin:
            skipped.append(f"{m.name} (checkout is at the workspace's pin)")
            continue
        ancestry = gitops.ancestry_proc(checkout, m.pin, sha, via_cwd=True)
        if ancestry.returncode == 1:
            skipped.append(f"{m.name} (checkout does not descend from the workspace's pin)")
            continue
        if ancestry.returncode != 0:
            detail = (ancestry.stderr or ancestry.stdout or "").strip().splitlines()
            refused.append(f"{m.name} (ancestry could not be read): pin {m.pin[:12]}, checkout {sha[:12]}"
                           + (f", git said: {detail[0]}" if detail else ""))
            continue
        rows.append(_row(m, ref, sha, m.pin, checkout))
    if len(set(branches.values())) > 1:
        # One review is one change set; with no pin to say which commits belong to it, the branch is the only claim.
        refused.append("members on different branches and no workspace pin: "
                       + ", ".join(f"{n} on {b}" for n, b in branches.items()))
    return Choice(rows, skipped, tuple(refused), frozenset(branches))


def _row(m: Member, ref: str, head: str, base: str, checkout: Path) -> dict:
    return {"key": m.name, "remote": m.url, "path": m.path, "head": head, "base": base,
            "ref": ref, "title": "", "body": "", "source": str(checkout.resolve())}


def _live_head(checkout: Path, url: str, ref: str) -> tuple[str | None, str]:
    """(oid, "") for the remote's row named exactly ``ref``, else (None, why)."""
    proc = gitops.git(checkout, "ls-remote", "--", url, ref)
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        return None, detail[0] if detail else f"git ls-remote exited {proc.returncode}"
    try:
        oid = gitops.ls_remote_oid(proc.stdout, ref)
    except ValueError as exc:  # LsRemoteMalformed is a ValueError
        return None, str(exc)
    return (oid, "") if oid else (None, "the remote holds no such ref")
