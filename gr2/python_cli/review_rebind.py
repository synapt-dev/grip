"""`gr2 review rebind` — rebase a frozen public-push range onto a moved integration
base when the moving commit is unrelated, and refuse the moment the range would
change.

A freeze's ``behind 0`` is a moment property: the integration branch moves under a
frozen range constantly, and today every such death is a hand re-freeze plus two
fresh reads. ``rebind`` does the mechanical rebase safely — patch-id identity plus a
rename-aware net-diff equality — and refuses (never silently rebinds) the moment the
range would actually change.

Contract (tests/test_review_rebind.py):
  * base unchanged        -> RebindResult(outcome="base_unchanged"), no new dir
  * unrelated base move   -> RebindResult(outcome="rebased", patch_id_held=True), new frozen dir
  * range would change    -> raise RebindRefused (real conflict or patch-id divergence)

Landed: the working core (freeze the artifacts rebind consumes; the base-unchanged
short-circuit; clone + am + patch-id identity + re-freeze), the CLI verb,
already-applied detection, intended-ref-public refusal, and rename-aware
diff-equality (defense-in-depth behind patch-id; its refuse path has no
discriminating unit fixture yet, as in the shell prototype). Remaining: TDD-spec
completion and the two-read gate before the branch pushes to grip dev.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .clone_exec import IncompleteRemoval, rmtree_or_refuse

_TARGET_RE = re.compile(r"^target:[^@]*@ ([0-9a-f]{40})", re.MULTILINE)
_INTENDED_RE = re.compile(r"^repo:.*intended ref: (\S+)", re.MULTILINE)


class RebindRefused(Exception):
    """The frozen range would change on the moved base (conflict or patch-id
    divergence), or the intended ref is already public: a hand re-freeze and a
    fresh read are required, not a mechanical rebind."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass
class RebindResult:
    outcome: str  # base_unchanged | rebased | already_applied
    patch_id_held: bool | None = None
    landing_sha: str | None = None
    out_dir: Path | None = None


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True, check=check
    )


def _patch_ids(patch_text: str) -> str:
    """Sorted set of stable patch-ids for a format-patch stream (rename-blind; the
    rename-aware _norm_diff equality below is the complement)."""
    p = subprocess.run(
        ["git", "patch-id", "--stable"], input=patch_text, text=True, capture_output=True
    )
    ids = sorted(line.split()[0] for line in p.stdout.splitlines() if line.strip())
    return "\n".join(ids)


_HUNK_RE = re.compile(r"^@@ -[0-9]+(,[0-9]+)? \+[0-9]+(,[0-9]+)? @@")


def _norm_diff(diff_text: str) -> str:
    """Normalize a diff for content-equality across two different bases: drop the
    `index <blob>..<blob>` lines (blob shas differ for files an unrelated move also
    touched, which the am/patch-id checks already catch) and erase hunk offsets
    (@@ -25 vs @@ -28) so only content, not position, is compared."""
    out = []
    for line in diff_text.splitlines():
        if line.startswith("index "):
            continue
        out.append(_HUNK_RE.sub("@@ @@", line))
    return "\n".join(out)


def freeze(repo: Path, target_ref: str, out_dir: Path, *, title: str, body: str) -> Path:
    """Write the artifacts rebind consumes: range.patch (target..HEAD), a fuller
    metadata log, the platform texts, and a REQUEST.md carrying the target sha,
    intended ref, and head. A focused freeze for the rebind lane; the production
    public-push freeze is the richer sibling."""
    repo = Path(repo)
    out_dir = Path(out_dir)
    if out_dir.exists():
        raise RebindRefused("out_dir_exists", f"a new version is a new dir: {out_dir}")
    out_dir.mkdir(parents=True)
    base = _git(repo, "rev-parse", target_ref).stdout.strip()
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    url = _git(repo, "remote", "get-url", "origin").stdout.strip()
    intended_ref = f"refs/heads/{branch}"
    (out_dir / "range.patch").write_text(
        _git(repo, "format-patch", f"{base}..HEAD", "--stdout").stdout
    )
    (out_dir / "metadata.fuller.txt").write_text(
        _git(repo, "log", "--format=fuller", f"{base}..HEAD").stdout
    )
    (out_dir / "pr-title.txt").write_text(title)
    (out_dir / "pr-body.md").write_text(body)
    (out_dir / "REQUEST.md").write_text(
        f"repo: {repo.name}   branch: {branch}   intended ref: {intended_ref}\n"
        f"intended ref on remote at freeze: {_ref_state(repo, url, intended_ref)}\n"
        f"target: {target_ref} @ {base}\n"
        f"head: {head}\n"
    )
    return out_dir


def _frozen_base(frozen_dir: Path) -> str:
    req = (frozen_dir / "REQUEST.md").read_text()
    m = _TARGET_RE.search(req)
    if not m:
        raise RebindRefused("unparseable_request", f"no target sha in {frozen_dir}/REQUEST.md")
    return m.group(1)


