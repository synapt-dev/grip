"""Review-ephemeral lane materialization: blobless + sparse, from the persistent
per-host review mirror, NEVER through ``materialize_lane_clone``.

A review lane is read-only and ephemeral -- nobody commits into it and it is
``rm -rf``'d when the review closes. Sections 8.1 (complete history) and 8.2 (no
external object sharing) that ``materialize_lane_clone`` enforces exist to protect
a MUTABLE work lane's independent state; a review lane does not need them, and a
blobless clone keeps the whole commit+tree graph, so patch-id, diff, and
merge-base all work (measured: identical base..head patch-id vs a full clone).
Blobless is the only ``.git`` reducer (shallow is out -- a base..head review must
reach an arbitrary base); sparse is the working-tree reducer, using EXACTLY
``review-clone.sh``'s two rules so the two tools cannot disagree:

  1. the profile is ignore-syntax and comes from, in order: the in-repo
     ``.review-exclude`` at the reviewed ref (read from the bare mirror), else the
     gripspace fallback ``<profile_dir>/<repo>.exclude`` (``SYNAPT_REVIEW_PROFILE_DIR``,
     the same env ``review-clone.sh`` honors), else none (whole tree, still blobless);
  2. every path ``base..head`` touches is appended AFTER the profile as an explicit
     ``/path`` include, so a review can never be missing a file it is about (later
     patterns win in ``--no-cone`` sparse-checkout).

This lane KIND never enters the work-lane clone seam, so it cannot weaken 8.1/8.2.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REVIEW_EPHEMERAL_KIND = "review-ephemeral"


class ReviewEphemeralError(Exception):
    pass


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=False)


def review_profile_dir() -> Path | None:
    """The gripspace-level sparse-profile fallback dir, honoring the same
    ``SYNAPT_REVIEW_PROFILE_DIR`` override review-clone.sh honors. gr2 is
    repo-layout-agnostic, so with the env unset there is NO gripspace fallback and
    only an in-repo ``.review-exclude`` applies (which is versioned with the repo
    and always found)."""
    value = os.environ.get("SYNAPT_REVIEW_PROFILE_DIR")
    return Path(value) if value else None


def resolve_sparse_profile(mirror: Path, ref: str, repo_name: str) -> tuple[str, str]:
    """Resolve the sparse profile for ``ref`` by review-clone.sh's precedence.

    Returns ``(profile_content, source_label)``; empty content means no profile
    (whole tree, still blobless)."""
    shown = _git(mirror, "show", f"{ref}:.review-exclude")
    if shown.returncode == 0 and shown.stdout.strip():
        return shown.stdout, f"in-repo .review-exclude @ {ref}"
    pdir = review_profile_dir()
    if pdir is not None:
        fallback = pdir / f"{repo_name}.exclude"
        if fallback.is_file():
            return fallback.read_text(), f"fallback {fallback}"
    return "", "none"


def _pattern_lines(profile_content: str, touched_paths: list[str]) -> list[str]:
    """Profile lines (comments/blanks stripped) first, then each touched path as an
    explicit ``/path`` include -- review-clone.sh's exact ordering."""
    lines: list[str] = []
    for raw in profile_content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    for p in touched_paths:
        p = p.strip()
        if p:
            lines.append("/" + p.lstrip("/"))
    return lines


def touched_paths(mirror: Path, base: str, head: str) -> list[str]:
    """The paths ``base..head`` touches (the range-union source). Read from the
    bare mirror, which holds the complete commit+tree graph."""
    proc = _git(mirror, "diff", "--name-only", base, head)
    if proc.returncode != 0:
        raise ReviewEphemeralError(
            f"cannot compute base..head touched paths ({base[:8]}..{head[:8]}) in {mirror}: "
            f"{proc.stderr.strip()}"
        )
    return [p for p in proc.stdout.splitlines() if p.strip()]


def materialize_review_ephemeral(
    *,
    mirror: Path,
    dest: Path,
    head: str,
    base: str,
    repo_name: str,
    echo=lambda _l: None,
) -> dict[str, str]:
    """Materialize a review lane as a BLOBLESS + SPARSE clone from the mirror.

    Returns provenance: the profile source, the pattern count, and the resulting
    HEAD. Refuses if the checked-out HEAD is not the requested head."""
    mirror = Path(mirror)
    dest = Path(dest)
    if dest.exists():
        raise ReviewEphemeralError(f"review-ephemeral destination already exists: {dest}")

    profile_content, profile_source = resolve_sparse_profile(mirror, head, repo_name)
    patterns = _pattern_lines(profile_content, touched_paths(mirror, base, head))

    dest.parent.mkdir(parents=True, exist_ok=True)
    cloned = subprocess.run(
        ["git", "clone", "--quiet", "--no-checkout", "--filter=blob:none",
         f"file://{mirror}", str(dest)],
        text=True, capture_output=True, check=False,
    )
    if cloned.returncode != 0:
        raise ReviewEphemeralError(f"blobless clone from {mirror} failed: {cloned.stderr.strip()}")

    # Sparse only when there is a profile AND patterns; a bare range-union with no
    # profile would narrow to just the changed files, hiding their context, so a
    # profile-less repo stays whole-tree (blobless), matching review-clone.sh.
    if profile_content.strip() and patterns:
        applied = subprocess.run(
            ["git", "-C", str(dest), "sparse-checkout", "set", "--no-cone", "--stdin"],
            input="\n".join(patterns) + "\n", text=True, capture_output=True, check=False,
        )
        if applied.returncode != 0:
            raise ReviewEphemeralError(f"sparse-checkout set failed in {dest}: {applied.stderr.strip()}")

    checked = _git(dest, "checkout", "--quiet", head)
    if checked.returncode != 0:
        raise ReviewEphemeralError(f"cannot check out review head {head} in {dest}: {checked.stderr.strip()}")
    got = _git(dest, "rev-parse", "HEAD").stdout.strip()
    if got != head:
        raise ReviewEphemeralError(f"review-ephemeral lane is at {got}, not the requested head {head}")

    return {"profile_source": profile_source, "patterns": str(len(patterns)), "head": got}
