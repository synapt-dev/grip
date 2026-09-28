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
        workspace / ".grip" / "state" / "lanes" / owner / lane / "review" / f"{key}.json",
        legacy_review_record_path(lane_repo_root),
    )


def read_review_record(paths: ReviewRecordPaths, *, notice: Callable[[str], None] = print) -> dict | None:
    for path, legacy in ((paths.current, False), (paths.legacy, True)):
        if not path.is_file():
            continue
        try:
            result = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if legacy:
            notice(f"legacy review record read from {path}; re-open the review to migrate it")
        return result
    return None


def write_review_record(paths: ReviewRecordPaths, record: dict) -> Path:
    paths.current.parent.mkdir(parents=True, exist_ok=True)
    paths.current.write_text(json.dumps(record, indent=2) + "\n")
    return paths.current


def lane_paths_for_repo(repo: Path | str) -> ReviewRecordPaths | None:
    """Locate only a repo structurally inside a materialized lane, never by scan."""
    for candidate in (Path(repo).resolve(), *Path(repo).resolve().parents):
        if candidate.parent.name != "repos":
            continue
        lane_dir, owner_dir = candidate.parent.parent, candidate.parent.parent.parent
        lanes_dir, state_dir, grip_dir = owner_dir.parent, owner_dir.parent.parent, owner_dir.parent.parent.parent
        if lanes_dir.name == "lanes" and state_dir.name == "state" and grip_dir.name == ".grip":
            return review_record_paths(grip_dir.parent, owner_dir.name, lane_dir.name, candidate.name, candidate)
    return None
