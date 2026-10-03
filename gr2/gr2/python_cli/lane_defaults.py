"""What `lane create` can default from what the workspace already holds: the repos (all of them) and the branch
(the lane's own name), with the one check that makes the branch default safe, whether that name is already taken
on a remote by work the lane does not start from.

The repos come from the workspace spec and from the root's ``grip.toml`` (a root made by ``store init`` alone has
only the second), read through the same loader downstream uses, so a malformed file refuses here exactly as there.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import NamedTuple

from . import lane_downstream

#: ONE budget for the whole remote check, not per repository: ten unreachable remotes must not wait ten times.
REMOTE_CHECK_BUDGET_S = 20.0


class Repo(NamedTuple):
    name: str
    url: str | None
    ref: str | None  # the integration branch a lane forks from, when the workspace names one


def workspace_repos(workspace_root: Path) -> list[Repo]:
    """Every repo the workspace declares, in declaration order, spec first and grip.toml members after.
    Raises ``lane_downstream.MembersUnreadable`` for a file that cannot be read."""
    from .spec_apply import workspace_spec_path

    found: dict[str, Repo] = {}
    spec_path = workspace_spec_path(workspace_root)
    if spec_path.is_file():
        for r in lane_downstream._read_toml(spec_path).get("repos", []):
            if isinstance(r, dict) and isinstance(r.get("name"), str) and r["name"]:
                found.setdefault(r["name"], Repo(r["name"], _text(r.get("url")), _text(r.get("ref") or r.get("default_branch"))))
    root_path = workspace_root / "grip.toml"
    if root_path.is_file():
        for m in lane_downstream._read_toml(root_path).get("members", []):
            if isinstance(m, dict) and isinstance(m.get("name"), str) and m["name"]:
                remotes = m.get("remotes") if isinstance(m.get("remotes"), dict) else {}
                found.setdefault(m["name"], Repo(m["name"], _text(remotes.get("origin")), _text(m.get("ref"))))
    return list(found.values())


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


class Collision(NamedTuple):
    repo: str
    branch: str
    remote_tip: str
    base_ref: str
    base_tip: str


class RemoteCheck(NamedTuple):
    collisions: list[Collision]
    not_checked: list[str]  # repos whose remote was not asked, and why, as "name (reason)"


def _ls_remote(url: str, ref: str, timeout: float) -> str | None:
    """The sha ``ref`` points at on ``url``, "" when the remote answered and has no such ref. Raises
    ``TimeoutError`` or ``OSError`` when the remote could not be asked."""
    try:
        out = subprocess.run(
            ["git", "ls-remote", url, ref], capture_output=True, text=True, timeout=max(timeout, 0.1),
            env={**__import__("os").environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("did not answer in time") from exc
    if out.returncode != 0:
        raise OSError((out.stderr.strip().splitlines() or ["ls-remote failed"])[-1])
    line = out.stdout.split("\n", 1)[0].strip()
    return line.split()[0] if line else ""


def check_remote_branch(repos: list[Repo], branch: str, *, budget: float = REMOTE_CHECK_BUDGET_S) -> RemoteCheck:
    """For each repo with a url: is ``refs/heads/<branch>`` on its remote at a commit other than the tip of the
    integration branch the lane forks from? The first remote that cannot be asked ends the check (it spends the
    one budget), and every repo not asked is named, so nothing reads as checked that was not."""
    deadline = time.monotonic() + budget
    collisions: list[Collision] = []
    not_checked: list[str] = []
    for i, repo in enumerate(repos):
        if time.monotonic() >= deadline:
            not_checked.extend(f"{r.name} (the {budget:g} s budget was spent)" for r in repos[i:])
            break
        if repo.url is None:
            not_checked.append(f"{repo.name} (no url recorded)")
            continue
        try:
            remote_tip = _ls_remote(repo.url, f"refs/heads/{branch}", deadline - time.monotonic())
            if not remote_tip:
                continue
            base_ref = repo.ref or "HEAD"
            full = base_ref if base_ref == "HEAD" or base_ref.startswith("refs/") else f"refs/heads/{base_ref}"
            base_tip = _ls_remote(repo.url, full, deadline - time.monotonic())
        except (TimeoutError, OSError) as exc:
            not_checked.extend([f"{repo.name} ({exc})", *(f"{r.name} (not asked)" for r in repos[i + 1:])])
            break
        if base_tip != remote_tip:
            collisions.append(Collision(repo.name, branch, remote_tip, base_ref, base_tip or "unknown"))
    return RemoteCheck(collisions, not_checked)
