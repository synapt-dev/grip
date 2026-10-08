"""Receipt-keyed staged-input cleanup (S4 cleanup slice).

Carved out of S4-C so a DELETE never shares a review surface with a copy. C
stages, projects, and publishes a receipt, and deletes nothing. This module is
the only thing in the system permitted to remove a staged input.

Contract: MaterializationPlan v1 staged-input durability invariant 8.

    "Do not delete a staged project-file input until the final materialization
    receipt containing that operation's evidence has been atomically published
    and made durable. A successful destination write or in-memory operation
    result is not acknowledgement. If cleanup is interrupted after receipt
    publication, rerun performs the idempotent cleanup from the verified
    receipt."


THE PAIRED INVARIANT WITH S4-C
------------------------------
Cleanup deletes the ONLY remaining copy of the projected content. That is
survivable for exactly one reason: C proves a projection is current from the
DESTINATION alone, by hashing it against source_sha256, so it never needs the
staged input again.

    Cleanup may delete a staged input only because C can re-establish currency
    from the destination alone. The two slices are safe only TOGETHER.

Neither module can state that on its own, which is why it is the thing most
likely to rot. TestCrossSliceSeam runs the whole sequence -- project, receipt,
cleanup, project again -- and is the probe for the seam itself.


WHERE AUTHORITY COMES FROM, AND WHY IT IS SPLIT
----------------------------------------------
    the RECEIPT answers    "MAY I delete?"      -- is this plan durably acknowledged
    the frozen PLAN answers "WHAT may I delete?" -- the validated paths

The obvious design reads source_path out of the receipt. That is wrong, and it
is validation-vs-use masking one more time: NOTHING IN THE RECEIPT IS SIGNED, so
anyone who can write that file can set plan_hash correctly -- it is a public
value, not a secret -- and point the evidence at someone's real work. Those path
strings were validated as PLAN fields; the receipt is a different artifact read
at a later time, and its bytes are not the validated ones.

Invariant 8 says cleanup runs "from the VERIFIED receipt", and verification is
exactly the receipt's job. Taking the paths from the immutable capability loses
nothing, because a plan_hash match forecloses any receipt/plan disagreement.

Working, not exhaustively hardened (Layne's 2026-07-27 prototype doctrine) --
but the delete-side guards are not the place to economise, so the acknowledgement
gate and the never-escalate rule are complete.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from .spec_apply import (
    MaterializationPlanError,
    ValidatedPlan,
    _read_canonical_workspace_spec_bytes,
    canonicalize_workspace_path,
    compute_plan_hash,
    materialization_receipt_path,
)


class StagingCleanupError(MaterializationPlanError):
    """A staged input could not be removed safely."""


def _require_workspace_binding(validated: ValidatedPlan, workspace_root: Path) -> None:
    try:
        spec_bytes = _read_canonical_workspace_spec_bytes(workspace_root)
    except MaterializationPlanError as exc:
        raise StagingCleanupError(
            f"cannot clean up against {workspace_root}: its canonical WorkspaceSpec "
            f"is unreadable ({exc})"
        ) from exc
    if hashlib.sha256(spec_bytes).hexdigest() != validated.workspace_spec_sha256:
        raise StagingCleanupError(
            f"plan is bound to WorkspaceSpec {validated.workspace_spec_sha256} but "
            f"{workspace_root} has a different one -- deleting against a workspace "
            "this plan was not validated for would resolve every path elsewhere"
        )


def _load_acknowledging_receipt(
    validated: ValidatedPlan, *, workspace_root: Path, receipt_path: Path
) -> dict[str, object]:
    """The MAY-I half. Returns the receipt only if it durably acknowledges THIS
    plan; raises otherwise.

    Three checks, each seeing a failure the others cannot:

      - CANONICAL LOCATION. The receipt is the authority to delete. Accepting
        one from an arbitrary path makes that authority forgeable by anyone who
        can write a file anywhere in the workspace.
      - EXISTENCE. Invariant 8's core prohibition. A successful projection and a
        correct destination are explicitly NOT acknowledgement.
      - PLAN_HASH, not plan_id. plan_id is an opaque token that may repeat
        across successive plans for the same unit, so matching it alone would
        let yesterday's receipt authorise today's deletions. The bound is the
        CONTENT hash."""
    canonical = materialization_receipt_path(workspace_root, validated.plan_id)
    if receipt_path.resolve() != canonical.resolve():
        raise StagingCleanupError(
            f"receipt {receipt_path} is not the canonical receipt for this plan "
            f"({canonical}) -- the authority to delete is not accepted from an "
            "arbitrary path"
        )
    if not canonical.is_file():
        raise StagingCleanupError(
            f"no durable receipt at {canonical} -- invariant 8 forbids removing a "
            "staged input before the receipt carrying its evidence is published, and "
            "a successful destination write is not acknowledgement"
        )
    try:
        receipt = json.loads(canonical.read_text())
    except json.JSONDecodeError as exc:
        raise StagingCleanupError(f"receipt {canonical} is not valid JSON: {exc}") from exc

    if receipt.get("plan_id") != validated.plan_id:
        raise StagingCleanupError(
            f"receipt at {canonical} does not acknowledge this plan: it records "
            f"plan_id {receipt.get('plan_id')!r}, not {validated.plan_id!r}"
        )
    expected_hash = compute_plan_hash(validated.plan)
    if receipt.get("plan_hash") != expected_hash:
        raise StagingCleanupError(
            f"receipt at {canonical} records plan_hash {receipt.get('plan_hash')!r}, "
            f"not this plan's {expected_hash!r} -- an opaque plan_id may be reused "
            "across re-emissions, so the content hash is the bound"
        )
    return receipt


def _remove_staged_input(path: Path) -> bool:
    """Remove one staged input. Returns False if it was already gone.

    NEVER escalates. Between C's execute and this call the path could have
    become a directory; the tempting fix is rmtree and the correct answer is to
    refuse. unlink() on a symlink removes the LINK rather than its target, which
    is already the safe behaviour, so the input that actually matters here is a
    directory."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return False
    if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
        raise StagingCleanupError(
            f"staged input {path} is not a regular file -- refusing to remove it. "
            "Cleanup never escalates to a recursive delete, whatever is sitting at "
            "a path the plan expected a file at"
        )
    path.unlink()
    return True