def _frozen_intended_ref(frozen_dir: Path) -> str | None:
    m = _INTENDED_RE.search((frozen_dir / "REQUEST.md").read_text())
    return m.group(1) if m else None


def _branch_of(ref: str) -> str:
    """The branch name from a ref, keeping every segment after the known prefix.
    `rsplit('/', 1)[-1]` dropped all but the last segment, so a slash-bearing
    branch (release/1.x, feat/x) was ls-remoted as the wrong ref."""
    for pfx in ("refs/remotes/origin/", "refs/heads/", "origin/"):
        if ref.startswith(pfx):
            return ref[len(pfx):]
    return ref


def _ref_state(repo: Path, url: str, ref: str | None) -> str:
    """Measured presence of ``ref`` on ``url`` for the emitted REQUEST.md, rather
    than an unconditional 'ABSENT' (false under --allow-public-ref)."""
    if not ref:
        return "unknown (no intended ref recorded)"
    ls = _git(repo, "ls-remote", url, ref).stdout.split()
    return f"PRESENT at {ls[0][:12]}" if ls else "ABSENT on remote"


def rebind(frozen_dir: Path, repo: Path, target_ref: str, out_dir: Path,
           *, allow_public_ref: bool = False) -> RebindResult:
    """Rebase the frozen range onto the live tip of ``target_ref`` and emit a new
    frozen dir when the range is unchanged (patch-ids identical). Refuse when it
    would change (conflict or patch-id divergence) or when the intended ref is
    already public (a force-push question, not a rebase) unless ``allow_public_ref``.

    The live base is read from ``ls-remote`` of ``repo``'s origin; when that origin
    is a local cache mirror (as for a review clone), 'live' is the mirror's last
    fetch, not GitHub at this instant. Fetch the mirror first, or point ``repo`` at
    a clone whose origin is the GitHub URL, when instant-live matters."""
    frozen_dir = Path(frozen_dir)
    repo = Path(repo)
    out_dir = Path(out_dir)
    if out_dir.exists():
        raise RebindRefused("out_dir_exists", f"a new version is a new dir: {out_dir}")
    frozen_base = _frozen_base(frozen_dir)
    branch = _branch_of(target_ref)
    url = _git(repo, "remote", "get-url", "origin").stdout.strip()

    # intended-ref-public: a rebind mints a new head, so if the ref is already on the
    # remote this is a force-push, not the pre-push rebase rebind is for. Mirrors the
    # freeze script's ref-absent assertion (exit 6); --allow-public-ref is the
    # sanctioned fix-forward on a branch already ratified and pushed.
    intended = _frozen_intended_ref(frozen_dir)
    if intended and not allow_public_ref:
        present = _git(repo, "ls-remote", url, intended).stdout.split()
        if present:
            raise RebindRefused(
                "intended_ref_public",
                f"{intended} is already on {url} at {present[0][:12]}; a rebind would "
                "force-push it — pass allow_public_ref for a ratified fix-forward",
            )

    ls = _git(repo, "ls-remote", url, f"refs/heads/{branch}").stdout.split()
    live_base = ls[0] if ls else None
    if not live_base:
        raise RebindRefused("no_live_base", f"cannot ls-remote refs/heads/{branch} from {url}")

    if frozen_base == live_base:
        return RebindResult(outcome="base_unchanged")

    clone = Path(tempfile.mkdtemp(prefix="rebind-clone."))
    try:
        subprocess.run(["git", "clone", "--quiet", url, str(clone)],
                       capture_output=True, text=True, check=True)
        _git(clone, "checkout", "--quiet", live_base)
        # `git am` writes a commit, which needs a committer identity. A fresh clone
        # inherits none, and a host with no global git config falls back to git's
        # auto-detected user@hostname — which on a CI runner whose hostname has no
        # domain is rejected (email ends in "(none)"; git refuses to invent one),
        # so `am` fails and the catch-all below would mislabel it "conflict". A
        # developer host auto-detects a usable address and never hits this. Set the
        # generic identity explicitly so the rebase never depends on the host's.
        _git(clone, "config", "user.name", "Layne Penney")
        _git(clone, "config", "user.email", "dev@layne.pro")
        # The frozen range was authored in `repo` and may never have been pushed to
        # origin (a pre-push freeze is the whole point), so a fresh clone of origin
        # lacks the range's blobs. Bring them in so `am --3way` has real objects for
        # its three-way fallback when the base and the range touch nearby content.
        _git(clone, "fetch", "--quiet", str(repo),
             "+refs/heads/*:refs/rebind-src/*", check=False)
        # already-applied: the base moved forward by landing this very work (someone
        # merged the range). Its patch-ids are then a subset of the commits added
        # since the frozen base, so the rebased range would be EMPTY — which the
        # patch-id check below reads as a divergence. Detect it first and name the
        # landing tip, rather than refusing a range that is simply already there.
        anc = _git(clone, "merge-base", "--is-ancestor", frozen_base, live_base, check=False)
        if anc.returncode == 0:
            frozen_ids = {
                x for x in _patch_ids((frozen_dir / "range.patch").read_text()).splitlines() if x
            }
            moved = _git(clone, "format-patch", f"{frozen_base}..{live_base}", "--stdout").stdout
            moved_ids = {x for x in _patch_ids(moved).splitlines() if x}
            if frozen_ids and frozen_ids <= moved_ids:
                return RebindResult(
                    outcome="already_applied", patch_id_held=True, landing_sha=live_base
                )
        am = _git(clone, "am", "--3way", str(frozen_dir / "range.patch"), check=False)
        if am.returncode != 0:
            # Carry `am`'s own stderr into the refusal. A non-zero `am` is not always
            # a content conflict (an unusable committer identity, a missing blob, a
            # corrupt patch all land here); swallowing its stderr would relabel every
            # one of them "conflict" and hide the real cause from every reader.
            detail = (am.stderr or am.stdout or "").strip()
            _git(clone, "am", "--abort", check=False)
            raise RebindRefused(
                "conflict",
                f"the frozen range does not apply on the moved base {live_base[:12]}; "
                "a hand re-freeze and a fresh read are required"
                + (f" — git am said: {detail}" if detail else ""),
            )
        new_range = _git(clone, "format-patch", f"{live_base}..HEAD", "--stdout").stdout
        # Defense-in-depth behind the conflict path above: a would-change range is
        # normally caught as an `am --3way` conflict, so this refusal has no known
        # reachable case (a clean 3-way apply that still yields different patch-ids).
        # Kept because that guarantee is git's, not ours; no discriminating fixture.
        if _patch_ids((frozen_dir / "range.patch").read_text()) != _patch_ids(new_range):
            raise RebindRefused(
                "patch_id_diverged",
                "patch-ids differ after rebasing onto the moved base — the range CHANGED",
            )
        new_head = _git(clone, "rev-parse", "HEAD").stdout.strip()

        # rename-aware diff-equality: patch-id is rename-blind, so reconstruct the
        # frozen range on its OWN base and compare its rename-aware net diff to the
        # rebased range's. Refuse a divergence (defense-in-depth behind patch-id).
        # If the frozen base is unreconstructable in the clone, patch-id identity
        # above is authoritative. Port of the shell prototype's ENFORCED check.
        d_new = _norm_diff(
            _git(clone, "diff", "--find-renames", f"{live_base}..{new_head}").stdout
        )
        anc2 = _git(clone, "merge-base", "--is-ancestor", frozen_base, new_head, check=False)
        if anc2.returncode == 0:
            scratch = f"_rebind_frozen_{os.getpid()}"
            _git(clone, "checkout", "--quiet", "-b", scratch, frozen_base, check=False)
            am2 = _git(clone, "am", "--3way", str(frozen_dir / "range.patch"), check=False)
            if am2.returncode == 0:
                d_old = _norm_diff(
                    _git(clone, "diff", "--find-renames", f"{frozen_base}..HEAD").stdout
                )
                _git(clone, "checkout", "--quiet", new_head, check=False)
                _git(clone, "branch", "-D", scratch, check=False)
                if d_old != d_new:
                    raise RebindRefused(
                        "diff_diverged",
                        "the range's rename-aware net content diff differs between the "
                        "frozen base and the moved base — the range CHANGED",
                    )
            else:
                _git(clone, "am", "--abort", check=False)
                _git(clone, "checkout", "--quiet", new_head, check=False)
                _git(clone, "branch", "-D", scratch, check=False)
        # else: frozen base not reconstructable; patch-id identity is authoritative.

        # re-freeze: same title/body, new range/metadata against the moved base
        out_dir.mkdir(parents=True)
        (out_dir / "range.patch").write_text(new_range)
        (out_dir / "metadata.fuller.txt").write_text(
            _git(clone, "log", "--format=fuller", f"{live_base}..HEAD").stdout
        )
        shutil.copyfile(frozen_dir / "pr-title.txt", out_dir / "pr-title.txt")
        shutil.copyfile(frozen_dir / "pr-body.md", out_dir / "pr-body.md")
        # carry the ORIGINAL intended ref (the feature branch being pushed), not the
        # target branch, and print its MEASURED remote state (false-'ABSENT' under
        # --allow-public-ref otherwise).
        intended_ref = intended or f"refs/heads/{_branch_of(target_ref)}"
        (out_dir / "REQUEST.md").write_text(
            f"repo: {repo.name}   branch: {_branch_of(intended_ref)}   intended ref: {intended_ref}\n"
            f"intended ref on remote at freeze: {_ref_state(repo, url, intended_ref)}\n"
            f"target: {target_ref} @ {live_base}\n"
            f"head: {new_head}\n"
        )
        return RebindResult(outcome="rebased", patch_id_held=True, out_dir=out_dir)
    finally:
        # Best-effort cleanup of the temp clone; do not let an incomplete removal
        # mask the primary outcome (routed through the shared helper per the
        # rmtree class-closure invariant).
        try:
            rmtree_or_refuse(clone)
        except IncompleteRemoval:
            pass
