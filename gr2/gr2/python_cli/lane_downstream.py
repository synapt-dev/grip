"""Members of the workspace that a review lane does not bind, brought into the lane at their PIN.

A review lane binds the repositories that changed. "Tested together, before merge" also needs the members that
depend on them, and the members those need installed, each at the commit the workspace records for it: never at
whatever its remote's default branch points at today. The pin is the ``pin`` field of the member's entry in the
workspace spec, read through ``pin_of`` (the one accessor, so a later move of pins into state refs changes one
function). Everything here refuses rather than fall through to the remote tip: a green over a tip the workspace
never declared is exactly the claim this exists to prevent.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import gitops

_SHA40 = re.compile(r"^[0-9a-f]{40}$")


class PinRefused(Exception):
    """An unchanged member cannot be put at its pin. ``reason`` is the one sentence; the member is named."""

    def __init__(self, member: str, reason: str) -> None:
        super().__init__(f"member {member!r}: {reason}")
        self.member, self.reason = member, reason


def pin_of(repo_spec: dict) -> str | None:
    """The commit the workspace records for this member, or None when there is no usable one (absent, empty, or
    not a full lowercase 40-hex sha, which is also what a branch name or a short sha looks like)."""
    pin = repo_spec.get("pin")
    if isinstance(pin, str) and _SHA40.match(pin):
        return pin
    return None


def unchanged_members(spec: dict, lane_keys: list[str]) -> list[dict]:
    """The workspace members the lane does not bind, in spec order. A lane key names a member by its spec name."""
    bound = set(lane_keys)
    return [r for r in spec.get("repos", []) if isinstance(r, dict) and r.get("name") and r["name"] not in bound]


def materialize_at_pin(repo_spec: dict, dest: Path, *, workspace_root: Path) -> str:
    """Clone one unchanged member into ``dest`` at its pin and return the pin. ONE path for every way this can
    fail: no usable pin and a pin the clone cannot reach both refuse as ``PinRefused``, and a clone that did not
    land on the pin is refused too, because ``clone_and_pin`` answers "was this the first materialization", not
    "is the member at the pin" (an existing clone is reported as done without being looked at).

    An empty pin is refused HERE, before any clone: ``clone_and_pin`` treats an empty pin as "stay on the
    default branch", which is the fall-through this refuses."""
    from .clone_exec import CloneExecutionError, clone_and_pin
    from .spec_apply import repo_cache_path

    name = str(repo_spec["name"])
    pin = pin_of(repo_spec)
    if pin is None:
        raise PinRefused(name, "the workspace records no usable pin for it (a full 40-hex commit is required)")
    url = repo_spec.get("url")
    if not isinstance(url, str) or not url:
        raise PinRefused(name, f"the workspace records a pin {pin[:12]} but no url to fetch it from")
    try:
        clone_and_pin(
            url,
            dest,
            pin=pin,
            member=name,
            reference_repo_root=repo_cache_path(workspace_root, name),
        )
    except (SystemExit, CloneExecutionError, OSError) as exc:  # checkout_declared_pin refuses with SystemExit
        raise PinRefused(name, f"its pin {pin[:12]} could not be materialized ({exc})") from exc
    head = gitops.git(dest, "rev-parse", "--verify", "HEAD")
    landed = head.stdout.strip() if head.returncode == 0 else ""
    if landed != pin:
        raise PinRefused(name, f"it is at {landed[:12] or 'no commit'} in the lane, not at its pin {pin[:12]}")
    return pin


def tree_at(dest: Path, pin: str) -> str:
    """The tree id of the pinned commit: what a pinned member's tracked content must equal for the whole run."""
    out = gitops.git(dest, "rev-parse", "--verify", f"{pin}^{{tree}}")
    if out.returncode != 0 or not out.stdout.strip():
        raise PinRefused(dest.name, f"its pin {pin[:12]} has no tree in the lane clone")
    return out.stdout.strip()
