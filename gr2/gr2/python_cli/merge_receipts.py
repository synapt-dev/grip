"""Every `review merge` run's receipt, kept in the workspace root's own `.git`.

One ref per review, `refs/dev.synapt.grip/__merges__/v1/<review sha>`, holding one commit per run whose parent is
the previous run, so a rerun appends and never overwrites. Local only: nothing here is pushed.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import grip

MERGE_REF_PREFIX = "refs/dev.synapt.grip/__merges__/v1/"
_BLOB = "receipt.json"


def _ref(review_id: str) -> str:
    return MERGE_REF_PREFIX + review_id.removeprefix("gr:")


def _tip(workspace: Path, ref: str) -> str | None:
    proc = grip._bind_git(workspace, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
    return proc.stdout.strip() if proc.returncode == 0 else None


def record(workspace: Path, review_id: str, receipt: dict) -> str:
    """Append this run's receipt; returns the new commit. Compare-and-swap on the previous tip."""
    ref = _ref(review_id)
    parent = _tip(workspace, ref)
    blob = grip._bind_blob(workspace, json.dumps(receipt, indent=2, sort_keys=True, default=str) + "\n")
    tree = grip._bind_mktree(workspace, [f"100644 blob {blob}\t{_BLOB}"])
    commit = grip._bind_commit_tree(workspace, tree, parent=parent,
                                    message=f"gr2 merge receipt: exit {receipt.get('exit')}")
    proc = grip._bind_git(workspace, "update-ref", ref, commit, parent or grip._ZERO_OID)
    if proc.returncode != 0:
        raise RuntimeError(f"update-ref {ref} failed: {proc.stderr.strip()}")
    return commit


def _shaped(receipt: object) -> dict:
    """The receipt if it has the shape `review show` reads; valid JSON of any other shape is as corrupt as invalid JSON."""
    members = receipt.get("members") if isinstance(receipt, dict) else None
    if not isinstance(members, list) or not all(
            isinstance(m, dict) and isinstance(m.get("key"), str)
            and (m.get("refusal") is None
                 or (isinstance(m.get("refusal"), dict) and isinstance(m["refusal"].get("code"), str)))
            for m in members):
        raise ValueError("not a merge receipt")
    return receipt


def runs(workspace: Path, review_id: str) -> list[dict]:
    """Every stored receipt for the review, oldest first; [] when it was never merged."""
    ref = _ref(review_id)
    if _tip(workspace, ref) is None:
        return []
    commits = grip._bind_git(workspace, "rev-list", "--reverse", "--first-parent", ref).stdout.split()
    out = []
    for c in commits:
        proc = grip._bind_git(workspace, "cat-file", "blob", f"{c}:{_BLOB}")
        try:
            if proc.returncode != 0:
                raise ValueError(f"no {_BLOB}")
            out.append(_shaped(json.loads(proc.stdout)))
        except ValueError as exc:  # json's decode error is a ValueError too
            raise grip.GripCorruptError(f"merge receipt {c} under {ref} is unreadable: {exc}") from exc
    return out
