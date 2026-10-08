"""Merge a bound review into plain Git remotes: preflight every member, merge none on any failure.

The gate keys on the review id, not a hosting-platform PR. For each member it requires, read from
the member's remote: the feature branch still at the reviewed head, the target branch still at
the reviewed base, and a passing exact-head check record. Only when every member passes are
merges built (all before any push) and pushed, each under a lease on the base it was checked
against. The exit status is computed from the remote state after the run, not from this run's
actions: 0 all merged, 3 none merged (refused), 4 partial or unknown.

Not atomic across repositories: the pushes are separate. A lost acknowledgement is re-read, and
an unresolved member is `unknown`; no further member is pushed after one.
"""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Mapping, Sequence

from . import check_records, grip

EXIT_MERGED, EXIT_REFUSED, EXIT_PARTIAL = 0, 3, 4


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)
    if check and proc.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _remote_tip(repo: Path, remote: str, branch: str) -> str | None:
    """Fetch one remote branch into a private ref and return its commit, None when absent."""
    out = _git(repo, "ls-remote", "--heads", remote, f"refs/heads/{branch}")
    if not out:
        return None
    staging = f"refs/dev.synapt.grip/__merge_transfers__/{uuid.uuid4().hex}"
    try:
        _git(repo, "fetch", "--no-tags", "--no-write-fetch-head", remote, f"refs/heads/{branch}:{staging}")
        return _git(repo, "rev-parse", "--verify", f"{staging}^{{commit}}")
    finally:
        _git(repo, "update-ref", "-d", staging, check=False)


def _merged_at(repo: Path, target: str, head: str) -> str | None:
    """The commit on target's first-parent chain whose SECOND parent is head, if any."""
    for line in _git(repo, "rev-list", "--first-parent", "--parents", target).splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == head:
            return parts[0]
    return None


def _state(repo: Path, remote: str, into: str, head: str) -> tuple[str, str | None]:
    """Re-read one member from its remote: ('merged', commit) | ('unmerged', None) | ('unknown', None)."""
    try:
        tip = _remote_tip(repo, remote, into)
    except (RuntimeError, subprocess.SubprocessError, OSError):
        return "unknown", None
    if tip is None:
        return "unknown", None
    merged = _merged_at(repo, tip, head)
    return ("merged", merged) if merged else ("unmerged", None)


def review_merge(workspace: Path, review_id: str, *, into: str = "main", feature: str,
                 required_checks: Sequence[str] = ("test",)) -> tuple[int, dict]:
    review_id = review_id.removeprefix("gr:")
    grip.verify_review_commit(workspace, review_id)  # raises on a non-verifying bind
    view = grip.show_review_commit(workspace, review_id)
    rows: list[dict] = []
    for m in view["members"]:
        repo = (workspace / m["path"]).resolve()
        row = {"key": m["key"], "remote": m["remote"], "head": m["head"], "base": m["base"],
               "state": None, "merged": None, "refused": None, "check": None}
        rows.append(row)
        try:
            target = _remote_tip(repo, m["remote"], into)
            feat = _remote_tip(repo, m["remote"], feature)
        except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
            row["refused"] = f"remote_unmeasurable: {exc}"
            continue
        published = _git(repo, "ls-remote", m["remote"], f"refs/dev.synapt.grip/__reviews__/v1/{review_id}", check=False)
        if published.split("\t")[0] != review_id:
            row["refused"] = "review_not_on_member_remote: the remote does not hold this exact review"
            continue
        merged = _merged_at(repo, target, m["head"]) if target else None
        if merged:
            row.update(state="already_merged", merged=merged)
            continue
        if feat != m["head"]:
            row["refused"] = f"feature_moved: remote {feature} is {feat}, reviewed {m['head']}"
        elif target != m["base"]:
            row["refused"] = f"base_moved: remote {into} is {target}, reviewed base {m['base']}"
        elif _git(repo, "merge-base", m["head"], target, check=False) != target:
            row["refused"] = "base_not_ancestor_of_head"
        else:
            check = check_records.read_remote_check(
                m["remote"], {"path": str(repo), "key": m["key"], "remote": m["remote"]},
                m["head"], tuple(required_checks))
            row["check"] = {k: check.get(k) for k in ("status", "record_id", "reason")}
            if check["status"] != "pass":
                row["refused"] = f"check_{check['status']}: {check['reason']}"
            else:
                row["state"] = "ready"

    if not any(r["refused"] for r in rows):
        # Build every merge before any push.
        builds = {}
        for r, m in zip(rows, view["members"]):
            if r["state"] != "ready":
                continue
            repo = (workspace / m["path"]).resolve()
            proc = subprocess.run(["git", "-C", str(repo), "merge-tree", "--write-tree", m["base"], m["head"]],
                                  capture_output=True, text=True, timeout=60)
            if proc.returncode:
                r["refused"] = "merge_conflict"
                continue
            tree = proc.stdout.split()[0]
            builds[r["key"]] = _git(repo, "commit-tree", tree, "-p", m["base"], "-p", m["head"],
                                    "-m", f"Merge {feature} into {into} (review gr:{view['id'].removeprefix('gr:')})")
        if not any(r["refused"] for r in rows):
            for r, m in zip(rows, view["members"]):
                if r["state"] != "ready":
                    continue
                repo = (workspace / m["path"]).resolve()
                try:
                    still = _remote_tip(repo, m["remote"], feature)
                except (RuntimeError, subprocess.SubprocessError, OSError):
                    still = None
                if still != m["head"]:
                    r["refused"] = f"feature_moved_before_push: remote {feature} is {still}"
                    break  # narrows the head race; no further member is pushed
                proc = subprocess.run(
                    ["git", "-C", str(repo), "push", f"--force-with-lease=refs/heads/{into}:{m['base']}",
                     m["remote"], f"{builds[r['key']]}:refs/heads/{into}"],
                    capture_output=True, text=True, timeout=120)
                state, merged = _state(repo, m["remote"], into, m["head"])
                r.update(state=state, merged=merged)
                if proc.returncode and state != "merged":
                    r["push_error"] = proc.stderr.strip()[-300:]
                if state == "unknown":
                    break  # push no further members after an unresolved one

    # Exit from the remote STATE after the run.
    final = []
    for r, m in zip(rows, view["members"]):
        repo = (workspace / m["path"]).resolve()
        state, merged = _state(repo, m["remote"], into, m["head"])
        r["final"] = state
        if merged:
            r["merged"] = merged
        final.append(state)
    if all(s == "merged" for s in final):
        code = EXIT_MERGED
    elif all(s == "unmerged" for s in final):
        code = EXIT_REFUSED
    else:
        code = EXIT_PARTIAL
    return code, {"id": view["id"], "into": into, "feature": feature, "exit": code, "members": rows}
