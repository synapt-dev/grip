"""Minimal M1 project-tier composition over the existing review clone seam."""
from __future__ import annotations

import argparse
import dataclasses
import re
import tomllib
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

from gr2.prototypes import lane_workspace_prototype as lanes
from . import grip, review, spec_apply
from .gitops import git

_SHA40 = re.compile(r"\A[0-9a-f]{40}\Z")


@dataclasses.dataclass(frozen=True)
class ProjectReviewPin:
    key: str
    repo: str
    path: str
    base: str
    head: str


@dataclasses.dataclass(frozen=True)
class ProjectReviewSpec:
    schema: str
    grip_commit: str
    pins: tuple[ProjectReviewPin, ...]


@dataclasses.dataclass(frozen=True)
class ProjectReviewFailure:
    key: str
    reason: str


@dataclasses.dataclass(frozen=True)
class ProjectReviewOutcome:
    status: Literal["opened", "refused", "partial"]
    grip_commit: str
    observed: tuple[review.ReviewRecord, ...]
    failures: tuple[ProjectReviewFailure, ...]
    review_root: Path | None
    current_lane_changed: bool


def make_spec(
    workspace: Path, pins: list[ProjectReviewPin], ranges: dict[str, str] | None = None,
    committers: dict[str, str] | None = None,
) -> ProjectReviewSpec:
    ordered = tuple(sorted((_canonical_pin(pin) for pin in pins), key=lambda pin: pin.key))
    if not ordered or len({pin.key for pin in ordered}) != len(ordered):
        raise ValueError("project review pins must be non-empty with unique keys")
    commit = grip.create_project_review_commit(
        workspace, [{**dataclasses.asdict(pin), "repo": pin.repo} for pin in ordered],
        ranges=ranges, committers=committers,
    )
    return ProjectReviewSpec("gr2-project-review/v1", commit, ordered)


def pins_from_lane(workspace_root: Path, owner_unit: str, lane_name: str) -> list[ProjectReviewPin]:
    """Build project-review pins for a materialized lane, reading each repo's base
    from the RECORDED fork base, never from HEAD^.

    A review is measured from the point the lane forked from its integration
    branch; that coordinate is recorded at lane create and read here through the
    same resolver the workspace snapshot uses. A lane with no recorded fork base is
    refused by that resolver (unknown, never HEAD^), so a review can never be bound
    against a base the lane did not actually fork from.
    """
    from . import workspace_snapshot as ws_snap

    rows = ws_snap.resolve_lane_repos(Path(workspace_root).resolve(), owner_unit, lane_name)
    return [
        ProjectReviewPin(key=r["key"], repo=r["remote"], path=r["path"], base=r["base"], head=r["commit"])
        for r in rows
    ]


def _normalized_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or str(path) in {".", ""}:
        raise ValueError(f"invalid project review path: {value!r}")
    return path.as_posix()


def _canonical_pin(pin: ProjectReviewPin) -> ProjectReviewPin:
    if not pin.key or any(ch in pin.key for ch in "/\\") or not pin.repo or not _SHA40.match(pin.base) or not _SHA40.match(pin.head):
        raise ValueError(f"invalid project review pin: {pin.key}")
    return dataclasses.replace(pin, path=_normalized_path(pin.path))


def _review_path_component(value: str, field: str) -> str:
    """Validate names before they become a clone destination or lane path."""
    if not value or value in {".", ".."}:
        raise ValueError(f"invalid {field}: {value!r}")
    windows = PureWindowsPath(value)
    if (
        "/" in value
        or "\\" in value
        or ":" in value
        or windows.drive
        or windows.root
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    ):
        raise ValueError(f"invalid {field}: {value!r}")
    return value


