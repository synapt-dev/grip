"""Grip object model: git-native multi-repo workspace snapshots.

Uses git plumbing (hash-object, mktree, commit-tree, update-ref) to store
workspace state as content-addressable objects in a dedicated .grip/ repo.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
import re

from .gitops import git
from .layout import grip_dir as _layout_grip_dir
from .workspace_guidance import missing_gr2_workspace_guidance


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GripInitError(Exception):
    """Raised when .grip/ repo is missing or not properly initialized."""


class AlphaRootRefused(GripInitError):
    """A review verb on an alpha root (its record is the `.grip/.git` snapshot store). Review binds
    live in a native root's own `.git`, so the one thing the caller can do is convert the root."""


class StoreSetupRefused(GripInitError):
    """The native store a bind sets up on a root with no store was refused by `store init` itself
    (no sibling git repositories, a pin not on its upstream, ...). The reason is `store init`'s own."""


class ReviewStoreAbsent(GripInitError):
    """A native store root with no review store: nothing has been bound here yet.

    This is the state of every fresh `store init` root, not a setup mistake, so it is
    reported as "nothing bound" and never as advice to run the verb that was just run."""


class GripCorruptError(Exception):
    """Raised when .grip/ repo state is corrupt (bad HEAD, missing objects)."""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class GripCommitInfo:
    sha: str
    message: str
    repos: list[str]
    timestamp: str = ""


@dataclass
class GripDiff:
    changed: dict[str, dict[str, str]] = field(default_factory=dict)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


_PROJECT_REVIEW_SCHEMA = "gr2-project-review/v1"
_SHA40 = re.compile(r"\A[0-9a-f]{40}\Z")


class _RangeApplyError(Exception):
    """A carried range failed to apply, or its committer metadata did not match the
    commits it describes. Callers translate this into their own refusal type."""

    def __init__(self, op: str, detail: str):
        self.op = op
        self.detail = detail
        super().__init__(f"{op}: {detail}")


def _apply_range_in_lane(lane: Path, range_patch: str, committers: str | None) -> None:
    """Apply a carried range in ``lane`` (already detached on the recorded base).

    ``committers`` None -> a plain ``git am``: the reconstructed commits are
    TREE-faithful but the committer identity and date are re-stamped, so the head
    SHA differs from the pre-push head (row 1's contract). Otherwise ``committers``
    is a TSV, one line per commit in apply order (oldest first, the order
    ``format-patch`` and ``mailsplit`` both use): ``name<TAB>email<TAB>ISO-date``.
    The range is mailsplit and each commit applied under its recorded committer
    identity AND date (author identity/date already ride in the patch), so the
    reconstructed head SHA equals the original pre-push head, not merely its tree
    (row 2's contract). A row count that does not match the commit count is a
    refusal -- the committer metadata must describe exactly the commits in the range.
    """
    import os
    import tempfile

    def _run(*args: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-c", "user.name=grip-review", "-c", "user.email=review@grip",
             "-C", str(lane), *args],
            capture_output=True, text=True, check=False, env=env,
        )

    with tempfile.TemporaryDirectory() as td:
        mbox = Path(td) / "range.patch"
        mbox.write_text(range_patch)
        if committers is None:
            p = _run("am", "--empty=keep", str(mbox))
            if p.returncode != 0:
                raise _RangeApplyError("am", p.stderr.strip()[:160])
            return
        rows = [ln for ln in committers.splitlines() if ln.strip()]
        split_dir = Path(td) / "split"
        split_dir.mkdir()
        s = _run("mailsplit", f"-o{split_dir}", str(mbox))
        if s.returncode != 0:
            raise _RangeApplyError("mailsplit", s.stderr.strip()[:160])
        patches = sorted(split_dir.glob("[0-9]*"))
        if len(patches) != len(rows):
            raise _RangeApplyError(
                "committer_count_mismatch",
                f"{len(rows)} committer row(s) for {len(patches)} commit(s)")
        for patch, row in zip(patches, rows):
            parts = row.split("\t")
            if len(parts) != 3 or not all(parts):
                raise _RangeApplyError("committer_row_malformed", row[:80])
            cn, ce, cd = parts
            env = {**os.environ, "GIT_COMMITTER_NAME": cn,
                   "GIT_COMMITTER_EMAIL": ce, "GIT_COMMITTER_DATE": cd}
            p = _run("am", "--empty=keep", str(patch), env=env)
            if p.returncode != 0:
                raise _RangeApplyError("am", p.stderr.strip()[:160])


def _carry_objects_from_range(workspace: Path, remote: str, base: str, range_patch: str,
                              committers: str | None = None, expected_head: str | None = None) -> dict[str, str]:
    """Derive the carried objects for a project-review pin from a frozen RANGE.

    The producer of a gate review holds the range.patch (the frozen artifact), not a
    clone of the pre-push head. To record the head-tree the reconstruction will be
    asserted against, apply the range over the base in a throwaway clone: clone the
    recorded remote, check out the base, apply the range, and read the resulting tree
    and fuller metadata. The range is the source of truth (it is what was frozen,
    gated, and leak-scanned).

    Without ``committers`` the applied commits are TREE-faithful only (the committer is
    re-stamped, so the derived sha differs from the pinned head). With ``committers``
    (a TSV, one committer row per commit) the reconstruction is SHA-faithful and the
    derived head is asserted equal to ``expected_head`` at create time -- so committer
    metadata that does not describe these commits is refused HERE, not baked into the
    commit to surface only when a reviewer opens it."""
    import tempfile
    if not range_patch.strip():
        raise GripCorruptError("project review range is empty")
    with tempfile.TemporaryDirectory() as td:
        lane = Path(td) / "recon"
        clone = subprocess.run(["git", "clone", "--quiet", remote, str(lane)],
                               capture_output=True, text=True, check=False)
        if clone.returncode != 0:
            raise GripCorruptError(f"cannot clone {remote} to derive review head-tree: {clone.stderr.strip()[:160]}")

        def _lg(*a: str, allow_fail: bool = False) -> subprocess.CompletedProcess[str]:
            p = subprocess.run(["git", "-c", "user.name=grip-review", "-c",
                                "user.email=review@grip", "-C", str(lane), *a],
                               capture_output=True, text=True, check=False)
            if p.returncode != 0 and not allow_fail:
                raise GripCorruptError(f"range reconstruction failed ({' '.join(a)}): {p.stderr.strip()[:160]}")
            return p

        if _lg("rev-parse", "--verify", f"{base}^{{commit}}", allow_fail=True).returncode != 0:
            raise GripCorruptError(f"base {base} not reachable on {remote} to derive review head-tree")
        _lg("checkout", "--detach", base)
        try:
            _apply_range_in_lane(lane, range_patch, committers)
        except _RangeApplyError as exc:
            raise GripCorruptError(f"range reconstruction failed ({exc.op}): {exc.detail}") from exc
        head_sha = _lg("rev-parse", "HEAD").stdout.strip()
        head_tree = _lg("rev-parse", "HEAD^{tree}").stdout.strip()
        if committers is not None and expected_head is not None and head_sha != expected_head:
            raise GripCorruptError(
                f"committer-faithful reconstruction derived {head_sha}, not the pinned head "
                f"{expected_head}: the carried committer metadata does not describe these commits")
        metadata = _lg("log", "--format=fuller", f"{base}..HEAD").stdout
    obj = {"range.patch": range_patch, "metadata": metadata, "head-tree": head_tree}
    if committers is not None:
        obj["committers"] = committers
    return obj


def create_project_review_commit(
    workspace: Path, pins: list[dict[str, str]], ranges: dict[str, str] | None = None,
    committers: dict[str, str] | None = None,
) -> str:
    created = _validate_bind_store(workspace, create=True)
    return _guarded_bind(workspace, created, lambda: _project_review_body(workspace, pins, ranges, committers))


