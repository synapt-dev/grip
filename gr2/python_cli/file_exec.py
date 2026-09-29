"""Executor for the neutral MaterializationPlan `project_file` operation (S4-C).

The operation copies one declared staged input to one declared destination
inside the unit, and refuses unless the bytes hash to the `source_sha256` the
plan declares. gr2 reads only the plan: it does not know what the file is for,
and nothing here depends on its contents.

Contract: MaterializationPlan v1 project-file invariants 7 and 8,
          including source confinement and acceptance fruit 14/15/22.

INVARIANT 7 IS A TRAP AS LITERALLY WORDED. It says verify the source's
"reopened bytes match source_sha256". Read word for word -- reopen, hash, then
copy -- that IS the read-1/read-2 TOCTOU: the bytes hashed and the bytes written
come from two different reads and nothing binds them, so the conforming-sounding
implementation is the vulnerable one. `verify_staged_source` therefore RETURNS
the bytes it hashed, and the caller writes exactly those. One read. Same
conclusion S4-A reached about consume(): the fix is not to check again, it is to
stop re-reading.

INVARIANT 8 IS SPLIT ACROSS TWO SLICES. It forbids deleting a staged input
before the receipt carrying its evidence is durable, and names the wrong signal
outright -- "a successful destination write or in-memory operation result is not
acknowledgement". That is a purely NEGATIVE constraint, so this slice satisfies
it by DELETING NOTHING. Receipt-keyed cleanup is its own later slice, because a
delete driven by a file on disk deserves its own review surface.

That carve has a consequence this module must carry: once cleanup has run there
is no staged input left, so an executor that demanded one would fail on the
system's own second apply. A destination already hashing to source_sha256 IS
proof the projection is current, so that case short-circuits before the source
is touched at all. The short-circuit only reaches reruns -- on a first apply the
destination does not exist and every source guard runs.
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import stat
from pathlib import Path

from .spec_apply import (
    MaterializationPlanError,
    ValidatedPlan,
    _read_canonical_workspace_spec_bytes,
    canonicalize_workspace_path,
)

_STAGING_INPUTS_PARTS = (".grip", "staging", "inputs")


class ProjectFileExecutionError(MaterializationPlanError):
    """A validated plan's project_file operation could not be executed safely."""


@dataclasses.dataclass(frozen=True)
class _ProjectFileBinding:
    """One immutable reading of the operation, taken from A's frozen snapshot
    before any caller-reachable code runs. Same rationale as S4-B's
    _CloneBinding: verification and use must not be separated by anything that
    can run caller code."""

    source_path: str
    dest_path: str
    source_sha256: str


def staging_inputs_root(workspace_root: Path) -> Path:
    return workspace_root.joinpath(*_STAGING_INPUTS_PARTS)


def _require_workspace_binding(validated: ValidatedPlan, workspace_root: Path) -> None:
    """Re-prove at USE what validation proved earlier. The plan is bound to a
    WorkspaceSpec at validation while the executor takes workspace_root
    separately, so nothing structurally stops executing a plan against a
    workspace it was never validated for -- every declared relative path would
    resolve elsewhere.

    Duplicated from clone_exec rather than imported: that one raises
    CloneExecutionError, and a project_file failure reporting a clone error is
    worse than twelve duplicated lines. The shared executor scaffolding wants
    extracting into one module; deferred so it does not land inside a slice PR."""
    try:
        spec_bytes = _read_canonical_workspace_spec_bytes(workspace_root)
    except MaterializationPlanError as exc:
        raise ProjectFileExecutionError(
            f"cannot execute against {workspace_root}: its canonical WorkspaceSpec is "
            f"unreadable ({exc})"
        ) from exc
    actual = hashlib.sha256(spec_bytes).hexdigest()
    if actual != validated.workspace_spec_sha256:
        raise ProjectFileExecutionError(
            f"plan is bound to WorkspaceSpec {validated.workspace_spec_sha256} but "
            f"{workspace_root} has {actual} -- executing a plan against a workspace it "
            "was not validated for would resolve every declared path elsewhere"
        )