def _canonical_repo_identity(value: str, *, allow_local: bool) -> str:
    """Canonicalize a pin/spec identity without treating a clone path as truth.

    Local paths are test-only transport. When one names a checkout, its origin
    supplies the repository identity just as production HTTPS/SSH transport
    does. A bare local path remains marked local for isolated fixtures.

    ``allow_local`` is threaded to ``canonical_source_identity`` on EVERY path,
    including the two ``local:`` sub-cases, so the literal ``local:`` prefix
    cannot bypass the FILESYSTEM-identity refusal: with ``allow_local=False`` a
    filesystem identity is refused with the same ``ReviewError`` as a bare
    non-GitHub origin, because the refusal is delegated to
    ``canonical_source_identity`` rather than re-implemented here. A ``local:``
    checkout whose origin is a portable ``https://github.com/<owner>/<repo>``
    still canonicalizes to that GitHub identity regardless of the flag — the
    refusal is of a non-portable filesystem identity, not of the prefix itself.
    """
    if value.startswith("local:"):
        local = Path(value.removeprefix("local:"))
        origin = review.gitops.remote_origin_url(local) if local.is_dir() else None
        if origin:
            return review.canonical_source_identity(origin, allow_local=allow_local)
        return review.canonical_source_identity(str(local), allow_local=allow_local)
    return review.canonical_source_identity(value, allow_local=allow_local)


def _validate_workspace_repository_boundary(
    *, workspace: Path, pins: tuple[ProjectReviewPin, ...], sources: dict[str, tuple[Path, str]], allow_local: bool
) -> ProjectReviewFailure | None:
    """Bind every review pin and ephemeral source to the compiled workspace.

    This is deliberately before pin-object checks, clone destinations, or lane
    transition. A project review may only materialize repositories the compiled
    workspace itself authorizes.
    """
    workspace_doc, failure = _load_workspace_boundary_doc(workspace)
    if failure is not None:
        return failure
    entries: dict[str, str] = {}
    for row in workspace_doc.get("repos", []):
        if not isinstance(row, dict):
            return ProjectReviewFailure("workspace_spec", "compiled workspace repo entry is not a mapping")
        key = str(row.get("name", ""))
        url = str(row.get("url", ""))
        try:
            entries[key] = _canonical_repo_identity(url, allow_local=allow_local)
        except Exception as exc:
            return ProjectReviewFailure("workspace_spec", f"invalid compiled repository {key!r}: {exc}")
    for pin in pins:
        expected = entries.get(pin.key)
        if expected is None:
            return ProjectReviewFailure(pin.key, f"unknown workspace repository key {pin.key!r}")
        try:
            pin_identity = _canonical_repo_identity(pin.repo, allow_local=allow_local)
        except Exception as exc:
            return ProjectReviewFailure(pin.key, f"invalid pin repository identity: {exc}")
        if pin_identity != expected:
            return ProjectReviewFailure(
                pin.key,
                f"workspace repository identity mismatch: pin {pin_identity!r}, compiled {expected!r}",
            )
        source_branch = sources.get(pin.key)
        if source_branch is None:
            continue
        source, _branch = source_branch
        origin = review.gitops.remote_origin_url(source)
        if not origin:
            return ProjectReviewFailure(pin.key, f"selected source {source} has no origin for identity validation")
        try:
            source_identity = _canonical_repo_identity(origin, allow_local=allow_local)
        except Exception as exc:
            return ProjectReviewFailure(pin.key, f"invalid selected source origin: {exc}")
        if source_identity != pin_identity:
            return ProjectReviewFailure(
                pin.key,
                f"selected source identity mismatch: source {source_identity!r}, pin {pin_identity!r}",
            )
    return None


def _load_workspace_boundary_doc(workspace: Path) -> tuple[dict[str, object] | None, ProjectReviewFailure | None]:
    """Keep absent and malformed authority in the review outcome channel."""
    try:
        return spec_apply.load_workspace_spec_doc(workspace), None
    except (SystemExit, tomllib.TOMLDecodeError) as exc:
        return None, ProjectReviewFailure("workspace_spec", str(exc))