def _project_review_body(
    workspace: Path, pins: list[dict[str, str]], ranges: dict[str, str] | None,
    committers: dict[str, str] | None,
) -> str:
    """Encode the minimal project-review gr tree through the sole object seam.

    ``ranges`` (key -> range.patch text) makes the commit SELF-DESCRIBING for a
    pre-push head: each keyed pin carries ``objects/<key>/{range.patch, metadata,
    head-tree}`` (derived by applying the range over the base, see
    ``_carry_objects_from_range``), so ``reconstruct_project_review_lane`` rebuilds
    the head from the commit alone -- no hand ``git am``, no clone that holds the
    head, no head on any remote. This ports the review-BIND carry-the-range model to
    the project path.

    ``committers`` (key -> TSV, one ``name<TAB>email<TAB>ISO-date`` row per commit in
    apply order) upgrades reconstruction from TREE-faithful to SHA-faithful: with it,
    each carried key ALSO stores ``objects/<key>/committers`` and the reconstruction
    re-stamps each commit's committer identity+date so the rebuilt head SHA equals the
    pinned pre-push head, not merely its tree (the committer-date-match contract). A
    key present in ``committers`` must also be in ``ranges``; the derived head is
    asserted equal to the pinned head at create time."""
    if not pins:
        raise GripCorruptError("project review requires at least one repository pin")
    ranges = ranges or {}
    committers = committers or {}
    entries: list[str] = []
    objects_entries: list[str] = []
    seen: set[str] = set()
    for pin in sorted(pins, key=lambda item: item["key"]):
        key = pin.get("key", "")
        if not key or key in seen or any(ch in key for ch in "/\\"):
            raise GripCorruptError(f"invalid or duplicate project review key: {key!r}")
        seen.add(key)
        fields: list[str] = []
        for name, value in (("remote", pin.get("repo", "")), ("path", pin.get("path", "")), ("commit", pin.get("head", "")), ("base", pin.get("base", ""))):
            if not value or (name in {"commit", "base"} and not _SHA40.match(value)):
                raise GripCorruptError(f"invalid project review {name} for {key}")
            fields.append(f"100644 blob {_bind_blob(workspace, value)}\t{name}")
        entries.append(f"040000 tree {_bind_mktree(workspace, fields)}\t{key}")
        if key in ranges:
            obj = _carry_objects_from_range(
                workspace, pin.get("repo", ""), pin.get("base", ""), ranges[key],
                committers=committers.get(key), expected_head=pin.get("head", ""))
            names = ("range.patch", "metadata", "head-tree") + (("committers",) if "committers" in obj else ())
            obj_fields = [f"100644 blob {_bind_blob(workspace, obj[n])}\t{n}" for n in names]
            objects_entries.append(f"040000 tree {_bind_mktree(workspace, obj_fields)}\t{key}")
    unknown = set(ranges) - seen
    if unknown:
        raise GripCorruptError(f"ranges reference keys not in the pins: {sorted(unknown)}")
    committer_only = set(committers) - set(ranges)
    if committer_only:
        raise GripCorruptError(
            f"committer metadata for keys without a carried range: {sorted(committer_only)}")
    repos_tree = _bind_mktree(workspace, entries)
    meta_tree = _bind_mktree(workspace, [f"100644 blob {_bind_blob(workspace, _PROJECT_REVIEW_SCHEMA)}\tschema", f"100644 blob {_bind_blob(workspace, 'review')}\tkind"])
    root_fields = [f"040000 tree {meta_tree}\t.grip", f"040000 tree {repos_tree}\trepos"]
    if objects_entries:
        root_fields.append(f"040000 tree {_bind_mktree(workspace, objects_entries)}\tobjects")
    root_tree = _bind_mktree(workspace, root_fields)
    commit = _bind_commit_tree(workspace, root_tree, message="grip project review")
    return _publish_bind(workspace, commit, "grip project review")


def project_review_carried_keys(workspace: Path, commit: str) -> set[str]:
    """The project-review keys that carry a reconstruction range (an ``objects/<key>``
    subtree). Empty when the commit carries no ranges (the remote-resolved case).
    Guards the absent-objects case so a plain project-review commit is not an error."""
    _validate_bind_store(workspace)
    commit = _resolve_bound(workspace, commit)
    root = {
        line.strip()
        for line in _bind_git(workspace, "ls-tree", "--name-only", commit).stdout.splitlines()
        if line.strip()
    }
    return _tree_keys(workspace, commit, "objects") if "objects" in root else set()


def reconstruct_project_review_lane(
    workspace: Path, commit: str, key: str, lane_dir: Path
) -> dict[str, str]:
    """Reconstruct a project-review pin's head from its carried range (shape (b)).

    Refuses anything but a project-review-KIND commit, then reuses the review-BIND
    reconstruction (clone remote, check out base, ``git am`` the carried range, assert
    the resulting TREE equals the recorded head-tree). The assertion is on the TREE,
    never the sha: ``git am`` re-stamps the committer, so the reconstructed sha differs
    from the pinned head until the committer-date-match lane; both are returned so that
    lane has its before/after."""
    actual = _bind_git(workspace, "show", f"{commit}:.grip/schema").stdout.strip()
    if actual != _PROJECT_REVIEW_SCHEMA:
        raise GripCorruptError(
            f"not a gr2 project review commit: found {actual or '<none>'!r}, expected {_PROJECT_REVIEW_SCHEMA!r}"
        )
    return reconstruct_review_lane(workspace, commit, key, lane_dir)


def read_project_review_commit(workspace: Path, commit: str) -> list[dict[str, str]]:
    """Strictly decode the minimal reviewed repository fields."""
    _validate_bind_store(workspace)
    commit = _resolve_bound(workspace, commit)
    actual_schema = _bind_git(workspace, "show", f"{commit}:.grip/schema").stdout.strip()
    if actual_schema != _PROJECT_REVIEW_SCHEMA:
        raise GripCorruptError(
            f"not a gr2 project review commit: found kind {actual_schema or '<none>'!r}, "
            f"expected {_PROJECT_REVIEW_SCHEMA!r} (a project-review-KIND commit; use `review open` "
            f"for a review-BIND commit)"
        )
    rows = _read_repo_state(workspace, commit, bind=True)
    decoded: list[dict[str, str]] = []
    for key, fields in sorted(rows.items()):
        if set(fields) != {"remote", "path", "commit", "base"} or not _SHA40.match(fields["commit"]) or not _SHA40.match(fields["base"]):
            raise GripCorruptError(f"invalid project review repository tree: {key}")
        decoded.append({"key": key, "repo": fields["remote"], "path": fields["path"], "head": fields["commit"], "base": fields["base"]})
    return decoded


# ---------------------------------------------------------------------------
# kind=workspace: a lane's resolved repository state captured as one gr commit.
# Same section-5 shape as the review kind, different .grip/kind. Records the
# resolved head each repo checks out plus the base it builds on; the commit is
# the reproduction coordinate for the workspace at snapshot time.
# ---------------------------------------------------------------------------

_WORKSPACE_SCHEMA = "gr2-workspace/v1"


def create_workspace_commit(workspace: Path, repos: list[dict[str, str]]) -> str:
    """Encode a kind=workspace gr commit through the sole object seam."""
    _validate_grip_repo(workspace)
    if not repos:
        raise GripCorruptError("workspace commit requires at least one repository")
    entries: list[str] = []
    seen: set[str] = set()
    for repo in sorted(repos, key=lambda item: item["key"]):
        key = repo.get("key", "")
        if not key or key in seen or any(ch in key for ch in "/\\"):
            raise GripCorruptError(f"invalid or duplicate workspace repo key: {key!r}")
        seen.add(key)
        fields: list[str] = []
        for name, value in (("remote", repo.get("remote", "")), ("path", repo.get("path", "")), ("commit", repo.get("commit", "")), ("base", repo.get("base", ""))):
            if not value or (name in {"commit", "base"} and not _SHA40.match(value)):
                raise GripCorruptError(f"invalid workspace {name} for {key}")
            fields.append(f"100644 blob {_hash_blob(workspace, value)}\t{name}")
        entries.append(f"040000 tree {_mktree(workspace, fields)}\t{key}")
    repos_tree = _mktree(workspace, entries)
    meta_tree = _mktree(workspace, [f"100644 blob {_hash_blob(workspace, _WORKSPACE_SCHEMA)}\tschema", f"100644 blob {_hash_blob(workspace, 'workspace')}\tkind"])
    root_tree = _mktree(workspace, [f"040000 tree {meta_tree}\t.grip", f"040000 tree {repos_tree}\trepos"])
    commit = _commit_tree(workspace, root_tree, parent=_current_head(workspace), message="grip workspace snapshot")
    _grip_git(workspace, "update-ref", "HEAD", commit)
    return commit


def read_workspace_commit(workspace: Path, commit: str) -> list[dict[str, str]]:
    """Strictly decode a kind=workspace gr commit's resolved repository fields."""
    if _grip_git(workspace, "show", f"{commit}:.grip/schema").stdout.strip() != _WORKSPACE_SCHEMA:
        raise GripCorruptError("not a gr2 workspace commit")
    if _grip_git(workspace, "show", f"{commit}:.grip/kind").stdout.strip() != "workspace":
        raise GripCorruptError("gr2 workspace commit has the wrong kind")
    rows = _read_repo_state(workspace, commit)
    decoded: list[dict[str, str]] = []
    for key, fields in sorted(rows.items()):
        if set(fields) != {"remote", "path", "commit", "base"} or not _SHA40.match(fields["commit"]) or not _SHA40.match(fields["base"]):
            raise GripCorruptError(f"invalid workspace repository tree: {key}")
        decoded.append({"key": key, "remote": fields["remote"], "path": fields["path"], "commit": fields["commit"], "base": fields["base"]})
    return decoded


# ---------------------------------------------------------------------------
# The gate on gr2: review bind + verify
#
# A review gr commit is a project-review commit plus two subtrees: observed/
# (the live remote head of each row's target ref at bind time) and texts/ (the
# platform title and body, NORM: trailing newlines stripped). The gr commit id
# is the freeze; the frozen directory and its five SHA-256s are deleted, not
# wrapped: the object is its own frozen record.
# ---------------------------------------------------------------------------

_REVIEW_BIND_SCHEMA = "gr2-review-bind/v2"


class GripReviewRefused(Exception):
    """A bind refusal. Carries the refusal name and the two values that disagreed."""

    def __init__(self, refusal: str, expected: str = "", observed: str = "") -> None:
        self.refusal = refusal
        self.expected = expected
        self.observed = observed
        detail = f": expected {expected!r}, observed {observed!r}" if expected or observed else ""
        super().__init__(f"{refusal}{detail}")


def _norm_text(value: str) -> str:
    """NORM a platform text: strip trailing newlines (the freeze's own rule)."""
    return value.rstrip("\n")


def _remote_head(workspace: Path, remote: str, ref: str) -> str:
    """The live head of one ref on a remote, or '' if absent. A ref name like
    refs/heads/<branch>; ls-remote is read-only and needs no local ref."""
    proc = _bind_git(workspace, "ls-remote", remote, ref)
    if proc.returncode != 0:
        raise GripReviewRefused("remote_unreadable", ref, proc.stderr.strip()[:120])
    line = proc.stdout.strip().splitlines()
    return line[0].split("\t", 1)[0] if line else ""


