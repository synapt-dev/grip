"""`gr2 review stamp`, in a module of its own so the dispatch table can build this one verb.

The full CLI registers this same function, so the verb `gr2 review stamp` runs is one function on
both paths; only what is imported before it runs differs (see dispatch.py)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from . import grip, next_steps
from .root_option import ROOT_OPTION


def review_approve(
    workspace_root: Path,
    review_id: Optional[str] = typer.Argument(None, help="Review id; default is the pinned review at the current member heads"),
    root: Optional[Path] = ROOT_OPTION,
) -> None:
    """Stamp the review: append an unsigned exact-head approval as GIT_AUTHOR_NAME, else the workspace git user.name, on every member remote.

    WORKSPACE_ROOT may be a lane `review open` made, or one member directory in it: the stamp then uses the store
    of the workspace the lane was opened from and the members checked out in the lane, so a reviewer in another
    gripspace stamps from the lane. The approver is GIT_AUTHOR_NAME, else the git user.name of the workspace the lane was opened from."""
    from . import approvals
    opened = None
    try:
        # A review lane (`review checkout-pr` or `review open`) carries its (repo, head) record; stamp binds to it.
        opened = approvals.review_open_lane(workspace_root.resolve()) if review_id is None else None
        if opened is not None:
            # A `review open` lane: the store is the workspace it was opened from, the members are read here.
            store, rid, members = opened
            result = approvals.approve(store, rid, members)
        elif review_id is None and approvals.pr_lane_record(workspace_root.resolve()) is not None:
            result = approvals.approve_pr_lane(workspace_root.resolve())
        else:
            result = approvals.approve(workspace_root.resolve(), review_id)
    except grip.ReviewStoreAbsent as exc:
        # The review was never received here (a reviewer's own workspace): a named refusal and the order that
        # works, never a traceback.
        typer.echo(f"refused: not_received: {exc}", err=True)
        rid = review_id or "<review id>"
        typer.echo(next_steps.not_received(str(workspace_root), rid if rid.startswith(("gr:", "<")) else f"gr:{rid}"),
                   err=True)
        raise typer.Exit(code=2)
    except approvals.ApprovalRefused as exc:
        typer.echo(str(exc), err=True)
        if getattr(exc, "next_step", None):
            typer.echo(exc.next_step, err=True)
        if str(exc) == "self_approval" and not getattr(exc, "next_step", None):
            # A `review open` lane stamped against the store it was opened from: the policy, the review and the
            # merge the next line prints are that store's, never the lane's (a lane has no grip.toml).
            here = opened[0] if opened is not None else workspace_root.resolve()
            try:
                rid = review_id or (opened[1] if opened is not None else approvals.current_review(here))
            except Exception:  # the next line is advice; a review id we cannot resolve still leaves the command shape
                rid = "<review id>"
            try:
                floor = approvals.approval_policy(here, 0)["required"]
            except approvals.ApprovalRefused:  # unreadable or invalid policy: the merge refuses it too
                floor = None
            typer.echo(next_steps.self_approval(str(here if opened is not None else workspace_root),
                                                rid if rid.startswith(("gr:", "<")) else f"gr:{rid}", floor), err=True)
        raise typer.Exit(code=2)
    typer.echo(json.dumps(result))