def cleanup_staged_inputs(
    validated: ValidatedPlan, *, workspace_root: Path, receipt_path: Path
) -> list[str]:
    """Remove the staged inputs of a durably acknowledged plan.

    Returns the workspace-relative paths actually removed -- empty when there
    was nothing left to do, which is what makes a rerun after an interrupted
    cleanup a no-op rather than an error. Invariant 8's tail requires exactly
    that: the caller cannot know which state it is in, and neither the receipt
    nor the filesystem records whether cleanup ran.

    Paths come from the frozen plan, never from the receipt -- see the module
    docstring. The receipt says whether deletion is permitted; it does not get
    to say what gets deleted."""
    workspace_root = Path(os.fspath(workspace_root))
    validated.verify(require_provenance=True)
    _require_workspace_binding(validated, workspace_root)
    _load_acknowledging_receipt(
        validated, workspace_root=workspace_root, receipt_path=receipt_path
    )

    removed: list[str] = []
    failures: list[str] = []
    for index, op in enumerate(validated.plan["operations"]):
        if op.get("kind") != "project_file":
            continue
        declared = str(op["source_path"])
        source = canonicalize_workspace_path(
            workspace_root, declared, field_name=f"operations[{index}].source_path"
        )
        # Attempt every input, then report. Aborting on the first failure would
        # let one stuck file block cleanup of the others forever, and cleanup is
        # idempotent precisely so a partial pass is safe to repeat.
        try:
            if _remove_staged_input(source):
                removed.append(declared)
        except StagingCleanupError as exc:
            failures.append(str(exc))

    if failures:
        raise StagingCleanupError(
            "some staged inputs could not be removed:\n" + "\n".join(failures)
        )
    return removed
