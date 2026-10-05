"""Canonical review-record locations, outside member ``.git`` directories."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class ReviewRecordLocationError(ValueError):
    pass


@dataclass(frozen=True)
class ReviewRecordPaths:
    current: Path
    legacy: Path
    repo_root: Path | None = None

    @property
    def closing(self) -> Path:
        """Incomplete disposal evidence, never an active review identity."""
        return self.current.with_name(self.current.name + ".closing.json")


def read_close_recovery(paths: ReviewRecordPaths, target: Path, managed_root: Path) -> dict | None:
    if not paths.closing.exists() and not paths.closing.is_symlink():
        return None
    try:
        if paths.closing.is_symlink():
            raise ValueError("symlink recovery record")
        doc = json.loads(paths.closing.read_text())
        if set(doc) != {"version", "target", "managed_root", "target_identity", "git_identity", "selected", "receipts"}:
            raise ValueError("invalid recovery fields")
        if doc["version"] != 1 or doc["target"] != str(target) or doc["managed_root"] != str(managed_root):
            raise ValueError("recovery target or managed root mismatch")
        allowed = {str(paths.current), str(paths.legacy)}
        if not isinstance(doc["receipts"], dict) or not doc["receipts"] or not set(doc["receipts"]) <= allowed:
            raise ValueError("invalid retained receipt paths")
        if doc["selected"] not in doc["receipts"]:
            raise ValueError("missing selected receipt")
        for value in doc["receipts"].values():
            bytes.fromhex(value)
        for key in ("target_identity", "git_identity"):
            if not isinstance(doc[key], list) or len(doc[key]) != 2 or not all(type(v) is int for v in doc[key]):
                raise ValueError("invalid physical identity")
        return doc
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise ReviewRecordLocationError(f"close recovery cannot be read safely: {paths.closing}: {exc}") from exc


def stage_close_recovery(paths: ReviewRecordPaths, target: Path, managed_root: Path, selected: Path) -> dict:
    """Keep exact current and legacy bytes outside the clone before disposal."""
    if paths.closing.exists() or paths.closing.is_symlink():
        raise ReviewRecordLocationError(f"close already pending at {paths.closing}")
    if target in paths.closing.resolve().parents:
        raise ReviewRecordLocationError("close recovery must be outside the deletion target")
    receipts = {}
    for path in dict.fromkeys((paths.current, paths.legacy)):
        if path.is_symlink():
            raise ReviewRecordLocationError("refusing symlink receipt during close")
        if path.is_file():
            receipts[str(path)] = path.read_bytes().hex()
    target_stat, git_stat = target.stat(), (target / ".git").stat()
    doc = {"version": 1, "target": str(target), "managed_root": str(managed_root),
           "target_identity": [target_stat.st_dev, target_stat.st_ino],
           "git_identity": [git_stat.st_dev, git_stat.st_ino],
           "selected": str(selected), "receipts": receipts}
    paths.closing.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=paths.closing.parent, prefix=".closing-", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write((json.dumps(doc, indent=2) + "\n").encode())
    try:
        os.replace(temporary, paths.closing)
    finally:
        temporary.unlink(missing_ok=True)
    return doc


def finish_close_recovery(paths: ReviewRecordPaths, target: Path, doc: dict) -> None:
    if target.exists() or target.is_symlink():
        raise ReviewRecordLocationError("cannot finish close while target remains")
    for name, retained in doc["receipts"].items():
        path = Path(name)
        if path.is_symlink():
            raise ReviewRecordLocationError("receipt changed to symlink during close")
        if path.exists():
            if path.read_bytes() != bytes.fromhex(retained):
                raise ReviewRecordLocationError("receipt changed during close; retaining recovery")
            path.unlink()
    paths.closing.unlink()


def worktree_git_path(repo_root: Path | str, name: str) -> Path:
    """Resolve metadata in this worktree, including checkouts whose .git is a file."""
    repo = Path(repo_root).resolve()
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-path", name],
        text=True, capture_output=True,
    )
    if proc.returncode or not proc.stdout.strip():
        raise ReviewRecordLocationError(f"cannot resolve worktree Git metadata for {repo}")
    path = Path(proc.stdout.strip())
    absolute = path if path.is_absolute() else repo / path
    return absolute.parent.resolve() / absolute.name


def legacy_review_record_path(lane_repo_root: Path | str) -> Path:
    # Review-open computes its coordinates before creating the ordinary clone.
    # No lookup is possible yet. Publication resolves again once Git exists.
    if not Path(lane_repo_root).exists():
        return Path(lane_repo_root).resolve() / ".git" / "grip-review.json"
    return worktree_git_path(lane_repo_root, "grip-review.json")


def review_record_pointer_path(lane_repo_root: Path | str) -> Path:
    """The local discovery pointer, never a second copy of receipt content."""
    return worktree_git_path(lane_repo_root, "grip-review.pointer")


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
        Path(lane_repo_root).resolve(),
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
    if paths.closing.exists() or paths.closing.is_symlink():
        raise ReviewRecordLocationError(f"review close is incomplete; recovery at {paths.closing}")
    records: list[dict] = []
    for path in (paths.current, paths.legacy):
        if not path.is_file():
            continue
        try:
            record = json.loads(path.read_text())
            if not isinstance(record, dict):
                raise ValueError("receipt must be an object")
            records.append(record)
        except (OSError, ValueError) as exc:
            raise ReviewRecordLocationError(f"review receipt cannot be read: {path}") from exc
    return tuple(records)


def read_review_record_at(paths: ReviewRecordPaths, *, notice: Callable[[str], None] = print) -> tuple[dict, Path] | None:
    """Read a receipt and return the exact path that supplied it for cleanup."""
    if paths.closing.exists() or paths.closing.is_symlink():
        return None
    for path, legacy in ((paths.current, False), (paths.legacy, True)):
        if not path.is_file():
            continue
        try:
            result = json.loads(path.read_text())
            if not isinstance(result, dict):
                return None
        except (OSError, ValueError):
            return None
        if legacy:
            notice(f"legacy review record read from {path}; re-open the review to migrate it")
        return result, path
    return None


def write_review_record(paths: ReviewRecordPaths, record: dict) -> Path:
    # A project review can materialize outside `.grip/state/reviews`; commit and
    # push run from that member checkout, so leave one coordinate pointer there.
    # It deliberately contains no receipt fields.
    if paths.closing.exists() or paths.closing.is_symlink():
        raise ReviewRecordLocationError(f"review close is incomplete; recovery at {paths.closing}")
    repo = paths.repo_root or paths.legacy.parent.parent
    pointer = review_record_pointer_path(repo)
    if paths.current.is_symlink() or pointer.is_symlink():
        raise ReviewRecordLocationError("refusing a symlink review receipt or pointer")
    paths.current.parent.mkdir(parents=True, exist_ok=True)
    previous = paths.current.read_bytes() if paths.current.exists() else None
    staged: list[Path] = []

    def stage(target: Path, data: bytes) -> Path:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".review-", delete=False) as stream:
            path = Path(stream.name)
            staged.append(path)
            stream.write(data)
        return path

    try:
        payload = stage(paths.current, (json.dumps(record, indent=2) + "\n").encode())
        coordinate = stage(pointer, (str(paths.current) + "\n").encode())
        backup = stage(paths.current, previous) if previous is not None else None
        os.replace(payload, paths.current)
        try:
            os.replace(coordinate, pointer)
        except OSError as publication_error:
            if backup is not None:
                try:
                    os.replace(backup, paths.current)
                except OSError as rollback_error:
                    staged.remove(backup)
                    raise ReviewRecordLocationError(
                        f"pointer publication failed: {publication_error}; "
                        f"payload rollback failed: {rollback_error}; "
                        f"prior payload preserved for recovery at {backup}"
                    ) from rollback_error
            else:
                paths.current.unlink()
            raise
    finally:
        for path in staged:
            path.unlink(missing_ok=True)
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
            return ReviewRecordPaths(resolved_target, legacy, repo_path)
        except (OSError, ValueError):
            raise ReviewRecordLocationError("review pointer cannot be read safely")
    # A one-release legacy receipt is enough only where no pointer supplies a
    # canonical coordinate. When both exist, the safety guard receives both.
    if legacy.is_file():
        return ReviewRecordPaths(legacy, legacy, repo_path)
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
