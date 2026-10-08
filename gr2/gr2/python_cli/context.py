"""One resolver for the context a verb needs: the root, the unit, the lane.

A verb used to infer each of these on its own, silently, or made the caller type what the
workspace already held. Here every value comes back as ``(value, source)``, from the same four
places in the same order: what the caller typed (``explicit``), a record the user already wrote (the
entered lane, the spec's only unit), the place they stand in (the nearest workspace above the current
directory). Anything not explicit is announced on stderr in one line naming its source, except a
root that is the current directory itself, which is not a choice gr2 made. ``GR2_QUIET_CONTEXT=1``
or ``quiet=True`` suppresses the lines for a harness that treats stderr as failure.

Ambiguity refuses and says how to pick each reading; nothing is guessed between two.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..prototypes import lane_workspace_prototype as lane_proto
from .layout import grip_dir
from . import gitops


@dataclass(frozen=True)
class Resolved:
    value: str
    source: str  # "explicit", or a short phrase naming the record or place it came from

    @property
    def inferred(self) -> bool:
        return self.source != "explicit"

    def to_dict(self) -> dict[str, str]:
        return {"value": self.value, "source": self.source}


class ContextRefused(RuntimeError):
    """The context cannot be resolved without a choice only the caller can make."""


@dataclass(frozen=True)
class ReviewContext:
    lane: Path
    workspace: Path
    commit: str


def resolve_review_context(cwd: Optional[Path] = None) -> Optional[ReviewContext]:
    """The enclosing reconstruction's recorded subject, or no enclosing lane.

    Marker selection and compatibility belong to review_run. Bad or ambiguous
    context refuses; it must not turn into an ambient workspace/sole-bind lookup.
    """
    from . import review_run

    try:
        lane = review_run.resolve_run_lane(None, cwd=cwd)
        marker = review_run._read_marker(lane)
    except review_run.ReviewRunRefused as exc:
        if exc.code == "no_review_context":
            return None
        raise ContextRefused(f"{exc.code}: {exc.detail}") from exc
    recorded = marker.get("workspace_root")
    if not isinstance(recorded, str) or not recorded or not Path(recorded).is_absolute():
        raise ContextRefused("bad_review_workspace: the review marker has no absolute workspace_root; pass an explicit root")
    workspace = Path(recorded).resolve()
    if not _is_workspace_root(workspace):
        raise ContextRefused(f"bad_review_workspace: recorded workspace {workspace} is missing or not a workspace")
    commit = marker.get("gr_commit")
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", commit) is None:
        raise ContextRefused("bad_review_commit: the review marker has no full gr_commit; pass an explicit target")
    return ReviewContext(lane=lane, workspace=workspace, commit=f"gr:{commit}")


def quiet_from_env(env: Optional[dict[str, str]] = None) -> bool:
    return (env if env is not None else os.environ).get("GR2_QUIET_CONTEXT", "") not in ("", "0")


def _is_workspace_root(path: Path) -> bool:
    from .app import _is_workspace_root as is_root

    return is_root(path)


def resolve_root(explicit: Optional[Path], cwd: Optional[Path] = None) -> Resolved:
    if explicit is not None:
        return Resolved(str(explicit.resolve()), "explicit")
    here = (cwd or Path.cwd()).resolve()
    for path in (here, *here.parents):
        if _is_workspace_root(path):
            where = "the current directory" if path == here else f"nearest workspace above {here}"
            return Resolved(str(path), where)
    raise ContextRefused(f"no workspace at or above {here}; run this inside one, or name it with -C <root>")


def spec_units(root: Path) -> list[str]:
    spec = grip_dir(root) / "workspace_spec.toml"
    if not spec.is_file():
        return []
    try:
        doc = tomllib.loads(spec.read_text())
    except tomllib.TOMLDecodeError:
        return []
    return [str(unit["name"]) for unit in doc.get("units", []) if isinstance(unit, dict) and "name" in unit]


def entered_units(root: Path) -> dict[str, str]:
    """{unit: current lane} for every unit whose current-lane record names a lane (a record that was
    exited holds ``{"current": null}`` and does not count)."""
    folder = lane_proto.current_lane_file(root, "x").parent
    found: dict[str, str] = {}
    if folder.is_dir():
        for path in sorted(folder.glob("*.json")):
            try:
                current = json.loads(path.read_text()).get("current")
            except (OSError, ValueError):
                continue
            if current and current.get("lane_name"):
                found[path.stem] = str(current["lane_name"])
    return found


def resolve_unit(root: Path, explicit: Optional[str]) -> Resolved:
    if explicit is not None:
        return Resolved(explicit, "explicit")
    entered = entered_units(root)
    if len(entered) == 1:
        (unit,) = entered
        return Resolved(unit, "the only unit with an entered lane")
    if len(entered) > 1:
        raise ContextRefused(
            f"several units have an entered lane ({', '.join(sorted(entered))}); name one with --unit <name>"
        )
    units = spec_units(root)
    if len(units) == 1:
        return Resolved(units[0], "the only unit in the workspace spec")
    if units:
        raise ContextRefused(f"the workspace has several units ({', '.join(sorted(units))}); name one with --unit <name>")
    raise ContextRefused("no unit found in this workspace; name one with --unit <name>")


def resolve_lane(root: Path, unit: str, explicit: Optional[str] = None) -> Resolved:
    if explicit is not None:
        return Resolved(explicit, "explicit")
    lane = entered_units(root).get(unit)
    if lane is None:
        raise ContextRefused(f"unit {unit} has no entered lane; name one with --lane <name>")
    return Resolved(lane, f"the current lane of unit {unit}")


class ActorRefused(ContextRefused):
    """No actor was named and none can be derived; the exit code for it is 4."""


def resolve_actor(
    explicit: Optional[str], *, env: Optional[dict[str, str]] = None, tty: Optional[bool] = None,
    git_name: Optional[str] = None,
) -> Resolved:
    """Who is acting. Only from what the caller or the environment says: ``--actor`` (explicit), else the
    neutral variable ``GR2_ACTOR`` (whoever launches a session sets it; gr2 never derives an agent's
    identity itself), else ``human:<git user.name>`` for a person at a terminal, else REFUSE. There is no
    default label: a hard-coded identity in an audit trail is the hazard this exists to remove."""
    if explicit:
        return Resolved(explicit, "explicit")
    environment = env if env is not None else os.environ
    named = environment.get("GR2_ACTOR", "").strip()
    if named:
        return Resolved(named, "env GR2_ACTOR")
    on_terminal = sys.stdin.isatty() if tty is None else tty
    if on_terminal:
        name = git_name
        if name is None:
            try:
                probe = gitops.run_argv(["git", "config", "user.name"])
                name = probe.stdout.strip() if probe.returncode == 0 else ""
            except OSError:  # git is not installed or not on PATH: there is no name to use, so refuse below
                name = ""
        if name:
            return Resolved(f"human:{name}", "git user.name")
    raise ActorRefused(
        "no actor: pass --actor <label>, or set GR2_ACTOR (for example agent:atlas); at a terminal gr2 "
        "uses human:<git user.name> when git has one"
    )


def announce(items: dict[str, Resolved], *, root: Resolved, quiet: bool, stream=None) -> None:
    """One stderr line per value that was inferred; the root only when it is not the current directory."""
    if quiet:
        return
    out = stream if stream is not None else sys.stderr
    for name, item in items.items():
        if not item.inferred:
            continue
        if name == "root" and item.source == "the current directory":
            continue
        print(f"gr2: {name}={item.value} ({item.source})", file=out)


def context_dict(items: dict[str, Resolved]) -> dict[str, dict[str, str]]:
    return {name: item.to_dict() for name, item in items.items()}
