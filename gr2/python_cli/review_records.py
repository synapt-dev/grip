"""Canonical review-record locations, outside member ``.git`` directories."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class ReviewRecordLocationError(ValueError):
    pass


@dataclass(frozen=True)
class ReviewRecordPaths:
    current: Path
    legacy: Path


def legacy_review_record_path(lane_repo_root: Path | str) -> Path:
    return Path(lane_repo_root) / ".git" / "grip-review.json"


def review_record_pointer_path(lane_repo_root: Path | str) -> Path:
    """The local discovery pointer, never a second copy of receipt content."""
    return Path(lane_repo_root) / ".git" / "grip-review.pointer"


def _component(value: str | None, label: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ReviewRecordLocationError(
            f"review record needs a safe {label}; refusing to guess its workspace location"
        )
    return value


def review_record_paths(workspace_root: Path | str, owner_unit: str | None,
                        lane_name: str | None, member: str | None,
                        lane_repo_root: Path | str) -> ReviewRecordPaths:
    """The canonical path and the one-release member-git fallback."""
    owner = _component(owner_unit, "owner unit")
    lane = _component(lane_name, "lane name")
    key = _component(member, "member")
    workspace = Path(workspace_root).resolve()
    return ReviewRecordPaths(
        workspace / ".grip" / "state" / "reviews" / owner / lane / f"{key}.json",
        legacy_review_record_path(lane_repo_root),
    )


def read_review_record(paths: ReviewRecordPaths, *, notice: Callable[[str], None] = print) -> dict | None:
    result = read_review_record_at(paths, notice=notice)
    return result[0] if result else None


def read_review_records_for_guard(paths: ReviewRecordPaths) -> tuple[dict, ...]:
    """Read every receipt present so a disposable mark cannot be masked.

    ``read_review_record_at`` deliberately chooses one path for identity and
    cleanup. The commit and push guard has a different question: whether any
    extant receipt marks this checkout disposable.
    """
    records: list[dict] = []
    for path in (paths.current, paths.legacy):
        if not path.is_file():
            continue
        try:
            records.append(json.loads(path.read_text()))
        except (OSError, ValueError) as exc:
            raise ReviewRecordLocationError(f"review receipt cannot be read: {path}") from exc
    return tuple(records)


def read_review_record_at(paths: ReviewRecordPaths, *, notice: Callable[[str], None] = print) -> tuple[dict, Path] | None:
    """Read a receipt and return the exact path that supplied it for cleanup."""
    for path, legacy in ((paths.legacy, True), (paths.current, False)):
        if not path.is_file():
            continue
        try:
            result = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if legacy:
            notice(f"legacy review record read from {path}; re-open the review to migrate it")
        return result, path
    return None


def write_review_record(paths: ReviewRecordPaths, record: dict) -> Path:
    paths.current.parent.mkdir(parents=True, exist_ok=True)
    paths.current.write_text(json.dumps(record, indent=2) + "\n")
    # A project review can materialize outside `.grip/state/reviews`; commit and
    # push run from that member checkout, so leave one coordinate pointer there.
    # It deliberately contains no receipt fields.
    review_record_pointer_path(paths.legacy.parent.parent).write_text(str(paths.current) + "\n")
    return paths.current


def lane_paths_for_repo(repo: Path | str) -> ReviewRecordPaths | None:
    """Locate only a repo structurally inside a materialized lane, never by scan."""
    repo_path = Path(repo).resolve()
    legacy = legacy_review_record_path(repo_path)
    pointer = review_record_pointer_path(repo_path)
    if pointer.is_file():
        try:
            target = Path(pointer.read_text().strip())
            if not target.is_absolute() or ".." in target.parts:
                raise ReviewRecordLocationError("review pointer contains an unsafe coordinate")
            resolved_target = target.resolve()
            parts = resolved_target.parts
            marker = (".grip", "state", "reviews")
            if not any(parts[i:i + 3] == marker for i in range(len(parts) - 2)):
                raise ReviewRecordLocationError("review pointer is not a canonical workspace coordinate")
            index = next(i for i in range(len(parts) - 2) if parts[i:i + 3] == marker)
            if len(parts) != index + 6:
                raise ReviewRecordLocationError("review pointer is not a canonical review receipt")
            owner, lane, filename = parts[index + 3:index + 6]
            if not filename.endswith(".json"):
                raise ReviewRecordLocationError("review pointer is not a canonical review receipt")
            _component(owner, "owner unit"); _component(lane, "lane name")
            _component(filename[:-5], "member")
            return ReviewRecordPaths(resolved_target, legacy)
        except (OSError, ValueError):
            raise ReviewRecordLocationError("review pointer cannot be read safely")
    # A one-release legacy receipt is enough only where no pointer supplies a
    # canonical coordinate. When both exist, the safety guard receives both.
    if legacy.is_file():
        return ReviewRecordPaths(legacy, legacy)
    # Compatibility for project-review lanes created before the pointer.
    if repo_path.parent.name == "repos":
        lane_dir = repo_path.parent.parent
        owner_dir = lane_dir.parent
        reviews_dir = owner_dir.parent
        if reviews_dir.name == "reviews":
            return review_record_paths(reviews_dir.parent, owner_dir.name, lane_dir.name, repo_path.name, repo_path)
    for candidate in (repo_path, *repo_path.parents):
        if candidate.parent.name != "repos":
            continue
        lane_dir, owner_dir = candidate.parent.parent, candidate.parent.parent.parent
        lanes_dir, state_dir, grip_dir = owner_dir.parent, owner_dir.parent.parent, owner_dir.parent.parent.parent
        if lanes_dir.name == "lanes" and state_dir.name == "state" and grip_dir.name == ".grip":
            return review_record_paths(grip_dir.parent, owner_dir.name, lane_dir.name, candidate.name, candidate)
    return None
