"""Project-tier open-gr --enter: one review-kind gr commit opens one exact
multi-repository review, and exit returns the agent to its prior work.

Composes the repo-tier review open (``project_review.open_project_review``, which
materializes each pinned head and enters the review lane) with a project-tier
receipt naming the gr commit and the per-repo base/head, plus an exit that
restores the prior lane AND the prior cwd. The review's per-repo ``base`` is the
recorded fork base (it rode in with the review-kind pins), so one gr commit opens
the exact review the author bound.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from gr2.prototypes import lane_workspace_prototype as lane_proto

from . import grip, project_review, review
from .clone_exec import IncompleteRemoval, rmtree_or_refuse
from .gitops import git
from .review_run import _OUTPUT_LOG_NAME as _RUN_LOG_NAME
from .review_run import _MARKER_KIND, _MARKER_NAME, _RECEIPT_NAME as _RUN_RECEIPT_NAME
from .review_run import find_marker, marker_kind_ok
from .review_allocation import (
    ReviewAllocationError, allocation_path, require_allocation, save_allocation,
)


class OpenGrReviewError(Exception):
    pass


_RECEIPT_NAME = ".grip-open-gr.json"

# open-gr's teardown marker. Distinct from _RECEIPT_NAME (the open-PROJECT receipt
# exit-gr reads): open-gr pushes no lane and changes no cwd, so its teardown is only
# the rm of this disposable tree, and it must NOT be mistaken for an exit-gr receipt.
_OPEN_GR_MARKER = _MARKER_NAME


def write_open_gr_marker(lane_dir: Path, gr_commit: str, results: dict, workspace_root: Path | None = None) -> None:
    """Record a small teardown marker at the open-gr lane root so ``close_open_gr_lane``
    can verify the directory is an open-gr reconstruction before removing it, rather
    than rm an arbitrary path. ``results`` maps repo key -> the reconstruct dict."""
    marker = {
        "kind": _MARKER_KIND,
        "gr_commit": gr_commit,
        # where `review run` and `review close` emit their events; absent on older lanes
        "workspace_root": str(workspace_root) if workspace_root is not None else None,
        "repos": [
            {
                "key": key,
                "reconstructed_head": res.get("reconstructed_head", ""),
                "bound_head": res.get("bound_head", ""),
                "bound_head_tree": res.get("bound_head_tree", ""),
                "reconstructed_tree": res.get("reconstructed_tree", ""),
                "tree_match": res.get("bound_head_tree") == res.get("reconstructed_tree"),
            }
            for key, res in results.items()
        ],
    }
    (Path(lane_dir) / _OPEN_GR_MARKER).write_text(json.dumps(marker, indent=2) + "\n")


def close_open_gr_lane(lane_dir: Path, *, workspace_root: Path | None = None) -> dict:
    """Reclaim an ``open-gr --enter`` reconstruction lane: verify its marker, then rm
    the disposable tree. Refuses a directory with no open-gr marker (so a wrong
    ``--lane-dir`` can never remove an arbitrary path). No OWNER_UNIT, no lane pop,
    no cwd restore -- open-gr did none of those, so teardown undoes only the clone."""
    lane_dir = Path(lane_dir)
    marker_path = find_marker(lane_dir)
    marker_bytes = marker_path.read_bytes() if marker_path is not None else None
    marker = json.loads(marker_bytes) if marker_bytes is not None else None
    recorded_workspace = workspace_root or (marker or {}).get("workspace_root")
    if not recorded_workspace:
        raise OpenGrReviewError("review has no owning workspace allocation; save evidence and reopen an allocated lane")
    workspace = Path(recorded_workspace).resolve()
    try:
        allocation = require_allocation(workspace, lane_dir, owner_unit="workspace")
        if not allocation["disposable"]:
            raise ReviewAllocationError("allocation is not disposable")
    except ReviewAllocationError as exc:
        raise OpenGrReviewError(str(exc)) from exc
    retained = allocation.get("close_marker_hex")
    if marker_bytes is None and allocation["state"] == "closing" and retained is not None:
        try:
            marker_bytes = bytes.fromhex(retained)
            marker = json.loads(marker_bytes)
        except (ValueError, TypeError) as exc:
            raise OpenGrReviewError("invalid retained close marker; operator recovery required") from exc
    if marker is None or not marker_kind_ok(marker):
        raise OpenGrReviewError("no valid reconstruction marker; retaining allocation")
    if retained is not None and marker_bytes.hex() != retained:
        raise OpenGrReviewError("reconstruction marker changed during cleanup; retaining recovery")
    gr_commit = marker.get("gr_commit", "")
    if allocation["state"] != "closing":
        _refuse_local_work(lane_dir, allocation)
    if allocation["state"] == "closing":
        preserved = allocation.get("preserved_runs", [])
    else:
        preserved = _preserve_allocated_runs(workspace, lane_dir, allocation)
        allocation.update(state="closing", close_marker_hex=marker_bytes.hex(), preserved_runs=preserved)
        save_allocation(workspace, lane_dir, allocation)
    _remove_allocated(workspace, lane_dir, allocation)
    result = {"reclaimed": str(lane_dir), "gr_commit": gr_commit}
    if preserved:
        # Preserve the existing single-run response while exposing all members.
        result["preserved_run"] = preserved[0]
        result["preserved_runs"] = preserved
    return result


def _refuse_local_work(target: Path, allocation: dict) -> None:
    """Close removes the lane, so it first refuses when the lane holds work that exists nowhere
    else: in a member repository, a staged, unstaged or untracked change (the review's own
    marker, run receipt and logs excepted), a stash, or a commit no remote has and the reviewed
    HEAD does not contain; and, at the root of a multi-member lane, any entry that is not a member,
    the shared environment, or the review's own files. A git call that cannot answer refuses too.
    Files git ignores are not examined. The lane, its allocation and its run evidence stay."""
    from .review_run import _is_allowlisted_untracked

    def ask(path: Path, *args: str) -> str:
        proc = git(path, *args)
        if proc.returncode != 0:
            raise OpenGrReviewError(f"lane {target}: could not read {path} (git {args[0]}): "
                                    f"{(proc.stderr or proc.stdout).strip()}; close refused")
        return proc.stdout

    from .review_runners import RUNNER_CREATED_PATHS
    # A runner's own outputs (cargo's target/ and Cargo.lock, ...) are the review's, exactly as
    # `review run` treats them; the receipt names the runner that wrote them.
    try:
        runner = json.loads((target / _RUN_RECEIPT_NAME).read_text()).get("runner")
    except (OSError, ValueError, AttributeError):
        runner = None
    created = RUNNER_CREATED_PATHS.get(runner or "", {})
    found: list[str] = []
    members = [target / item["path"] for item in allocation["members"]]
    if not (target / ".git").exists():
        from .review_run import _LEGACY_MARKERS
        member_names = {m.relative_to(target).parts[0] for m in members if m != target}
        # The exact names the review writes at the root: its marker, receipt and run logs (one per
        # member, in either spelling) and the shared environment. Nothing else is the review's.
        own = {".venv", _MARKER_NAME, *_LEGACY_MARKERS, _RUN_RECEIPT_NAME, _RUN_LOG_NAME,
               *(f"{key}{_RUN_LOG_NAME}" for key in member_names),
               *(f"{_RUN_LOG_NAME}.{key}" for key in member_names)}
        for entry in sorted(target.iterdir()):
            name = entry.name
            if name in member_names or (name == ".venv" and entry.is_dir()) \
                    or (name in own and entry.is_file() and not entry.is_symlink()):
                continue
            found.append(f"work at the lane root: {entry}")
    for path in dict.fromkeys([target, *members]):
        if not (path / ".git").exists():
            continue
        for line in ask(path, "status", "--porcelain", "--untracked-files=all").splitlines():
            if line.startswith("?? ") and _is_allowlisted_untracked(
                    line[3:], extra_names=created.get("names", frozenset()),
                    extra_tops=created.get("tops", ()), extra_segments=created.get("segments", frozenset())):
                continue
            found.append(f"uncommitted work in {path}: {line}")
        stashes = ask(path, "stash", "list", "--format=%H").split()
        if stashes:
            found.append(f"uncommitted work in {path}: a stash")
        # Every ref and every reflog entry (a commit left only in the reflog is still work), less
        # what a remote has, the reviewed HEAD, and the stashes reported above.
        unpublished = ask(path, "rev-list", "--exclude=refs/stash", "--all", "--reflog",
                          "--not", "--remotes", "HEAD", *stashes).split()
        if unpublished:
            found.append(f"unpublished commits in {path}: {', '.join(c[:12] for c in unpublished)}")
    if found:
        raise OpenGrReviewError(
            f"lane {target} holds work that exists nowhere else; commit and push it, or move it out, "
            "then close again:\n  " + "\n  ".join(found))


def _preserve_allocated_runs(workspace: Path, target: Path, allocation: dict) -> list[dict]:
    archive = allocation_path(workspace, target).with_suffix("")
    paths = list(dict.fromkeys([target, *(target / item["path"] for item in allocation["members"])]))
    result = []
    for path in paths:
        relative = str(path.relative_to(target))
        key = hashlib.sha256(relative.encode()).hexdigest()
        item = _preserve_run_artifacts(path, archive_root=archive / key)
        if item:
            result.append({"member_path": relative, **item})
    return result


def _remove_allocated(workspace: Path, target: Path, allocation: dict) -> None:
    try:
        # Revalidate physical target and every member immediately before delegation.
        require_allocation(workspace, target, owner_unit=allocation["owner_unit"])
        if target.exists():
            rmtree_or_refuse(target)
        if target.exists() or target.is_symlink():
            raise OpenGrReviewError("allocated target remains after cleanup")
        allocation_path(workspace, target).unlink()
    except (IncompleteRemoval, OSError, ReviewAllocationError) as exc:
        raise OpenGrReviewError(f"review cleanup incomplete: {exc}; recovery at {allocation_path(workspace, target)}") from exc


def _preserve_run_artifacts(lane_dir: Path, *, archive_root: Path | None = None) -> dict | None:
    """Copy a `review run` receipt (and its output log) out of the lane into a sibling
    dir that the lane's rmtree cannot reach, BEFORE the rmtree. Returns
    ``{"dir", "receipt", "log"?}`` of the preserved paths, or None when the lane holds
    no run receipt (the run was never done). The preserved receipt's ``output_log`` is
    rewritten to the preserved log path, so the surviving receipt still names its log."""
    receipt_src = lane_dir / _RUN_RECEIPT_NAME
    if not receipt_src.is_file():
        return None
    # Sibling of the lane (NOT under it), `.resolve()` so it is a true sibling even when
    # lane_dir was passed relative.
    lane_dir = lane_dir.resolve()
    receipt = json.loads(receipt_src.read_text())
    # Key the preserved dir by (gr short commit, run timestamp), both taken from the
    # receipt the run wrote. Keying only by lane name let a SECOND close of the same
    # lane name overwrite the first close's receipt and log in place, so door 1's
    # "the evidence survives close" held for one close per lane name only. A distinct
    # run (a reopen + rerun) has a distinct `created`, so it lands beside the first
    # rather than on top of it; the same run closed twice is idempotent.
    gr_short = (receipt.get("gr_commit") or "unknown")[:12]
    created = receipt.get("created") or "no-timestamp"
    key = re.sub(r"[^A-Za-z0-9._-]", "-", f"{gr_short}-{created}")
    dest = (archive_root or lane_dir.parent / f"{lane_dir.name}.review-run") / key
    dest.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {"dir": str(dest)}
    # Keep byte-exact original evidence in addition to the usable rewritten view.
    original_dest = dest / (_RUN_RECEIPT_NAME + ".original")
    original_dest.write_bytes(receipt_src.read_bytes())
    out["original_receipt"] = str(original_dest)

    log_src = lane_dir / _RUN_LOG_NAME
    log_dest = dest / _RUN_LOG_NAME
    if log_src.is_file():
        shutil.copy2(log_src, log_dest)
        out["log"] = str(log_dest)

    # Point the surviving receipt at the surviving log (absolute), since the lane-
    # relative name it carried will not exist after the rmtree.
    if "log" in out:
        receipt["output_log"] = out["log"]
    # A multi-member lane keeps one log per member, `<lane>/<key>.grip-review-run.log`, named
    # by the receipt (each member entry, and a refusal's own `output_log`). Carry each out
    # the same way, so the evidence for every member survives the close.
    member_logs: dict[str, str] = {}
    for entry in [receipt, *receipt.get("members", [])]:
        name = entry.get("output_log")
        if not name or name == _RUN_LOG_NAME or name != Path(name).name:
            continue
        src = lane_dir / name
        if not src.is_file():
            continue
        shutil.copy2(src, dest / name)
        entry["output_log"] = str(dest / name)
        member_logs[name] = str(dest / name)
    if member_logs:
        out["member_logs"] = member_logs
    receipt_dest = dest / _RUN_RECEIPT_NAME
    receipt_dest.write_text(json.dumps(receipt, indent=2) + "\n")
    out["receipt"] = str(receipt_dest)
    return out


def _pin_transport_location(pin_repo: str) -> str:
    """The clone LOCATION named by a pin's recorded remote.

    A ``local:`` fixture prefix names a filesystem path; every other identity
    (HTTPS/SSH) is its own clone URL. For a ``local:`` checkout we clone from its
    ORIGIN (not the checkout itself), mirroring ``_canonical_repo_identity`` so
    the resolved source's origin canonicalizes to the SAME identity the pin
    carries -- otherwise ``_validate_workspace_repository_boundary`` refuses the
    resolved source as an identity mismatch. This is transport only; that
    boundary check remains the authority regardless of what we clone from.
    """
    if pin_repo.startswith("local:"):
        local = Path(pin_repo.removeprefix("local:"))
        origin = review.gitops.remote_origin_url(local) if local.is_dir() else None
        return origin or str(local)
    return pin_repo


def review_cache_root() -> Path:
    """The persistent per-host review-mirror cache root, SHARED with
    config/scripts/review-clone.sh so there is one bare mirror per repo on this
    host, never a second keyed differently. Honors the same
    ``SYNAPT_REVIEW_CACHE_ROOT`` override the script honors."""
    import os
    return Path(os.environ.get("SYNAPT_REVIEW_CACHE_ROOT") or (Path.home() / ".synapt-review-cache"))


def _mirror_basename(transport_location: str) -> str:
    """The repo name the shared cache keys a mirror under: the URL/path basename
    minus a trailing ``.git`` (e.g. …/recall.git -> recall). This matches
    review-clone.sh's ``<repo>.git`` for every repo whose review-clone name is its
    URL basename (all the code repos); ``site`` is the one review-clone name that
    is NOT its URL basename, and is out of scope for multi-repo code review."""
    name = transport_location.rstrip("/").rsplit("/", 1)[-1]
    return name[:-4] if name.endswith(".git") else name


def resolve_sources_from_pins(
    pins: list[project_review.ProjectReviewPin] | tuple[project_review.ProjectReviewPin, ...],
    staging_dir: Path | str | None = None,
    *,
    allow_local: bool = False,
    cache_root: Path | str | None = None,
    local_sources: dict[str, Path | str] | None = None,
) -> tuple[dict[str, tuple[Path, str]], dict[str, dict[str, str]]]:
    """Resolve each source from its RECORDED REMOTE through a PERSISTENT bare mirror.

    For each pin, ensure a bare mirror at the SHARED per-host cache
    (``<cache_root>/<repo>.git``, cache_root defaulting to review-clone.sh's
    ``~/.synapt-review-cache``) via ``ensure_repo_cache`` -- seed with
    ``git clone --mirror`` if absent, else ``remote update --prune`` (fetch fresh; a
    stale mirror must never read as current) -- then confirm the pin's base AND head
    are commits IN the mirror. The mirror IS the source handed to the review open:
    the review lane then clones from it blobless (``--filter=blob:none``) and sparse
    (see ``review_ephemeral.materialize_review_ephemeral``), so the lane fetches only
    the blobs and checks out only the paths a review reads instead of copying a full
    clone per review (the measured 978M harm). The mirror's objects are NOT borrowed
    via ``--reference``: the review lane is a self-contained clone, disposable on exit.

    The returned ``review_branch`` is the head sha itself (a sha resolves to itself
    under ``rev-parse --verify <sha>^{commit}``). A mirror that does not carry the
    pinned head is refused HERE, before any review lane opens. Returns
    ``(srcmap, mirror_meta)`` where ``mirror_meta[key]`` names the mirror path and
    its fetched tip so a reviewer can tell a stale mirror from a fresh one.

    ``staging_dir`` is accepted for signature compatibility and no longer used:
    the mirror replaces the per-review staging clone.
    """
    root = Path(cache_root) if cache_root is not None else review_cache_root()
    srcmap: dict[str, tuple[Path, str]] = {}
    mirror_meta: dict[str, dict[str, str]] = {}
    for pin in pins:
        location = _pin_transport_location(pin.repo)
        mirror = root / f"{_mirror_basename(location)}.git"
        try:
            review.gitops.ensure_repo_cache(location, mirror)
        except SystemExit as exc:
            raise OpenGrReviewError(
                f"cannot ensure review mirror for {pin.key!r} at {mirror} from {location!r}: {exc}"
            ) from exc
        # Serve blobless (--filter=blob:none) clones from the mirror, same as
        # review-clone.sh sets on its cache.
        git(mirror, "config", "uploadpack.allowFilter", "true")
        # A pre-push head is ABSENT from the remote-seeded mirror BY DESIGN (a gated
        # review head is never on the remote). If the caller names a LOCAL clone that
        # holds it (the author's own desk has the objects), top the shared mirror up
        # from that clone -- into a namespaced ref so the mirror's own branch names
        # are not clobbered -- so the SAME blobless+sparse ephemeral path runs instead
        # of falling back to a full --sources-json clone (the measured 595M harm).
        local = local_sources.get(pin.key) if local_sources else None
        if local is not None and git(mirror, "cat-file", "-e", f"{pin.head}^{{commit}}").returncode != 0:
            # Fetch ONLY the pinned head (scoped to the sha, not every local branch)
            # from the local clone, then anchor it under refs/localsrc/<key>/ so the
            # object survives gc WITHOUT touching the mirror's own refs/heads/* -- a
            # desk clone's stale branches must never become the shared mirror's
            # branches. These refs/localsrc/* refs persist until gr2's next
            # `remote update --prune` on this mirror.
            fetched = git(mirror, "fetch", "--quiet", str(Path(local)), pin.head)
            if fetched.returncode != 0:
                raise OpenGrReviewError(
                    f"cannot top up review mirror for {pin.key!r} from local source "
                    f"{local!r}: {fetched.stderr.strip() or fetched.stdout.strip()}"
                )
            git(mirror, "update-ref", f"refs/localsrc/{pin.key}/head", pin.head)
        for label, sha in (("base", pin.base), ("head", pin.head)):
            if git(mirror, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
                raise OpenGrReviewError(
                    f"review mirror for {pin.key!r} does not carry {label} {sha}: publish the "
                    f"pinned head to the remote, or pass a local source that holds it"
                )
        tip = git(mirror, "rev-parse", "HEAD")
        srcmap[pin.key] = (mirror, pin.head)
        mirror_meta[pin.key] = {"mirror": str(mirror), "fetched_tip": tip.stdout.strip()}
    return srcmap, mirror_meta


def open_gr_receipt_path(review_root: Path) -> Path:
    """The project-tier receipt lives at the review lane root (not a repo's .git),
    because it names the whole review, not one member."""
    return Path(review_root) / _RECEIPT_NAME


def _current_lane_name(workspace: Path, owner_unit: str) -> str | None:
    try:
        return lane_proto.require_current_lane(workspace, owner_unit).get("lane_name")
    except SystemExit:
        return None


def open_gr_enter(
    workspace: Path,
    owner_unit: str,
    lane_name: str,
    gr_commit: str,
    sources: dict[str, tuple[Path, str]] | None = None,
    *,
    prior_cwd: Path | str,
    allow_local: bool = False,
    staging_dir: Path | str | None = None,
    local_sources: dict[str, Path | str] | None = None,
) -> project_review.ProjectReviewOutcome:
    """Open a multi-repo review from a review-KIND gr commit and enter its lane.

    Refuses a non-review-kind commit (``read_project_review_commit`` rejects a
    wrong schema) BEFORE any materialization. When ``sources`` is None the source
    transport map is RESOLVED from each pin's recorded remote through the shared
    mirror and the review lane is blobless + sparse (the production shape). For a
    PRE-PUSH head (absent on the remote by design for a gated review), pass
    ``local_sources`` (key -> local clone holding the head): the mirror is topped up
    from that clone and the SAME blobless + sparse path runs, so ``sources`` stays
    None and the lane is NOT a full clone. ``sources`` (an explicit author-clone map)
    is the older escape hatch that clones NORMALLY (full). Delegates materialization
    + lane enter to
    ``open_project_review`` (which refuses a missing pin before it clones
    anything). On success writes the project-tier receipt naming the gr commit,
    the per-repo base/head, the prior lane, and the prior cwd for exit-restore.
    """
    workspace = Path(workspace).resolve()
    # Control 1: a non-review-kind commit is refused here, before anything is made.
    rows = grip.read_project_review_commit(workspace, gr_commit)
    pins = [
        project_review.ProjectReviewPin(
            key=r["key"], repo=r["repo"], path=r["path"], base=r["base"], head=r["head"]
        )
        for r in rows
    ]
    spec = project_review.ProjectReviewSpec("gr2-project-review/v1", gr_commit, tuple(pins))

    # Carried-range reconstruction (shape (b), self-describing commit): for each pin
    # that carries a range, reconstruct its head from the commit alone into a scratch
    # clone (assert TREE == the recorded head-tree), then feed that scratch as the
    # local source. The reconstructed head is a DIFFERENT sha than the pinned head
    # (git am re-stamps the committer), so it is what the mirror is topped up with and
    # what the review lane materializes at, via materialize_heads -- while the gr
    # commit stays validated against the pinned head. The review lane is still the
    # blobless+sparse ephemeral clone; only the transient scratch is a full clone.
    materialize_heads: dict[str, str] = {}
    reconstructed_heads: dict[str, str] = {}
    scratch_root: Path | None = None
    resolve_pins = pins
    carried = grip.project_review_carried_keys(workspace, gr_commit) if sources is None else set()
    ephemeral = False
    mirror_meta: dict[str, dict[str, str]] = {}
    # The scratch clone is created (mkdtemp) and populated (reconstruct) BEFORE
    # open_project_review runs, so its removal must cover the reconstruction loop
    # and the source resolution too -- not just the open. A range that fails to
    # `git am` raises inside reconstruct_project_review_lane; if only the open
    # were wrapped, that failure would leak the scratch full clone. The finally
    # spans from mkdtemp through open so every exit path removes it.
    try:
        if carried:
            import tempfile
            scratch_root = Path(tempfile.mkdtemp(prefix="gr2-reconstruct-"))
            local_sources = dict(local_sources or {})
            resolve_pins = []
            for pin in pins:
                if pin.key in carried:
                    res = grip.reconstruct_project_review_lane(
                        workspace, gr_commit, pin.key, scratch_root / pin.key
                    )
                    rh = res["reconstructed_head"]
                    materialize_heads[pin.key] = rh
                    reconstructed_heads[pin.key] = rh
                    local_sources[pin.key] = scratch_root / pin.key
                    resolve_pins.append(dataclasses.replace(pin, head=rh))
                else:
                    resolve_pins.append(pin)

        # When sources are RESOLVED from the recorded remote, each source is the
        # persistent bare mirror and each review lane is a blobless+sparse
        # review-EPHEMERAL clone from it (never the work-lane clone seam). An explicit
        # sources map (pre-push author clones) has no mirror and clones normally.
        if sources is None:
            sources, mirror_meta = resolve_sources_from_pins(
                resolve_pins, staging_dir, allow_local=allow_local, local_sources=local_sources
            )
            ephemeral = True

        prior_lane = _current_lane_name(workspace, owner_unit)
        outcome = project_review.open_project_review(
            workspace=workspace, owner_unit=owner_unit, lane_name=lane_name,
            spec=spec, sources=sources, allow_local=allow_local, ephemeral=ephemeral,
            materialize_heads=materialize_heads,
        )
    finally:
        if scratch_root is not None:
            # Log-and-continue, deliberately: this finally spans whatever the try
            # block just did (opened a review, or raised) and a leftover scratch
            # clone must never mask either outcome by raising here instead.
            try:
                rmtree_or_refuse(scratch_root)
            except IncompleteRemoval as cleanup_exc:
                import sys
                print(f"warning: {cleanup_exc}", file=sys.stderr)
    if outcome.status != "opened" or outcome.review_root is None:
        # Refusal / partial propagates unchanged; no receipt, no enter to unwind.
        return outcome

    receipt = {
        "gr_commit": gr_commit,
        "owner_unit": owner_unit,
        "lane_name": lane_name,
        "lane_kind": "review-ephemeral" if ephemeral else "materialized",
        "prior_lane": prior_lane,
        "prior_cwd": str(Path(prior_cwd)),
        "review_root": str(outcome.review_root),
        "repos": [
            {
                "key": r["key"], "base": r["base"], "head": r["head"],
                # For a carried-range pin the lane materializes the RECONSTRUCTED head
                # (tree-equal to head, different sha until the committer-date lane);
                # record it beside the pinned head so that lane has its before/after.
                **({"reconstructed_head": reconstructed_heads[r["key"]]}
                   if r["key"] in reconstructed_heads else {}),
                # A reviewer can tell a stale mirror from a fresh one from these.
                **({"mirror": mirror_meta[r["key"]]["mirror"],
                    "fetched_tip": mirror_meta[r["key"]]["fetched_tip"]}
                   if r["key"] in mirror_meta else {}),
            }
            for r in rows
        ],
    }
    open_gr_receipt_path(outcome.review_root).write_text(json.dumps(receipt, indent=2) + "\n")
    return outcome


def provision_lane_venv(
    lane_dir: Path, bootstrap_python: str, extras: list[str] | None = None
) -> tuple[str, str]:
    """Create a fresh ``.venv`` INSIDE the reviewed lane and install the repo into it
    from its ``pyproject`` (editable), so verification runs under the LANE's OWN
    interpreter rather than the desk's. ``bootstrap_python`` only builds the venv; the
    RETURNED python is the one that then runs the tests, and its editable install makes
    ``import <pkg>`` resolve to the lane checkout. Returns (venv_python, venv_bin_dir).

    ``extras`` names the repo's own optional-dependency extras to install alongside the
    editable package (``pip install -e ".[e1,e2]"``) -- the mechanism that puts a repo's
    declared TEST dependencies (``pytest`` and what the tests import) into the lane venv,
    so ``python -m pytest`` records a real exit 0 instead of failing on a missing
    dependency. A fresh venv has pip but nothing else; without the extras a declared
    ``pytest`` command records non-zero, which is the deferred gap this closes. The
    extras come from the repo's OWN pyproject, so they resolve from wherever the repo
    already declares them (a local path reference resolves offline).

    Raises ``OpenGrReviewError`` if venv creation or the install fails."""
    lane_dir = Path(lane_dir)
    venv_dir = lane_dir / ".venv"
    bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    venv_python = bin_dir / ("python.exe" if os.name == "nt" else "python")
    create = subprocess.run(
        [bootstrap_python, "-m", "venv", str(venv_dir)],
        cwd=lane_dir, text=True, capture_output=True,
    )
    if create.returncode != 0:
        raise OpenGrReviewError(
            f"lane venv creation failed in {lane_dir}: {create.stderr.strip()}"
        )
    # One editable install; when extras are declared they ride the SAME install so the
    # test deps land in the venv, not the desk. A stray extra name pip cannot resolve
    # fails the install loudly rather than silently leaving the dep absent.
    target = "." if not extras else f".[{','.join(extras)}]"
    install = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "-e", target],
        cwd=lane_dir, text=True, capture_output=True,
    )
    if install.returncode != 0:
        raise OpenGrReviewError(
            f"lane venv editable install ({target}) failed in {lane_dir}: "
            f"{install.stderr.strip()[-500:]}"
        )
    return str(venv_python), str(bin_dir)


def record_review_verification(
    review_root: Path,
    key: str,
    *,
    command: list[str],
    interpreter: str,
    import_module: str,
    provision_venv: bool = False,
    extras: list[str] | None = None,
) -> dict:
    """Row 4 (run tests inside): run a repo's test COMMAND inside its materialized
    review lane and append a verification record to the project-tier receipt, so
    "the tests ran against the exact reviewed head, in the reviewed checkout, under
    this interpreter and this package" is FRUIT in the receipt rather than a claim
    in prose.

    The record binds four things a bare "tests pass" cannot:

    - ``exit_code``: the REAL subprocess return code, so a run that failed (or was
      never invoked) cannot be recorded as green.
    - ``head_tested``: the head the lane actually holds for this key -- the
      reconstructed head for a carried-range pin, else the pinned head -- read from
      the receipt, so a run against the desk worktree or dev tip cannot pass as the
      reviewed head.
    - ``cwd``: the materialized lane dir (``review_root/repos/<key>``), pinning the
      run to the reviewed checkout.
    - ``interpreter`` + ``module_path``: which python ran and which package file it
      imported, MEASURED by a probe under the same ``interpreter`` inside the lane
      (its real ``sys.executable`` and the imported module's ``__file__``). A stale
      editable install can silently import a DIFFERENT desk's package; a receipt
      that cannot say whose code it tested has the same defect one layer up. Pass
      the interpreter that runs ``command`` so the probe measures the tests' python.

    Returns the appended record. Raises ``OpenGrReviewError`` if there is no open-gr
    receipt, the key was not materialized by this review, its lane dir is missing,
    or the interpreter/module probe itself fails.
    """
    review_root = Path(review_root)
    receipt_path = open_gr_receipt_path(review_root)
    if not receipt_path.exists():
        raise OpenGrReviewError(
            f"no open-gr receipt at {receipt_path}; not a review opened by open-gr"
        )
    receipt = json.loads(receipt_path.read_text())
    row = next((r for r in receipt.get("repos", []) if r["key"] == key), None)
    if row is None:
        raise OpenGrReviewError(
            f"key {key!r} is not in the review receipt; cannot verify a repo the "
            f"review did not materialize"
        )
    # The lane holds the reconstructed head for a carried-range pin (tree-equal to
    # the pinned head, different sha), else the pinned head. Bind to what is on disk.
    head_tested = row.get("reconstructed_head") or row["head"]
    lane_dir = review_root / "repos" / key
    if not lane_dir.is_dir():
        raise OpenGrReviewError(
            f"materialized lane dir missing for {key!r}: {lane_dir}"
        )

    # When the spec asks for it, provision the lane's OWN venv and run under it, so the
    # verification interpreter is the reviewed checkout's python -- not the desk's whose
    # editable install could import a different tree's package. `interpreter` is
    # overridden to the lane venv python (the probe then reports IT), and the command is
    # run with the venv's bin on PATH so its `python`/tools resolve to the lane venv too.
    run_env = None
    if provision_venv:
        venv_python, venv_bin = provision_lane_venv(lane_dir, interpreter, extras=extras)
        interpreter = venv_python
        run_env = {
            **os.environ,
            "PATH": venv_bin + os.pathsep + os.environ.get("PATH", ""),
            "VIRTUAL_ENV": str(Path(venv_bin).parent),
        }

    # Run the tests inside the reviewed checkout; capture the REAL exit code. Both
    # this run and the probe below take the SAME `lane_dir` cwd variable, so the cwd
    # the probe MEASURES is the cwd the tests ran under.
    exit_code = subprocess.run(command, cwd=lane_dir, env=run_env).returncode

    # Probe, under the SAME interpreter and cwd, for the python, the package file, and
    # the working directory that actually resolve here. `cwd` is recorded from the
    # probe's own os.getcwd() -- MEASURED, not str(lane_dir) -- so the receipt reports
    # where the command ran rather than copying the intended path from the request; and
    # module_path exposes (not hides) a stale editable install that reaches outside the
    # lane.
    probe_args = [interpreter]
    if provision_venv:
        # -P (3.11+) drops the cwd/'' entry from sys.path, so `import <pkg>` must
        # resolve through the lane venv's editable install rather than through the cwd
        # copy that a plain `python -c` puts first. Without it the editable install is
        # untested: module_path reads "lane" even if the install never ran.
        probe_args.append("-P")
    probe_args += [
        "-c",
        "import sys, os, importlib; "
        f"m = importlib.import_module({import_module!r}); "
        "print(sys.executable); print(m.__file__); print(os.getcwd())",
    ]
    probe = subprocess.run(probe_args, cwd=lane_dir, text=True, capture_output=True)
    if probe.returncode != 0:
        raise OpenGrReviewError(
            f"interpreter/module probe for {import_module!r} under {interpreter} "
            f"failed (exit {probe.returncode}): {probe.stderr.strip()}"
        )
    probe_lines = probe.stdout.splitlines()
    measured_interpreter = probe_lines[0] if probe_lines else ""
    # DERIVE provisioned from the interpreter that actually ran, not from the request:
    # it is true iff the python that ran is under the lane's own .venv. A copied-from-
    # request flag would read true even if provisioning silently no-op'd.
    provisioned = bool(measured_interpreter) and (
        (lane_dir.resolve() / ".venv") in Path(measured_interpreter).parents
    )
    record = {
        "key": key,
        "command": list(command),
        "exit_code": exit_code,
        "head_tested": head_tested,
        "cwd": probe_lines[2] if len(probe_lines) > 2 else "",
        "interpreter": measured_interpreter,
        "module_path": probe_lines[1] if len(probe_lines) > 1 else "",
        "provisioned": provisioned,
    }
    receipt.setdefault("verification", []).append(record)
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    return record


def record_review_verifications(workspace: Path, review_root: Path) -> list[dict]:
    """Row 4 follow-on (multi-repo): run EVERY reviewed repo's declared test command
    inside its lane and record each in the receipt.

    The command lives in the workspace spec, per repo, so a review of N repos runs N
    verifications from one call rather than N hand-typed commands::

        [[repos]]
        name = "app"
        ...
        [repos.review_test]
        command = ["python", "-m", "pytest", "-q"]
        interpreter = "python3"
        import_module = "app_pkg"

    Iterates the repos the review materialized (from the receipt) and, for each that
    declares a ``review_test``, calls ``record_review_verification`` -- which binds the
    real exit code, the reviewed head, the measured cwd, and the interpreter/module
    actually resolved. A repo with no declaration is skipped. Returns the records
    appended this call, in the receipt's repo order.

    Refuses if the receipt names a repo absent from the spec, if a ``review_test`` is
    missing a required field, or if no reviewed repo declares one at all (a multi-repo
    verify that verified nothing is a silent pass, not a result)."""
    review_root = Path(review_root)
    receipt_path = open_gr_receipt_path(review_root)
    if not receipt_path.exists():
        raise OpenGrReviewError(
            f"no open-gr receipt at {receipt_path}; not a review opened by open-gr"
        )
    receipt = json.loads(receipt_path.read_text())
    keys = [r["key"] for r in receipt.get("repos", [])]
    spec = lane_proto.load_workspace_spec(Path(workspace))
    by_name = {r.get("name"): r for r in spec.get("repos", [])}

    records: list[dict] = []
    for key in keys:
        repo = by_name.get(key)
        if repo is None:
            raise OpenGrReviewError(
                f"reviewed repo {key!r} is not in the workspace spec; cannot resolve "
                f"its test command"
            )
        review_test = repo.get("review_test")
        if not review_test:
            continue
        for field in ("command", "interpreter", "import_module"):
            if field not in review_test:
                raise OpenGrReviewError(
                    f"review_test for {key!r} is missing required field {field!r}"
                )
        records.append(
            record_review_verification(
                review_root,
                key,
                command=list(review_test["command"]),
                interpreter=review_test["interpreter"],
                import_module=review_test["import_module"],
                provision_venv=bool(review_test.get("provision_venv", False)),
                extras=list(review_test["extras"]) if review_test.get("extras") else None,
            )
        )
    if not records:
        raise OpenGrReviewError(
            "no reviewed repo declares a [repos.review_test] command; nothing to verify"
        )
    return records


@dataclasses.dataclass(frozen=True)
class OpenGrExit:
    restored_lane: str | None
    restored_cwd: str
    gr_commit: str


def exit_gr_review(
    workspace: Path, owner_unit: str, review_root: Path, *, actor: str, adopt_legacy: bool = False
) -> OpenGrExit:
    """Exit a review opened by ``open_gr_enter``: restore the prior lane and cwd.

    Reads the project-tier receipt for the prior lane/cwd, then pops the review
    lane off the return stack (``exit_lane`` restores recent[0] as current). The
    restore is the whole point — skipping it leaves the agent stranded in the
    review lane.
    """
    import argparse

    workspace = Path(workspace).resolve()
    review_root = Path(review_root)
    if adopt_legacy:
        from .review_allocation import adopt_legacy_project_allocation
        # Target comes from the independent owning coordinate, not the receipt.
        if review_root.resolve() != workspace / "reviews" / owner_unit / review_root.name:
            raise OpenGrReviewError("legacy adoption needs the exact managed project review root")
        try:
            adopt_legacy_project_allocation(workspace, owner_unit, review_root.name)
        except ReviewAllocationError as exc:
            raise OpenGrReviewError(str(exc)) from exc
    try:
        allocation = require_allocation(workspace, review_root, owner_unit=owner_unit)
    except ReviewAllocationError as exc:
        raise OpenGrReviewError(str(exc)) from exc
    receipt_path = open_gr_receipt_path(review_root)
    retained = allocation.get("exit_receipt_hex")
    if receipt_path.is_symlink():
        raise OpenGrReviewError("project receipt is a symlink; retaining recovery")
    if receipt_path.exists():
        receipt_bytes = receipt_path.read_bytes()
        if retained is not None and receipt_bytes.hex() != retained:
            raise OpenGrReviewError("project receipt changed during cleanup; retaining recovery")
    elif retained is not None and allocation["state"] == "closing":
        receipt_bytes = bytes.fromhex(retained)
    else:
        raise OpenGrReviewError(f"project receipt unavailable at {receipt_path}; operator recovery required")
    receipt = json.loads(receipt_bytes)
    if receipt.get("owner_unit") != owner_unit or receipt.get("review_root") != str(review_root.resolve()):
        raise OpenGrReviewError("project receipt unit or target differs; refusing selection change")
    if allocation["lane_name"] != receipt.get("lane_name"):
        raise OpenGrReviewError("allocation lane differs from project receipt")
    if allocation.get("selection_restored"):
        selection_path = lane_proto.current_lane_file(workspace, owner_unit)
        if not selection_path.is_file() or selection_path.read_bytes().hex() != allocation.get("restored_selection_hex"):
            raise OpenGrReviewError("selection changed since incomplete exit; operator recovery required")
        _remove_allocated(workspace, review_root, allocation)
        return OpenGrExit(allocation.get("restored_lane"), receipt["prior_cwd"], receipt["gr_commit"])
    if _current_lane_name(workspace, owner_unit) != allocation["lane_name"]:
        raise OpenGrReviewError("current selection is not this allocated review; refusing exit")
    if receipt.get("lane_kind") == "review-ephemeral":
        if not allocation["disposable"]:
            raise OpenGrReviewError("allocation does not authorize disposable cleanup")
        allocation.update(state="closing", exit_receipt_hex=receipt_bytes.hex(),
                          preserved_runs=_preserve_allocated_runs(workspace, review_root, allocation))
        save_allocation(workspace, review_root, allocation)

    lane_proto.exit_lane(argparse.Namespace(
        workspace_root=workspace, owner_unit=owner_unit, actor=actor,
        notify_channel=False, recall=False,
    ))
    restored_lane = _current_lane_name(workspace, owner_unit)
    # A review-ephemeral review is READ-ONLY and disposable: rm -rf is the ONLY
    # cleanup (no prune verb that could ever touch a work lane). The mirror
    # persists; only the disposable review clones are removed.
    if receipt.get("lane_kind") == "review-ephemeral":
        allocation.update(selection_restored=True, restored_lane=restored_lane,
                          restored_selection_hex=lane_proto.current_lane_file(workspace, owner_unit).read_bytes().hex())
        save_allocation(workspace, review_root, allocation)
        _remove_allocated(workspace, review_root, allocation)
    return OpenGrExit(
        restored_lane=restored_lane,
        restored_cwd=receipt["prior_cwd"],
        gr_commit=receipt["gr_commit"],
    )