def verify_staged_source(
    source: Path, *, workspace_root: Path, source_sha256: str
) -> bytes:
    """Prove the staged input, and RETURN the bytes that were proven.

    Returning the buffer is the whole point. A verifier that answers True and
    leaves the caller to reopen the file has verified one read and authorised a
    different one; the bytes that were hashed must be the bytes that get
    written.

    Two guards this slice's design ASSUMED were its own turned out to be owned
    upstream, and both are deliberately absent rather than duplicated here,
    because an unreachable guard reads as protection while being untestable at
    its own level (S4-A's conclusion):

      - Section 383 staging confinement is enforced SYNTACTICALLY by the pinned
        v1 schema at validation. The spec says "syntactically confined", and A
        wired it, so a source_path outside .grip/staging/inputs never produces a
        capability at all.
      - The post-validation symlink is caught by A's canonicalize_workspace_path
        at EXECUTION, because the executor calls it and it lstat-walks every
        component including the last.

    Both facts are pinned by premise tests, so a loosened schema or canonicalizer
    surfaces there rather than silently opening a hole here.

    What remains genuinely this guard's own is the input neither upstream check
    sees: a path that exists, is not a symlink, and is not a regular file -- a
    FIFO, socket, or directory sitting where the staged foundation should be."""
    try:
        mode = os.lstat(source).st_mode
    except FileNotFoundError:
        raise ProjectFileExecutionError(
            f"staged input {source} does not exist -- the staged input is written "
            "foundation before gr2 projects it"
        ) from None
    if not stat.S_ISREG(mode):
        kind = "a symlink" if stat.S_ISLNK(mode) else "not a regular file"
        raise ProjectFileExecutionError(
            f"staged input {source} must be a regular file, but it is {kind} -- a "
            "path validated as symlink-free at validation time is unbound at "
            "execution time unless the executor re-proves it"
        )

    data = source.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != source_sha256:
        raise ProjectFileExecutionError(
            f"staged input {source} hashes to {actual}, not the declared "
            f"source_sha256 {source_sha256}"
        )
    return data


def _publish_atomically(dest: Path, data: bytes) -> None:
    """Same-directory temp -> write -> fsync -> atomic replace -> parent fsync.

    A half-written AGENTS.md is worse than none: Codex reads it on startup, and
    a truncated foundation is a silently wrong one rather than a loud failure."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{dest.name}.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    dir_fd = os.open(dest.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _destination_is_current(dest: Path, source_sha256: str) -> bool:
    """A destination hashing to the declared value IS proof the projection is
    current -- the hash is the contract, and satisfying it needs no source read.

    This is what makes the cleanup slice safe to exist: once cleanup removes the
    staged input, currency can still be established from the destination alone.
    Without it, C and the cleanup slice would each be correct and together break
    the system's own second apply."""
    try:
        return hashlib.sha256(dest.read_bytes()).hexdigest() == source_sha256
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return False


def execute_project_file_operation(
    validated: ValidatedPlan, index: int, *, workspace_root: Path
) -> dict[str, object]:
    """Execute the `project_file` operation at `index` of a validated plan.

    Addressed by index rather than filtered by kind so evidence[i] stays aligned
    with operation i, which S4-A's receipt screen requires."""
    # Plain Path before anything else: a caller-supplied Path SUBCLASS can run
    # arbitrary code from its dunders during path work, which is the callback
    # that reopens the check/use window. Normalising before verification removes
    # the surface rather than trying to be safe around it (S4-B).
    workspace_root = Path(os.fspath(workspace_root))

    validated.verify(require_provenance=True)
    _require_workspace_binding(validated, workspace_root)

    operations = validated.plan["operations"]
    if not 0 <= index < len(operations):
        raise ProjectFileExecutionError(
            f"operation index {index} is out of range for a plan with "
            f"{len(operations)} operation(s)"
        )
    op = operations[index]
    kind = op.get("kind")
    if kind != "project_file":
        raise ProjectFileExecutionError(
            f"operations[{index}] is kind {kind!r}, not 'project_file' -- this executor "
            "applies project_file operations only; each other kind has its own "
            "executor"
        )

    binding = _ProjectFileBinding(
        source_path=str(op["source_path"]),
        dest_path=str(op["dest_path"]),
        source_sha256=str(op["source_sha256"]),
    )

    source = canonicalize_workspace_path(
        workspace_root, binding.source_path, field_name=f"operations[{index}].source_path"
    )
    dest = canonicalize_workspace_path(
        workspace_root, binding.dest_path, field_name=f"operations[{index}].dest_path"
    )

    if _destination_is_current(dest, binding.source_sha256):
        written = False
    else:
        data = verify_staged_source(
            source, workspace_root=workspace_root, source_sha256=binding.source_sha256
        )
        _publish_atomically(dest, data)
        written = True

    # Nothing is deleted here. Invariant 8 forbids removing a staged input
    # before the receipt carrying this evidence is durable, and this executor
    # cannot know when that happens -- so the staged input survives, and the
    # cleanup slice keyed off the published receipt is the only thing entitled
    # to remove it.
    return {
        "kind": "project_file",
        "source_path": binding.source_path,
        "dest_path": binding.dest_path,
        "source_sha256": binding.source_sha256,
        "mode": "copy",
        "written": written,
    }