def _stamp_lane_kind(workspace: Path, owner_unit: str, lane_name: str, kind: str) -> None:
    """Rewrite the lane's ``lane_kind`` in place. create_lane writes the default
    ``materialized``; a review lane overrides it so require_current_lane reports the
    review kind to every mutating verb."""
    path = lanes.lane_file(workspace, owner_unit, lane_name)
    text = path.read_text()
    new = re.sub(r'(?m)^lane_kind\s*=\s*"[^"]*"\s*$', f'lane_kind = "{kind}"', text)
    if 'lane_kind' not in new:
        new = new.rstrip("\n") + f'\nlane_kind = "{kind}"\n'
    path.write_text(new)


def open_project_review(*, workspace: Path, owner_unit: str, lane_name: str, spec: ProjectReviewSpec, sources: dict[str, tuple[Path, str]], allow_local: bool = False, ephemeral: bool = False, materialize_heads: dict[str, str] | None = None) -> ProjectReviewOutcome:
    """Preflight every immutable pin, then materialize all members before enter.

    ``ephemeral`` materializes each member as a blobless+sparse review-ephemeral
    lane from the persistent mirror (``sources[key]`` is the mirror), never through
    the work-lane clone seam.

    ``materialize_heads`` (key -> sha) OVERRIDES which head each member is
    materialized at, WITHOUT changing what the gr commit is validated against. It is
    the carried-range reconstruction case: the commit pins the original (pre-push)
    head, but ``git am`` re-stamps the committer so the reconstructed head is a
    DIFFERENT sha with the SAME tree; the commit-binding check stays on the pinned
    head while materialization uses the reconstructed head that actually exists in
    the topped-up mirror. Absent a key, the pinned head is used (the ordinary case)."""
    materialize_heads = materialize_heads or {}
    if spec.schema != "gr2-project-review/v1":
        return ProjectReviewOutcome("refused", spec.grip_commit, (), (ProjectReviewFailure("spec", "unsupported schema"),), None, False)
    try:
        owner_unit = _review_path_component(owner_unit, "owner unit")
        lane_name = _review_path_component(lane_name, "lane name")
    except ValueError as exc:
        return ProjectReviewOutcome("refused", spec.grip_commit, (), (ProjectReviewFailure("review_root", str(exc)),), None, False)
    # A cross-repo project review requires a MATERIALIZED lane whose rows
    # reconstruct together from carried ranges. A bound lane is a single-repo
    # label on the author's own worktree — its bytes live in a tree the author
    # keeps editing, so it cannot give the exact-reconstruction guarantee across
    # rows. Refuse it here with a specific message rather than letting the later
    # create_lane emit a generic "refusing to replace" (bound lanes are
    # single-repo `pr` only).
    lane_path = lanes.lane_file(workspace, owner_unit, lane_name)
    if lane_path.exists():
        try:
            existing_kind = tomllib.loads(lane_path.read_text()).get("lane_kind")
        except (OSError, tomllib.TOMLDecodeError):
            existing_kind = None
        if existing_kind == "bound":
            return ProjectReviewOutcome(
                "refused", spec.grip_commit, (),
                (ProjectReviewFailure(
                    lane_name,
                    f"lane {owner_unit}/{lane_name} is a BOUND single-repo lane; a project review "
                    "requires a materialized lane whose rows reconstruct together. Use `gr2 pr` for "
                    "this bound lane's single-repo review, or open the project review on a "
                    "materialized lane.",
                ),),
                None, False,
            )
    try:
        canonical_pins = tuple(_canonical_pin(pin) for pin in spec.pins)
    except ValueError as exc:
        return ProjectReviewOutcome("refused", spec.grip_commit, (), (ProjectReviewFailure("spec", str(exc)),), None, False)
    decoded = grip.read_project_review_commit(workspace, spec.grip_commit)
    if len(decoded) != len(canonical_pins):
        return ProjectReviewOutcome("refused", spec.grip_commit, (), (ProjectReviewFailure("spec", "gr commit member count mismatch"),), None, False)
    for row, pin in zip(decoded, canonical_pins):
        for field, expected, observed in (("key", pin.key, row["key"]), ("repo", pin.repo, row["repo"]), ("path", pin.path, _normalized_path(row["path"])), ("base", pin.base, row["base"]), ("head", pin.head, row["head"])):
            if expected != observed:
                return ProjectReviewOutcome("refused", spec.grip_commit, (), (ProjectReviewFailure(pin.key, f"gr commit mismatch for {field}: expected {expected!r}, observed {observed!r}"),), None, False)
    boundary_failure = _validate_workspace_repository_boundary(
        workspace=workspace, pins=canonical_pins, sources=sources, allow_local=allow_local
    )
    if boundary_failure is not None:
        return ProjectReviewOutcome("refused", spec.grip_commit, (), (boundary_failure,), None, False)
    for pin in canonical_pins:
        source_branch = sources.get(pin.key)
        if source_branch is None:
            return ProjectReviewOutcome("refused", spec.grip_commit, (), (ProjectReviewFailure(pin.key, "missing source transport"),), None, False)
        source, _branch = source_branch
        materialize_head = materialize_heads.get(pin.key, pin.head)
        for label, sha in (("base", pin.base), ("head", materialize_head)):
            if git(source, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
                return ProjectReviewOutcome("refused", spec.grip_commit, (), (ProjectReviewFailure(pin.key, f"missing {label} pin {sha}"),), None, False)
    review_root = workspace / "reviews" / owner_unit / lane_name
    observed: list[review.ReviewRecord] = []
    for pin in canonical_pins:
        source, branch = sources[pin.key]
        repo_name = Path(source).name.removesuffix(".git")
        materialize_head = materialize_heads.get(pin.key, pin.head)
        try:
            record = review.open_review_lane(source_repo_root=source, review_branch=branch, expected_head_sha=materialize_head, base_sha=pin.base, lane_repo_root=review_root / "repos" / pin.key, workspace_root=workspace, allow_local=allow_local, ephemeral=ephemeral, repo_name=repo_name, echo=lambda _line: None)
        except Exception as exc:
            return ProjectReviewOutcome("partial", spec.grip_commit, tuple(observed), (ProjectReviewFailure(pin.key, str(exc)),), review_root, False)
        observed.append(record)
    try:
        lanes.create_lane(argparse.Namespace(workspace_root=workspace, owner_unit=owner_unit, lane_name=lane_name, type="review", repos=",".join(pin.key for pin in canonical_pins), branch="main", source="project-review", default_commands=[]))
        if ephemeral:
            # Stamp the lane kind so every mutating verb (commit/push/bind) can
            # refuse this lane naming the kind: a review lane never becomes a work lane.
            _stamp_lane_kind(workspace, owner_unit, lane_name, "review-ephemeral")
        lanes.enter_lane(argparse.Namespace(workspace_root=workspace, owner_unit=owner_unit, lane_name=lane_name, actor="project-review", notify_channel=False, recall=False))
    except Exception as exc:
        return ProjectReviewOutcome("partial", spec.grip_commit, tuple(observed), (ProjectReviewFailure("transition", str(exc)),), review_root, False)
    return ProjectReviewOutcome("opened", spec.grip_commit, tuple(observed), (), review_root, True)


def outcome_payload(outcome: ProjectReviewOutcome) -> dict[str, object]:
    return {"status": outcome.status, "grip_commit": outcome.grip_commit,
            "observed": [record.to_dict() for record in outcome.observed],
            "failures": [dataclasses.asdict(failure) for failure in outcome.failures],
            "review_root": str(outcome.review_root) if outcome.review_root else None,
            "current_lane_changed": outcome.current_lane_changed}