def _head_present_on_remote(workspace: Path, remote: str, head: str) -> bool:
    """True if the head SHA is already an object any ref on the remote points at.
    A pre-push branch's head is present at no ref; a re-freeze of an already
    pushed head is refused unless a prior ratify receipt is named."""
    proc = _bind_git(workspace, "ls-remote", remote)
    if proc.returncode != 0:
        raise GripReviewRefused("remote_unreadable", remote, proc.stderr.strip()[:120])
    return any(row.split("\t", 1)[0] == head for row in proc.stdout.splitlines())


def _source_capture(source: str, *args: str) -> str:
    """Run a read-only git command in the author's source repo and return stdout.

    Bind carries the frozen set INSIDE the gr commit (decision (a), 2026-09-02):
    the object must reconstruct a pre-push head with nothing but itself and the
    live base, so bind derives the range, the fuller metadata, and the head tree
    from the source that actually holds the head. Read-only; a failure refuses
    the bind rather than writing a partial object."""
    proc = subprocess.run(
        ["git", "-C", source, *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise GripReviewRefused(
            "source_unreadable", f"{source} git {' '.join(args)}", proc.stderr.strip()[:160]
        )
    return proc.stdout


def _carry_objects(workspace: Path, source: str, base: str, head: str) -> dict[str, str]:
    """From the author's source repo, capture the range (format-patch base..head),
    the fuller metadata (author+committer per commit), and the head tree. These
    are the bytes the hand freeze produced by hand; here they live in the object.
    The head tree is recorded so ``run`` can assert the reconstruction matches it
    WITHOUT needing the pre-push head object anywhere but the range."""
    if _source_capture(source, "rev-parse", "--verify", f"{head}^{{commit}}").strip() != head:
        raise GripReviewRefused("source_missing_head", head, source)
    if _source_capture(source, "rev-parse", "--verify", f"{base}^{{commit}}").strip() != base:
        raise GripReviewRefused("source_missing_base", base, source)
    range_patch = _source_capture(source, "format-patch", f"{base}..{head}", "--stdout")
    if not range_patch.strip():
        raise GripReviewRefused("empty_range", f"{base}..{head}", source)
    metadata = _source_capture(source, "log", "--format=fuller", f"{base}..{head}")
    head_tree = _source_capture(source, "rev-parse", f"{head}^{{tree}}").strip()
    return {"range.patch": range_patch, "metadata": metadata, "head-tree": head_tree}


def _run_policy_hook(policy_hook: list[str] | None, scan_items: list[tuple[str, str]]) -> str:
    """Run the configured policy hook over the carried readable bytes.

    None = OSS default, no hook, recorded as ``no-policy``. Otherwise the carried
    range and texts are written to a temp directory and the hook is invoked with
    that directory as its last argument; a nonzero exit REFUSES the bind (a bind
    that carries a leak refuses like a freeze), and the clean verdict is recorded
    in the object so a second reader can see which policy cleared it."""
    if policy_hook is None:
        return "no-policy"
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        for name, content in scan_items:
            (Path(td) / name).write_text(content)
        proc = subprocess.run(
            [*policy_hook, td], capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            raise GripReviewRefused(
                "policy_hook_refused", " ".join(policy_hook),
                (proc.stdout + proc.stderr).strip()[:200],
            )
        return f"clean: {' '.join(policy_hook)} exit 0"


def _range_terminal_head(range_patch: str) -> str | None:
    """The sha the range's LAST commit was formatted from. `git format-patch` writes
    one ``From <40-hex> `` header per commit, in apply order, so the terminal one is
    the range's head. Returns None if the patch carries no From header. Used to defend
    the bind: the head a range describes must equal the declared --head (the same
    guarantee the --source path gets from its rev-parse of the head in the clone)."""
    found: str | None = None
    for line in range_patch.splitlines():
        if line.startswith("From ") and len(line) >= 45 and line[45] == " ":
            cand = line[5:45]
            if _SHA40.match(cand):
                found = cand
    return found


def create_review_bind_commit(
    workspace: Path, rows: list[dict[str, str]], *, ratified: str | None = None,
    policy_hook: list[str] | None = None,
) -> str:
    """Bind a review gr commit (rows, refusals and the policy hook: see `_bind_review_rows`).

    On a native root the bind is a parentless commit in the root's own `.git`, published as
    `refs/dev.synapt.grip/__reviews__/<commit>` as the LAST step and create-only, so a bind that refuses leaves no
    ref and no store: only unreferenced objects, which `git gc` collects. A root that is not
    native is refused (an alpha root names `store migrate`)."""
    return _bind_review_rows(workspace, rows, ratified=ratified, policy_hook=policy_hook)


def _bind_review_rows(
    workspace: Path, rows: list[dict[str, str]], *, ratified: str | None = None,
    policy_hook: list[str] | None = None,
) -> str:
    created = _validate_bind_store(workspace, create=True)
    return _guarded_bind(workspace, created, lambda: _bind_review_rows_body(
        workspace, rows, ratified=ratified, policy_hook=policy_hook))


def _bind_review_rows_body(
    workspace: Path, rows: list[dict[str, str]], *, ratified: str | None = None,
    policy_hook: list[str] | None = None,
) -> str:
    """Bind a review gr commit. Each row: key, remote, path, head, base, ref, title, body.

    Reads the live remote head of every row's target ref, records it under
    observed/, and refuses BEFORE writing anything if base is not that head
    (behind-must-be-0) or if head is already on the remote without a named
    ratify receipt (the 2026-09-02 pre-gate-push lesson).

    ``policy_hook`` is the OSS-neutral seam: a configured command
    that receives a directory of the carried readable bytes (the range and the
    platform texts, which is what a leak scanner must see — commit messages
    leak, packs hide them) and refuses the bind on a nonzero exit, the way a
    freeze refuses today. OSS ships no hook (records ``no-policy``); our config
    points it at the leak scanner. The verdict is recorded in the object."""
    if not rows:
        raise GripCorruptError("review bind requires at least one repository row")
    entries: list[str] = []
    observed_entries: list[str] = []
    texts_entries: list[str] = []
    objects_entries: list[str] = []
    evidence_entries: list[str] = []
    scan_items: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in sorted(rows, key=lambda item: item["key"]):
        key = row.get("key", "")
        if not key or key in seen or any(ch in key for ch in "/\\"):
            raise GripCorruptError(f"invalid or duplicate review key: {key!r}")
        seen.add(key)
        remote, path, head, base, ref = (
            row.get("remote", ""), row.get("path", ""), row.get("head", ""),
            row.get("base", ""), row.get("ref", ""),
        )
        for name, value, sha in (("remote", remote, False), ("path", path, False),
                                 ("commit", head, True), ("base", base, True), ("ref", ref, False)):
            if not value or (sha and not _SHA40.match(value)):
                raise GripReviewRefused("invalid_field", f"{key}/{name}", value)

        # Refusal 1: base must be the live remote head of the target ref.
        observed = _remote_head(workspace, remote, ref)
        if base != observed:
            raise GripReviewRefused("base_not_live_head", base, observed)
        # Refusal 2: head must not already be on the remote (unless ratified).
        if _head_present_on_remote(workspace, remote, head) and not ratified:
            raise GripReviewRefused("head_already_on_remote", head, "present")

        fields = [f"100644 blob {_bind_blob(workspace, v)}\t{n}"
                  for n, v in (("remote", remote), ("path", path), ("commit", head), ("base", base))]
        entries.append(f"040000 tree {_bind_mktree(workspace, fields)}\t{key}")
        # remote-head field hoisted out of the outer f-string's expression: a
        # nested f-string carrying \t inside {…} is a SyntaxError on Python 3.11
        # (PEP 701 only lifted this in 3.12+), and gr2 supports >=3.11. Output is
        # byte-identical, so every content-hash is unchanged.
        remote_head_field = f"100644 blob {_bind_blob(workspace, observed)}\tremote-head"
        observed_entries.append(
            f"040000 tree {_bind_mktree(workspace, [remote_head_field])}\t{key}"
        )
        title = _norm_text(row.get("title", ""))
        body = _norm_text(row.get("body", ""))
        text_fields = [f"100644 blob {_bind_blob(workspace, title)}\ttitle",
                       f"100644 blob {_bind_blob(workspace, body)}\tbody"]
        texts_entries.append(f"040000 tree {_bind_mktree(workspace, text_fields)}\t{key}")

        # (a): carry the frozen set inside the object. A row with a source repo
        # carries range.patch + fuller metadata + head-tree so a pre-push head
        # reconstructs from the object alone. Rows without a source are the
        # legacy shape (SHAs only) and carry no objects subtree entry.
        scan_items.append((f"{key}.title", title))
        scan_items.append((f"{key}.body", body))
        source = row.get("source")
        range_patch = row.get("range_patch")
        if source and range_patch:
            raise GripCorruptError(
                f"row {key}: source and range_patch are mutually exclusive "
                "(a clone that holds the head vs the frozen range itself)")
        obj: dict[str, str] | None = None
        if source:
            obj = _carry_objects(workspace, source, base, head)
        elif range_patch:
            # A classic freeze-public-range.sh range.patch, head local-only: derive
            # the head-tree by applying the range over base in a throwaway clone, so
            # the producer owns the git am end to end (no author clone required).
            # Head defense (parity with the --source path's source_missing_head
            # check): the range's terminal `From <sha>` header is the head the patch
            # was formatted from; it must equal the declared head, or the row would
            # bind a range describing a DIFFERENT head than the one recorded here and
            # refused-against on the remote.
            from_head = _range_terminal_head(range_patch)
            if from_head is None:
                raise GripReviewRefused("range_no_from_header", key, "range.patch carries no From <sha> header")
            if from_head != head:
                raise GripReviewRefused("range_head_mismatch", head, from_head)
            obj = _carry_objects_from_range(workspace, remote, base, range_patch)
        if obj is not None:
            obj_fields = [f"100644 blob {_bind_blob(workspace, obj[n])}\t{n}"
                          for n in ("range.patch", "metadata", "head-tree")]
            objects_entries.append(f"040000 tree {_bind_mktree(workspace, obj_fields)}\t{key}")
            scan_items.append((f"{key}.range.patch", obj["range.patch"]))
        evidence = row.get("evidence")
        if evidence:
            ev_fields = [f"100644 blob {_bind_blob(workspace, evidence)}\tcommands"]
            resolution = row.get("resolution")
            if resolution:
                ev_fields.append(f"100644 blob {_bind_blob(workspace, resolution)}\tresolution")
            evidence_entries.append(f"040000 tree {_bind_mktree(workspace, ev_fields)}\t{key}")

    # Policy hook: scan the carried readable bytes; refuse on a hit
    # the way a freeze does, and record the verdict in the object.
    policy_verdict = _run_policy_hook(policy_hook, scan_items)

    repos_tree = _bind_mktree(workspace, entries)
    observed_tree = _bind_mktree(workspace, observed_entries)
    texts_tree = _bind_mktree(workspace, texts_entries)
    meta_tree = _bind_mktree(workspace, [
        f"100644 blob {_bind_blob(workspace, _REVIEW_BIND_SCHEMA)}\tschema",
        f"100644 blob {_bind_blob(workspace, 'review')}\tkind",
        f"100644 blob {_bind_blob(workspace, policy_verdict)}\tpolicy",
    ])
    root_fields = [
        f"040000 tree {meta_tree}\t.grip",
        f"040000 tree {observed_tree}\tobserved",
        f"040000 tree {repos_tree}\trepos",
        f"040000 tree {texts_tree}\ttexts",
    ]
    if objects_entries:
        root_fields.append(f"040000 tree {_bind_mktree(workspace, objects_entries)}\tobjects")
    if evidence_entries:
        root_fields.append(f"040000 tree {_bind_mktree(workspace, evidence_entries)}\tevidence")
    root_tree = _bind_mktree(workspace, root_fields)
    commit = _bind_commit_tree(workspace, root_tree, message="grip review bind")
    return _publish_bind(workspace, commit, "grip review bind")


def verify_review_commit(workspace: Path, commit: str) -> dict[str, object]:
    """Re-derive the review gr commit from its own objects and report what was measured (see
    `_verify_review_commit_in_store`). On a native root the id must be a bound review."""
    _validate_bind_store(workspace)
    return _verify_review_commit_in_store(workspace, _resolve_bound(workspace, commit))


def _verify_review_commit_in_store(workspace: Path, commit: str) -> dict[str, object]:
    """Re-derive the review gr commit from its own objects and report what was
    measured: the recomputed root tree (must equal the commit's tree, else
    corruption), and per row the remote/path/head/base, the observed remote
    head, and the SHA-256 of each NORM'd text (the bridge to the frozen title/
    body NORM the hand gate produced)."""
    import hashlib

    if _bind_git(workspace, "show", f"{commit}:.grip/schema").stdout.strip() != _REVIEW_BIND_SCHEMA:
        raise GripCorruptError("not a gr2 review bind commit")

    stored_tree = _bind_git(workspace, "rev-parse", f"{commit}^{{tree}}").stdout.strip()
    rows = _read_repo_state(workspace, commit, bind=True)
    root_paths = {
        line.strip()
        for line in _bind_git(workspace, "ls-tree", "--name-only", commit).stdout.splitlines()
        if line.strip()
    }
    has_objects = "objects" in root_paths
    has_evidence = "evidence" in root_paths
    objects_keys = _tree_keys(workspace, commit, "objects") if has_objects else set()
    evidence_keys = _tree_keys(workspace, commit, "evidence") if has_evidence else set()
    measured: list[dict[str, str]] = []
    recomputed_entries: list[str] = []
    observed_recomputed: list[str] = []
    texts_recomputed: list[str] = []
    objects_recomputed: list[str] = []
    evidence_recomputed: list[str] = []
    for key, fields in sorted(rows.items()):
        if set(fields) != {"remote", "path", "commit", "base"}:
            raise GripCorruptError(f"invalid review repository tree: {key}")
        observed = _bind_git(workspace, "show", f"{commit}:observed/{key}/remote-head").stdout.strip()
        title = _bind_git(workspace, "show", f"{commit}:texts/{key}/title").stdout
        body = _bind_git(workspace, "show", f"{commit}:texts/{key}/body").stdout
        title_norm = _norm_text(title)
        body_norm = _norm_text(body)
        row_measured = {
            "key": key, "remote": fields["remote"], "path": fields["path"],
            "head": fields["commit"], "base": fields["base"], "observed_remote_head": observed,
            "base_equals_observed": str(fields["base"] == observed),
            "title_sha256": hashlib.sha256(title_norm.encode()).hexdigest(),
            "body_sha256": hashlib.sha256(body_norm.encode()).hexdigest(),
        }
        # Recompute the three base subtrees from the decoded content, exactly as bind built them.
        f = [f"100644 blob {_bind_blob(workspace, v)}\t{n}"
             for n, v in (("remote", fields["remote"]), ("path", fields["path"]),
                          ("commit", fields["commit"]), ("base", fields["base"]))]
        recomputed_entries.append(f"040000 tree {_bind_mktree(workspace, f)}\t{key}")
        # remote-head field hoisted (see create_review_bind_commit): nested
        # f-string with \t in the expression is a 3.11 SyntaxError; byte-identical.
        remote_head_field = f"100644 blob {_bind_blob(workspace, observed)}\tremote-head"
        observed_recomputed.append(
            f"040000 tree {_bind_mktree(workspace, [remote_head_field])}\t{key}"
        )
        tf = [f"100644 blob {_bind_blob(workspace, title_norm)}\ttitle",
              f"100644 blob {_bind_blob(workspace, body_norm)}\tbody"]
        texts_recomputed.append(f"040000 tree {_bind_mktree(workspace, tf)}\t{key}")

        # (a): the carried frozen set. head-tree is what run asserts the
        # reconstruction against; range/metadata are the readable bytes the
        # leak scanner and reviewer see.
        if key in objects_keys:
            rng = _bind_git(workspace, "show", f"{commit}:objects/{key}/range.patch").stdout
            meta = _bind_git(workspace, "show", f"{commit}:objects/{key}/metadata").stdout
            head_tree = _bind_git(workspace, "show", f"{commit}:objects/{key}/head-tree").stdout.strip()
            of = [f"100644 blob {_bind_blob(workspace, v)}\t{n}"
                  for n, v in (("range.patch", rng), ("metadata", meta), ("head-tree", head_tree))]
            objects_recomputed.append(f"040000 tree {_bind_mktree(workspace, of)}\t{key}")
            row_measured["head_tree"] = head_tree
            row_measured["range_sha256"] = hashlib.sha256(rng.encode()).hexdigest()
        if key in evidence_keys:
            ev_paths = _tree_keys(workspace, commit, f"evidence/{key}")
            ef = []
            for name in ("commands", "resolution"):
                if name in ev_paths:
                    content = _bind_git(workspace, "show", f"{commit}:evidence/{key}/{name}").stdout
                    ef.append(f"100644 blob {_bind_blob(workspace, content)}\t{name}")
            evidence_recomputed.append(f"040000 tree {_bind_mktree(workspace, ef)}\t{key}")

        measured.append(row_measured)

    policy = _bind_git(workspace, "show", f"{commit}:.grip/policy").stdout
    meta_tree = _bind_mktree(workspace, [
        f"100644 blob {_bind_blob(workspace, _REVIEW_BIND_SCHEMA)}\tschema",
        f"100644 blob {_bind_blob(workspace, 'review')}\tkind",
        f"100644 blob {_bind_blob(workspace, policy)}\tpolicy",
    ])
    root_fields = [
        f"040000 tree {meta_tree}\t.grip",
        f"040000 tree {_bind_mktree(workspace, observed_recomputed)}\tobserved",
        f"040000 tree {_bind_mktree(workspace, recomputed_entries)}\trepos",
        f"040000 tree {_bind_mktree(workspace, texts_recomputed)}\ttexts",
    ]
    if objects_recomputed:
        root_fields.append(f"040000 tree {_bind_mktree(workspace, objects_recomputed)}\tobjects")
    if evidence_recomputed:
        root_fields.append(f"040000 tree {_bind_mktree(workspace, evidence_recomputed)}\tevidence")
    recomputed_tree = _bind_mktree(workspace, root_fields)
    return {
        "commit": commit,
        "stored_tree": stored_tree,
        "recomputed_tree": recomputed_tree,
        "tree_matches": stored_tree == recomputed_tree,
        "rows": measured,
    }


def _tree_keys(workspace: Path, commit: str, path: str) -> set[str]:
    """The immediate child names of a subtree in a gr commit (empty if absent)."""
    proc = _bind_git(workspace, "ls-tree", "--name-only", f"{commit}:{path}")
    if proc.returncode != 0:
        return set()
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def parse_evidence(text: str) -> list[dict[str, str]]:
    """Parse the carried evidence blob into declared checks.

    Format (the hand freeze's own evidence.txt): blocks separated by a line of
    ``---``, each with ``label:``, ``command:``, and ``exit:`` fields. The
    author's own runs; inputs to ``run``, not proof (a receipt that only re-runs
    them ratifies the disclosure axis and says so)."""
    checks: list[dict[str, str]] = []
    for block in re.split(r"(?m)^---\s*$", text):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            m = re.match(r"\s*(label|command|exit):\s*(.*)$", line)
            if m:
                fields[m.group(1)] = m.group(2).strip()
        if fields.get("command"):
            checks.append({
                "label": fields.get("label", ""),
                "command": fields["command"],
                "expected_exit": fields.get("exit", ""),
            })
    return checks


def run_review_checks(
    workspace: Path, commit: str, key: str, lane_dir: Path, *, env: dict[str, str] | None = None
) -> dict[str, object]:
    """Reconstruct the lane, then execute the carried declared checks INSIDE it.

    Reconstruction (with its tree assertion) runs first; a tree mismatch refuses
    before a single check executes. Each check runs with cwd = the lane (a check
    that escapes the lane is running against the wrong tree), and records
    command, cwd, exit, the declared expected exit, whether they matched, and a
    digest of the output tail. RAN is a field, not a label."""
    import hashlib

    import os

    materialized = reconstruct_review_lane(workspace, commit, key, lane_dir)
    lane = Path(materialized["lane"])
    checks = []
    if key in _tree_keys(workspace, commit, "evidence"):
        if "commands" in _tree_keys(workspace, commit, f"evidence/{key}"):
            checks = parse_evidence(
                _bind_git(workspace, "show", f"{commit}:evidence/{key}/commands").stdout
            )

    # Resolution: pin PYTHONPATH to the reconstructed lane so a declared check
    # (e.g. `python -m pytest ...`) imports the REVIEWED tree, never a machine-wide
    # install (the 2026-08-11 lesson: a green suite about someone else's checkout).
    # The caller's env wins if it sets PYTHONPATH explicitly.
    src = lane / "src"
    lane_pp = str(src if src.is_dir() else lane)
    base_env = {**os.environ}
    existing_pp = base_env.get("PYTHONPATH", "")
    base_env["PYTHONPATH"] = f"{lane_pp}{os.pathsep}{existing_pp}" if existing_pp else lane_pp
    run_env = {**base_env, **(env or {})}
    import_resolution = lane_pp
    runs: list[dict[str, object]] = []
    import shlex
    for check in checks:
        argv = shlex.split(check["command"])
        proc = subprocess.run(
            argv, cwd=str(lane), env=run_env, capture_output=True, text=True, check=False
        )
        tail = (proc.stdout + proc.stderr)[-4096:]
        runs.append({
            "label": check["label"],
            "command": check["command"],
            "cwd": str(lane),
            "exit": proc.returncode,
            "expected_exit": check["expected_exit"],
            "exit_matched": (check["expected_exit"] == "" or str(proc.returncode) == check["expected_exit"]),
            "output_digest": hashlib.sha256(tail.encode()).hexdigest(),
            "kind": "declared",
        })
    return {"materialized": materialized, "runs": runs, "import_resolution": import_resolution}


_FINDING_REQUIRED_WHEN_BLOCKING = ("seam", "smallest_fix", "witness", "risk")


def validate_finding(finding: dict[str, object]) -> dict[str, object]:
    """A blocking finding is refused unless every required field is present (the
    reviewer-who-blocks-must-propose rule, as a constraint, not a habit)."""
    if finding.get("blocking"):
        missing = [f for f in _FINDING_REQUIRED_WHEN_BLOCKING if not finding.get(f)]
        if missing:
            raise GripReviewRefused("incomplete_blocking_finding", ",".join(missing), "")
    return finding


def build_review_receipt(
    workspace: Path,
    commit: str,
    *,
    actor: str,
    verdict: str,
    axes: dict[str, str],
    run_results: dict[str, object],
    read: list[str] | None = None,
    probes: list[dict[str, object]] | None = None,
    mutations: list[dict[str, object]] | None = None,
    findings: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Assemble the receipt object: one object per actor per gr commit.

    Records verify (recomputed tree), liveness (every observed head re-read now),
    the materialized reconstruction (reconstructed head + tree beside the bound
    ones), the runs, and the findings. A block verdict needs at least one blocking
    finding, and each blocking finding is refused unless complete."""
    if verdict not in ("ratify", "block"):
        raise GripReviewRefused("invalid_verdict", verdict, "ratify|block")
    findings = [validate_finding(f) for f in (findings or [])]
    if verdict == "block" and not any(f.get("blocking") for f in findings):
        raise GripReviewRefused("block_without_blocking_finding", verdict, "")

    v = verify_review_commit(workspace, commit)
    # Liveness: re-read every observed remote head NOW and compare to the bound base.
    liveness: list[dict[str, str]] = []
    for key, fields in sorted(_read_repo_state(workspace, commit, bind=True).items()):
        live = _remote_head(workspace, fields["remote"], _row_ref(workspace, commit, key))
        liveness.append({
            "key": key, "bound_base": fields["base"], "live_remote_head": live,
            "state": "equal" if live == fields["base"] else "moved",
        })
    return {
        "gr_commit": commit,
        "actor": actor,
        "verdict": verdict,
        "axes": axes,
        "liveness": liveness,
        "verify": {"tree_matches": v["tree_matches"], "recomputed_tree": v["recomputed_tree"]},
        "materialized": run_results.get("materialized"),
        "import_resolution": run_results.get("import_resolution"),
        "runs": run_results.get("runs", []),
        "read": read or [],
        "probes": probes or [],
        "mutations": mutations or [],
        "findings": findings,
        "expires_on": {"any_base_moved": any(l["state"] == "moved" for l in liveness)},
    }


def _row_ref(workspace: Path, commit: str, key: str) -> str:
    """The target ref a bound row's base was observed against. Stored implicitly:
    bind recorded observed/<key>/remote-head against the row's ref. The ref name
    itself is not separately stored in v2, so liveness re-reads the base's ref by
    convention from the remote's default integration branch when unknown; here we
    fall back to refs/heads/dev, the team's only integration branch."""
    return "refs/heads/dev"


def review_row_keys(workspace: Path, commit: str) -> list[str]:
    """The repository keys bound in a review gr commit, sorted. Cheap: reads the
    repos/ subtree only (no tree recomputation)."""
    _validate_bind_store(workspace)
    commit = _resolve_bound(workspace, commit)
    return sorted(_read_repo_state(workspace, commit, bind=True).keys())


def reconstruct_review_lane(
    workspace: Path, commit: str, key: str, lane_dir: Path
) -> dict[str, str]:
    """Materialize a bound row's head by reconstruction, decision (a).

    Clone the recorded remote, check out the recorded BASE (the live remote head at
    bind), apply the carried range, and assert the resulting tree equals the bound
    head-tree. The reconstruction never needs the pre-push head object anywhere but
    the carried range; a tree mismatch is a REFUSAL, raised before ``run`` executes a
    single check (a check over a tree that is not the reviewed tree is a finding
    about the wrong bytes).

    When the commit ALSO carries ``objects/<key>/committers`` (the committer-date-
    match contract), the range is applied commit-by-commit under each commit's
    recorded committer identity+date, so the reconstructed head SHA equals the pinned
    pre-push head, not merely its tree -- and the SHA is asserted equal to the bound
    head. Without that object (a range-1-era commit) it falls back to a plain
    ``git am``: tree-faithful, committer re-stamped, so the returned reconstructed_head
    differs from the bound head by design."""
    _validate_bind_store(workspace)
    commit = _resolve_bound(workspace, commit)
    if key not in _tree_keys(workspace, commit, "objects"):
        raise GripReviewRefused("row_carries_no_objects", key, "reconstruction needs a carried range")
    repo = _read_repo_state(workspace, commit, bind=True)[key]
    remote, base, bound_head = repo["remote"], repo["base"], repo["commit"]
    head_tree_expected = _bind_git(workspace, "show", f"{commit}:objects/{key}/head-tree").stdout.strip()
    committers_proc = _bind_git(workspace, "show", f"{commit}:objects/{key}/committers")
    committers = committers_proc.stdout if committers_proc.returncode == 0 else None
    range_text = _bind_git(workspace, "show", f"{commit}:objects/{key}/range.patch").stdout

    lane_dir = Path(lane_dir)
    lane_dir.parent.mkdir(parents=True, exist_ok=True)

    def _lg(*args: str, allow_fail: bool = False) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            ["git", "-c", "user.name=grip-review", "-c", "user.email=review@grip", "-C",
             str(lane_dir), *args],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0 and not allow_fail:
            raise GripReviewRefused("reconstruct_failed", " ".join(args), proc.stderr.strip()[:160])
        return proc

    clone = subprocess.run(
        ["git", "clone", "--quiet", remote, str(lane_dir)],
        capture_output=True, text=True, check=False,
    )
    if clone.returncode != 0:
        raise GripReviewRefused("clone_failed", remote, clone.stderr.strip()[:160])
    if _lg("rev-parse", "--verify", f"{base}^{{commit}}", allow_fail=True).returncode != 0:
        raise GripReviewRefused("base_unreachable_on_remote", base, remote)
    _lg("checkout", "--detach", base)
    try:
        _apply_range_in_lane(lane_dir, range_text, committers)
    except _RangeApplyError as exc:
        raise GripReviewRefused("reconstruct_failed", exc.op, exc.detail) from exc

    reconstructed_head = _lg("rev-parse", "HEAD").stdout.strip()
    reconstructed_tree = _lg("rev-parse", "HEAD^{tree}").stdout.strip()
    if reconstructed_tree != head_tree_expected:
        raise GripReviewRefused("tree_mismatch", head_tree_expected, reconstructed_tree)
    if committers is not None and reconstructed_head != bound_head:
        raise GripReviewRefused("sha_mismatch", bound_head, reconstructed_head)
    return {
        "lane": str(lane_dir),
        "bound_head": bound_head,
        "reconstructed_head": reconstructed_head,
        "bound_head_tree": head_tree_expected,
        "reconstructed_tree": reconstructed_tree,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _grip_git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return git(workspace / ".grip", *args)


# Review binds live in the workspace root's own `.git` (a native root: a `grip.toml` beside a root
# `.git`), under `refs/dev.synapt.grip/__reviews__/<commit>`. An alpha root is refused naming `store migrate`.
# `_grip_git` and the alpha snapshot helpers above stay on `.grip/.git`: the snapshot verbs and the
# config reader still use it.
_REVIEW_REF_PREFIX = "refs/dev.synapt.grip/__reviews__/"
_ZERO_OID = "0" * 40


def _bind_dir(workspace: Path) -> Path:
    """The repo that holds review binds: the workspace root's own `.git`. Every bind-path entry
    point has already refused a root that is not native (`_validate_bind_store`)."""
    return workspace


def _bind_git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return git(_bind_dir(workspace), *args)


def _bind_run(workspace: Path, argv: list[str], *, input: str | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=_bind_dir(workspace), input=input, capture_output=True, text=True, check=False, env=env)


def _bind_blob(workspace: Path, content: str) -> str:
    proc = _bind_run(workspace, ["git", "hash-object", "-w", "--stdin"], input=content)
    if proc.returncode != 0:
        raise RuntimeError(f"hash-object failed: {proc.stderr}")
    return proc.stdout.strip()


def _bind_mktree(workspace: Path, entries: list[str]) -> str:
    proc = _bind_run(workspace, ["git", "mktree"], input="\n".join(entries) + "\n" if entries else "")
    if proc.returncode != 0:
        raise RuntimeError(f"mktree failed: {proc.stderr}")
    return proc.stdout.strip()


def _bind_commit_tree(workspace: Path, tree_sha: str, *, parent: str | None = None, message: str = "") -> str:
    args = ["git", "commit-tree", tree_sha]
    if parent:
        args.extend(["-p", parent])
    args.extend(["-m", message or "grip snapshot"])
    proc = _bind_run(workspace, args, env=_git_env())
    if proc.returncode != 0:
        raise RuntimeError(f"commit-tree failed: {proc.stderr}")
    return proc.stdout.strip()


def _validate_bind_store(workspace: Path, *, create: bool = False) -> tuple[list[str], str] | None:
    """A native root needs nothing but its own `.git` (a bind creates a ref, never a store), after
    any review binds an older gr2 left in `.grip/.git` have moved into refs (see
    `_migrate_legacy_binds`).

    A root with NO store goes native when something is bound into it (``create=True``, the writers):
    a workspace root (it holds `.grip/workspace_spec.toml`) whose `.grip/.git` is absent, or holds no
    bind and no snapshot commit, gets `store init`'s native store and any such empty `.grip/.git` is
    moved aside, with one line saying so. Reads never create it. A `.grip/.git` with REAL alpha state
    (a snapshot or a bind commit) is refused naming `store migrate`, because that conversion can
    legitimately refuse. Any other directory keeps the `not_initialized` refusal.

    Returns (the names this call created at the root, the root's HEAD right after setup) when it set
    up a store whose `.git` did not exist before, so a writer that then refuses can remove exactly
    those and only while nothing else has been published since; else None. An adopted root (one that
    already had its own `.git`) is never cleaned: a refused first bind there keeps the `grip.toml` and
    the `/.grip/` exclude line that `store init` wrote, both harmless."""
    if not _is_native_workspace(workspace):
        legacy = _layout_grip_dir(workspace) / ".git"
        if legacy.is_dir() and _alpha_state_commits(legacy):
            raise AlphaRootRefused(
                f"{workspace} is an alpha workspace (its record is the .grip/.git snapshot store); review "
                "binds live in a native root's own .git. Run `gr2 store migrate` to convert it, then bind."
            )
        if not (legacy.exists() or (_layout_grip_dir(workspace) / "workspace_spec.toml").is_file()):
            _validate_grip_repo(workspace)  # not a workspace at all: the existing refusal
            return None
        if not create:
            raise ReviewStoreAbsent(f"No review bind is bound in {workspace}: nothing has been stored there yet.")
        before = {entry.name for entry in workspace.iterdir()}
        had_git = (workspace / ".git").exists()
        _set_up_native_store(workspace)
        created = [] if had_git else sorted({entry.name for entry in workspace.iterdir()} - before)
        head = _bind_git(workspace, "rev-parse", "--verify", "--quiet", "HEAD").stdout.strip()
        _migrate_legacy_binds(workspace)
        return (created, head)
    _migrate_legacy_binds(workspace)
    return None


def _guarded_bind(workspace: Path, created: tuple[list[str], str] | None, write) -> str:
    """Run a bind writer; when it refuses after this call set up the native store, remove what the
    setup created (only when the root's `.git` did not exist before) so a refused command leaves
    no repository behind, and say nothing about a store that was never kept."""
    import shutil
    import sys

    try:
        commit = write()
    except BaseException:
        names, setup_head = created or ([], "")
        if ".git" in names and (
            _bind_git(workspace, "for-each-ref", "--count=1", _REVIEW_REF_PREFIX).stdout.strip()
            or _bind_git(workspace, "rev-parse", "--verify", "--quiet", "HEAD").stdout.strip() != setup_head
        ):
            names = []  # another writer has published into the store since setup: it is theirs now
        for name in names:
            target = workspace / name
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists() or target.is_symlink():
                target.unlink()
        raise
    if created is not None:
        print(f"set up a native store at {workspace} (store init), so a review can be bound", file=sys.stderr)
    return commit


def _move_aside_empty_store(workspace: Path, legacy: Path) -> None:
    """A `.grip/.git` holding no bind and no snapshot is not a store: a native root moves it aside."""
    import sys

    target = _free_aside_name(workspace)
    if target is None:
        return  # every aside name is taken; leave this one rather than overwrite
    legacy.rename(target)
    print(f"moved an empty .grip/.git aside to .grip/{target.name}", file=sys.stderr)


def _alpha_state_commits(store: Path) -> list[str]:
    """Commits in an alpha `.grip/.git` that carry real state: a snapshot, a review bind or a project
    review. An empty store, or one holding only unlabelled commits, has none."""
    listed = subprocess.run(["git", "--git-dir", str(store), "rev-list", "HEAD"], capture_output=True, text=True, check=False)
    if listed.returncode != 0:
        return []
    out = []
    for commit in listed.stdout.split():
        schema = subprocess.run(["git", "--git-dir", str(store), "show", f"{commit}:.grip/schema"],
                                capture_output=True, text=True, check=False).stdout.strip()
        if schema in (_REVIEW_BIND_SCHEMA, _PROJECT_REVIEW_SCHEMA, _WORKSPACE_SCHEMA):
            out.append(commit)
    return out


def _set_up_native_store(workspace: Path) -> None:
    """`store init` for a root that has no store, so the stranger never types it before a bind."""
    # Local import: grip_cli imports this module.
    import sys

    from .grip_cli import NativeStoreRefusal, _native_store_init

    try:
        _native_store_init(workspace)
    except NativeStoreRefusal as exc:
        raise StoreSetupRefused(f"{workspace} has no store and `store init` refused to make one: {exc}") from exc


def _resolve_bound(workspace: Path, commit: str) -> str:
    """The full id of a bound review. On a native root only a commit with a
    `refs/dev.synapt.grip/__reviews__/` ref is a bind, so an ordinary workspace commit is refused as unbound
    before any tree is read; an abbreviated sha is expanded first, as git did in the old store."""
    proc = _bind_git(workspace, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")
    full = proc.stdout.strip() if proc.returncode == 0 else ""
    if not full or _bind_git(workspace, "rev-parse", "--verify", "--quiet", f"{_REVIEW_REF_PREFIX}{full}").returncode != 0:
        raise ReviewStoreAbsent(f"No review bind {commit} is bound in {workspace}.")
    return full


def _migrate_legacy_binds(workspace: Path) -> None:
    """Move the review binds an older gr2 kept in `<root>/.grip/.git` into `refs/dev.synapt.grip/__reviews__/`.

    Automatic, once, announced, and verified, because the old store is something gr2 itself made
    and knows how to move. Every id keeps its sha (recorded `gr:<sha>` ids, review markers and
    verdict text still resolve); each ref is create-only; a rerun is a no-op; and the old store is
    renamed to `.grip/legacy-store.git` only after every bind is in the root and its tree matches
    the old store's. Any failure raises with its reason BEFORE the verb does its own work. A store
    holding no review bind (alpha snapshots only, or empty) is left where it is."""
    legacy = _layout_grip_dir(workspace) / ".git"
    if not legacy.is_dir():
        return
    src = ["git", "--git-dir", str(legacy)]

    def run(argv: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)

    if not _alpha_state_commits(legacy):
        _move_aside_empty_store(workspace, legacy)
        return
    listed = run([*src, "rev-list", "HEAD"])
    if listed.returncode != 0:
        return
    binds: list[tuple[str, str]] = []
    for commit in listed.stdout.split():
        schema = run([*src, "show", f"{commit}:.grip/schema"]).stdout.strip()
        if schema in (_REVIEW_BIND_SCHEMA, _PROJECT_REVIEW_SCHEMA):
            binds.append((commit, schema))
    if not binds:
        return  # alpha snapshots only: real alpha state, left where it is

    def fail(reason: str) -> GripCorruptError:
        return GripCorruptError(f"review bind migration from .grip/.git failed, nothing was renamed: {reason}")

    target = _free_aside_name(workspace)
    if target is None:
        raise fail("every .grip/legacy-store*.git name is taken")
    fetched = run(["git", "fetch", "--quiet", "--no-tags", str(legacy), "HEAD"], cwd=workspace)
    if fetched.returncode != 0:
        raise fail(f"fetching the old store: {fetched.stderr.strip()}")
    for commit, schema in binds:
        old_tree = run([*src, "rev-parse", f"{commit}^{{tree}}"]).stdout.strip()
        new_tree = _bind_git(workspace, "rev-parse", "--verify", "--quiet", f"{commit}^{{tree}}").stdout.strip()
        if not new_tree or new_tree != old_tree:
            raise fail(f"{commit} tree is {new_tree or 'absent'} in the root, {old_tree} in the old store")
        if schema == _REVIEW_BIND_SCHEMA and not _verify_review_commit_in_store(workspace, commit)["tree_matches"]:
            raise fail(f"{commit} does not re-derive its own tree")
    moved = 0
    for commit, _schema in binds:
        ref = f"{_REVIEW_REF_PREFIX}{commit}"
        if _bind_git(workspace, "rev-parse", "--verify", "--quiet", ref).stdout.strip() == commit:
            continue
        made = _bind_git(workspace, "update-ref", ref, commit, _ZERO_OID)
        if made.returncode != 0:
            raise fail(f"{ref}: {made.stderr.strip()}")
        moved += 1
    import sys

    if len(_alpha_state_commits(legacy)) != len(binds):
        # The store also holds alpha snapshots, which the snapshot verbs still read from it: the
        # binds are copied into refs and the store stays where it is.
        if moved:
            print(f"copied {moved} review binds from .grip/.git into refs/dev.synapt.grip/__reviews__; "
                  ".grip/.git stays, it also holds alpha snapshots", file=sys.stderr)
        return
    try:
        legacy.rename(target)
    except OSError as exc:
        raise fail(f"renaming {legacy}: {exc}") from exc
    print(f"migrated {len(binds)} review binds from .grip/.git into refs/dev.synapt.grip/__reviews__", file=sys.stderr)


def _free_aside_name(workspace: Path) -> Path | None:
    """The first unused `.grip/legacy-store[-N].git`: an earlier migration or `store migrate` may
    already hold the plain name, and an aside copy is never overwritten."""
    grip_dir = _layout_grip_dir(workspace)
    for n in range(1, 100):
        candidate = grip_dir / ("legacy-store.git" if n == 1 else f"legacy-store-{n}.git")
        if not candidate.exists():
            return candidate
    return None


def _publish_bind(workspace: Path, commit: str, message: str) -> str:
    """Make a bind commit durable and findable: the commit is parentless and its ref is created
    last and create-only (old value zero), so the ref is the single commit point and a bind that
    refuses leaves only unreferenced objects."""
    ref = f"{_REVIEW_REF_PREFIX}{commit}"
    proc = _bind_git(workspace, "update-ref", ref, commit, _ZERO_OID)
    if proc.returncode != 0:
        # The commit is parentless, so the same rows bound in the same second are the SAME commit:
        # that bind already exists, which is success, not a collision.
        if _bind_git(workspace, "rev-parse", "--verify", "--quiet", ref).stdout.strip() == commit:
            return commit
        raise RuntimeError(f"update-ref failed for {ref}: {proc.stderr.strip()}")
    return commit


def _is_native_workspace(workspace: Path) -> bool:
    """A native store root: what `store init` makes, a `grip.toml` beside a root `.git`."""
    return (workspace / "grip.toml").is_file() and (workspace / ".git").exists()


_REVIEW_STORE_EXCLUDE = "/.grip/"


def exclude_grip_state(workspace: Path) -> None:
    """Make the root repo ignore `.grip/`, through the root's own `.git/info/exclude`.

    An adopted root keeps its owner's `.gitignore` (store init never edits it), so nothing there
    hides gr2's per-desk state under `.grip/`: `git status` shows it and `git add -A` would record
    local state. The exclude file is local to the clone and is the place for a tool's private
    paths. `store init` writes the line, once and only when absent; a bind writes nothing under
    `.grip/` (its record is a ref in the root's own `.git`)."""
    proc = git(workspace, "rev-parse", "--git-path", "info/exclude")
    if proc.returncode != 0 or not proc.stdout.strip():
        diagnostic = (proc.stderr or proc.stdout).strip() or "no diagnostic"
        raise GripInitError(f"cannot locate the root's info/exclude: {diagnostic}")
    exclude = Path(proc.stdout.strip())
    if not exclude.is_absolute():
        exclude = workspace / exclude
    existing = exclude.read_text() if exclude.exists() else ""
    if _REVIEW_STORE_EXCLUDE in existing.splitlines():
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        handle.write(_REVIEW_STORE_EXCLUDE + "\n")


def _validate_grip_repo(workspace: Path) -> None:
    """Verify .grip/ is a valid git repo. Raises GripInitError if not."""
    grip_dir = workspace / ".grip"
    if _is_native_workspace(workspace) and not (grip_dir / ".git").exists():
        raise ReviewStoreAbsent(
            f"No alpha snapshot store at {workspace}/.grip: a native root keeps its workspace "
            "record and its review binds in the root's own .git."
        )
    if not grip_dir.exists():
        raise GripInitError(
            f"No .grip/ directory at {workspace}. "
            f"{missing_gr2_workspace_guidance(workspace, 'Run `gr2 store init .` first.')}"
        )
    git_dir = grip_dir / ".git"
    if not git_dir.exists():
        raise GripInitError(
            f".grip/ exists but has no .git/ at {workspace}. Run grip_init to repair."
        )
    if git_dir.is_file():
        raise GripInitError(
            f".grip/.git is a file, not a directory (corrupt). "
            f"Remove {git_dir} and run grip_init to repair."
        )


def _hash_blob(workspace: Path, content: str) -> str:
    proc = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=workspace / ".grip",
        input=content,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"hash-object failed: {proc.stderr}")
    return proc.stdout.strip()


def _mktree(workspace: Path, entries: list[str]) -> str:
    tree_input = "\n".join(entries) + "\n" if entries else ""
    proc = subprocess.run(
        ["git", "mktree"],
        cwd=workspace / ".grip",
        input=tree_input,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"mktree failed: {proc.stderr}")
    return proc.stdout.strip()


def _commit_tree(
    workspace: Path, tree_sha: str, *, parent: str | None = None, message: str = ""
) -> str:
    args = ["git", "commit-tree", tree_sha]
    if parent:
        args.extend(["-p", parent])
    args.extend(["-m", message or "grip snapshot"])
    proc = subprocess.run(
        args,
        cwd=workspace / ".grip",
        capture_output=True,
        text=True,
        check=False,
        env=_git_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"commit-tree failed: {proc.stderr}")
    return proc.stdout.strip()


def _git_env() -> dict[str, str]:
    import os

    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "grip")
    env.setdefault("GIT_AUTHOR_EMAIL", "grip@synapt.dev")
    env.setdefault("GIT_COMMITTER_NAME", "grip")
    env.setdefault("GIT_COMMITTER_EMAIL", "grip@synapt.dev")
    return env


def _current_head(workspace: Path, *, strict: bool = False) -> str | None:
    """Get current HEAD of .grip/ repo.

    Returns None if no commits yet. Raises GripCorruptError if HEAD exists
    but points to invalid state (when strict=True or when HEAD file is missing/corrupt).
    """
    head_path = workspace / ".grip" / ".git" / "HEAD"
    if not head_path.exists():
        raise GripCorruptError(
            f".grip/.git/HEAD is missing at {workspace}. "
            "The grip repo may be corrupt."
        )

    proc = _grip_git(workspace, "rev-parse", "HEAD")
    if proc.returncode != 0:
        head_content = head_path.read_text().strip()
        if head_content.startswith("ref: "):
            return None
        raise GripCorruptError(
            f".grip/HEAD points to invalid ref: {head_content!r}. "
            "The grip repo may be corrupt."
        )
    return proc.stdout.strip() or None


def _repo_tree_entries(workspace: Path, name: str, repo_path: Path) -> str:
    """Build a tree for one repo and return an mktree entry line."""
    from gr2.python_cli.gitops import repo_dirty

    if not repo_path.exists():
        # a missing path is nothing to read: record the empty state (no head,
        # no branch, no remote, not dirty) without invoking git, which would
        # raise on the first call; the index's repo_states record says the same
        dirty_sha = _hash_blob(workspace, "false")
        tree_sha = _mktree(workspace, [f"100644 blob {dirty_sha}\tdirty"])
        return f"040000 tree {tree_sha}\t{name}"

    blobs: list[str] = []

    head = git(repo_path, "rev-parse", "HEAD")
    if head.returncode == 0 and head.stdout.strip():
        sha = _hash_blob(workspace, head.stdout.strip())
        blobs.append(f"100644 blob {sha}\tcommit")

    branch = git(repo_path, "branch", "--show-current")
    if branch.returncode == 0 and branch.stdout.strip():
        sha = _hash_blob(workspace, branch.stdout.strip())
        blobs.append(f"100644 blob {sha}\tbranch")

    remote = git(repo_path, "config", "--get", "remote.origin.url")
    if remote.returncode == 0 and remote.stdout.strip():
        sha = _hash_blob(workspace, remote.stdout.strip())
        blobs.append(f"100644 blob {sha}\tremote")

    is_dirty = repo_dirty(repo_path)
    dirty_sha = _hash_blob(workspace, "true" if is_dirty else "false")
    blobs.append(f"100644 blob {dirty_sha}\tdirty")

    tree_sha = _mktree(workspace, blobs)
    return f"040000 tree {tree_sha}\t{name}"


def _changeset_tree(
    workspace: Path,
    *,
    changeset_type: str = "",
    sprint: str = "",
) -> str | None:
    """Build the .grip/ changeset metadata subtree. Returns tree SHA or None."""
    blobs: list[str] = []

    if changeset_type:
        sha = _hash_blob(workspace, changeset_type)
        blobs.append(f"100644 blob {sha}\ttype")

    if sprint:
        sha = _hash_blob(workspace, sprint)
        blobs.append(f"100644 blob {sha}\tsprint")

    if not blobs:
        return None
    return _mktree(workspace, blobs)


def _config_overlay_tree(workspace: Path, overlay_dir: Path) -> str | None:
    """Build a config/ tree from overlay JSON files for inclusion in grip commit."""
    entries: list[str] = []

    for f in sorted(overlay_dir.glob("*.json")):
        content = f.read_text()
        sha = _hash_blob(workspace, content)
        entries.append(f"100644 blob {sha}\t{f.name}")

    prompts_dir = overlay_dir / "prompts"
    if prompts_dir.is_dir():
        prompt_entries: list[str] = []
        for pf in sorted(prompts_dir.glob("*.json")):
            content = pf.read_text()
            sha = _hash_blob(workspace, content)
            prompt_entries.append(f"100644 blob {sha}\t{pf.name}")
        if prompt_entries:
            prompts_tree = _mktree(workspace, prompt_entries)
            entries.append(f"040000 tree {prompts_tree}\tprompts")

    if not entries:
        return None
    return _mktree(workspace, entries)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def grip_init(workspace: Path) -> Path:
    """Initialize the .grip/ git repo. Idempotent."""
    grip_dir = workspace / ".grip"
    if not grip_dir.exists():
        grip_dir.mkdir(parents=True)
    git_dir = grip_dir / ".git"
    if git_dir.is_file():
        raise GripInitError(
            f".grip/.git is a file, not a directory (corrupt). "
            f"Remove {git_dir} and run grip_init again."
        )
    if not git_dir.exists():
        _require_git_success(git(grip_dir, "init"), "git init")
        _require_git_success(
            git(grip_dir, "config", "user.email", "grip@synapt.dev"),
            "git config user.email",
        )
        _require_git_success(
            git(grip_dir, "config", "user.name", "grip"),
            "git config user.name",
        )
    probe = git(grip_dir, "rev-parse", "--is-inside-work-tree")
    _require_git_success(probe, "git rev-parse --is-inside-work-tree")
    if probe.stdout.strip() != "true":
        raise GripInitError(".grip is not a valid git object store after initialization")
    return grip_dir


def _require_git_success(proc: subprocess.CompletedProcess[str], action: str) -> None:
    if proc.returncode == 0:
        return
    diagnostic = (proc.stderr or proc.stdout).strip() or "no diagnostic"
    raise GripInitError(f"{action} failed: {diagnostic}")


def grip_snapshot(
    workspace: Path,
    repos: dict[str, Path],
    *,
    changeset_type: str = "",
    sprint: str = "",
    message: str = "",
    overlay_dir: Path | None = None,
) -> str:
    """Create a grip commit from current repo states. Returns commit SHA."""
    _validate_grip_repo(workspace)
    repo_entries: list[str] = []
    for name in sorted(repos):
        entry = _repo_tree_entries(workspace, name, repos[name])
        repo_entries.append(entry)

    repos_tree = _mktree(workspace, repo_entries)
    root_entries = [f"040000 tree {repos_tree}\trepos"]

    cs_tree = _changeset_tree(workspace, changeset_type=changeset_type, sprint=sprint)
    if cs_tree:
        root_entries.append(f"040000 tree {cs_tree}\t.grip")

    if overlay_dir and overlay_dir.is_dir():
        config_tree = _config_overlay_tree(workspace, overlay_dir)
        if config_tree:
            root_entries.append(f"040000 tree {config_tree}\tconfig")

    root_tree = _mktree(workspace, root_entries)

    parent = _current_head(workspace)
    commit_msg = message or f"grip snapshot ({changeset_type})" if changeset_type else message or "grip snapshot"
    commit_sha = _commit_tree(workspace, root_tree, parent=parent, message=commit_msg)

    _grip_git(workspace, "update-ref", "HEAD", commit_sha)

    return commit_sha


def grip_log(workspace: Path, *, max_count: int = 10) -> list[GripCommitInfo]:
    """List grip commit history, most recent first."""
    _validate_grip_repo(workspace)
    head = _current_head(workspace)
    if not head:
        return []

    proc = _grip_git(
        workspace,
        "log",
        f"--max-count={max_count}",
        "--format=%H%n%s%n%aI%n---",
        "HEAD",
    )
    if proc.returncode != 0:
        return []

    entries: list[GripCommitInfo] = []
    chunks = proc.stdout.strip().split("---\n")
    for chunk in chunks:
        chunk = chunk.strip().rstrip("---").strip()
        if not chunk:
            continue
        lines = chunk.splitlines()
        if len(lines) < 2:
            continue
        sha = lines[0].strip()
        msg = lines[1].strip()
        ts = lines[2].strip() if len(lines) > 2 else ""

        repo_names = _read_repo_names(workspace, sha)
        entries.append(GripCommitInfo(sha=sha, message=msg, repos=repo_names, timestamp=ts))

    return entries


def _read_repo_names(workspace: Path, commit_sha: str) -> list[str]:
    proc = _grip_git(workspace, "ls-tree", f"{commit_sha}:repos")
    if proc.returncode != 0:
        return []
    return [
        line.split("\t")[-1]
        for line in proc.stdout.strip().splitlines()
        if line.strip()
    ]


def grip_diff(workspace: Path, ref_a: str, ref_b: str) -> GripDiff:
    """Compare two grip commits and return changed/added/removed repos."""
    _validate_grip_repo(workspace)
    repos_a = _read_repo_state(workspace, ref_a)
    repos_b = _read_repo_state(workspace, ref_b)

    result = GripDiff()

    all_names = set(repos_a.keys()) | set(repos_b.keys())
    for name in sorted(all_names):
        if name in repos_a and name not in repos_b:
            result.removed.append(name)
        elif name not in repos_a and name in repos_b:
            result.added.append(name)
        else:
            old_commit = repos_a[name].get("commit", "")
            new_commit = repos_b[name].get("commit", "")
            if old_commit != new_commit:
                result.changed[name] = {
                    "old_commit": old_commit,
                    "new_commit": new_commit,
                }

    return result


def _read_repo_state(workspace: Path, ref: str, *, bind: bool = False) -> dict[str, dict[str, str]]:
    """Read all repo states from a grip commit. ``bind=True`` reads the review store (the root's
    own `.git` on a native root); the default keeps reading the alpha `.grip/.git` snapshot store."""
    _g = _bind_git if bind else _grip_git
    proc = _g(workspace, "ls-tree", f"{ref}:repos")
    if proc.returncode != 0:
        return {}

    repos: dict[str, dict[str, str]] = {}
    for line in proc.stdout.strip().splitlines():
        if not line.strip():
            continue
        name = line.split("\t")[-1]
        state: dict[str, str] = {}
        fields = _g(workspace, "ls-tree", f"{ref}:repos/{name}")
        if fields.returncode == 0:
            for fline in fields.stdout.strip().splitlines():
                if not fline.strip():
                    continue
                fname = fline.split("\t")[-1]
                blob = _g(workspace, "show", f"{ref}:repos/{name}/{fname}")
                if blob.returncode == 0:
                    state[fname] = blob.stdout.strip()
        repos[name] = state

    return repos


def grip_checkout(workspace: Path, ref: str) -> dict[str, str]:
    """Read a grip commit and checkout matching commits in workspace repos.

    Returns dict mapping repo name to commit SHA.
    """
    _validate_grip_repo(workspace)

    # Verify the ref resolves to a valid object
    verify = _grip_git(workspace, "cat-file", "-t", ref)
    if verify.returncode != 0:
        raise GripCorruptError(
            f"Ref '{ref}' does not resolve to a valid object in .grip/ repo."
        )

    repo_states = _read_repo_state(workspace, ref)
    result: dict[str, str] = {}

    for name, state in sorted(repo_states.items()):
        commit_sha = state.get("commit", "")
        if not commit_sha:
            continue
        result[name] = commit_sha

        repo_path = workspace / name
        if repo_path.is_dir():
            git(repo_path, "checkout", commit_sha)

    return result
