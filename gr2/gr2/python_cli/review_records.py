"""Per-worktree safety records and independently workspace-owned recovery."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from . import gitops


class ReviewRecordLocationError(ValueError):
    pass


@dataclass(frozen=True)
class ReviewRecordPaths:
    current: Path
    legacy: Path
    repo_root: Path | None = None
    recovery_path: Path | None = None

    @property
    def closing(self) -> Path:
        """Incomplete disposal evidence, never an active review identity."""
        return self.recovery_path or self.current.with_name(self.current.name + ".closing.json")

    @property
    def context(self) -> Path:
        """Workspace coordinate carried by the pointer, not an active payload."""
        return self.legacy if self.recovery_path is not None else self.current

    @property
    def publication_pending(self) -> Path:
        return self.current.with_name(self.current.name + ".publication.json")


def _refuse_pending_publication(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise ReviewRecordLocationError(f"review publication is incomplete; explicit recovery at {path}")


def _physical_identity(path: Path) -> list[int]:
    stat = path.stat()
    return [stat.st_dev, stat.st_ino]


def _publication_coordinates(paths: ReviewRecordPaths) -> tuple[Path, str, str, str]:
    context = paths.context
    workspace = context.parents[5]
    owner, lane, member = context.parents[1].name, context.parent.name, context.stem
    expected = review_record_paths(workspace, owner, lane, member, paths.repo_root or paths.current.parent.parent)
    if expected != paths:
        raise ReviewRecordLocationError("publication coordinates differ from the calling worktree")
    if workspace.is_symlink():
        raise ReviewRecordLocationError("publication workspace is a symlink")
    current = workspace
    for component in context.parent.relative_to(workspace).parts:
        current /= component
        if current.is_symlink():
            raise ReviewRecordLocationError("publication workspace evidence ancestor is a symlink")
    return workspace, owner, lane, member


def _publication_ownership(paths: ReviewRecordPaths, targets: list[Path], evidence: bool) -> dict:
    workspace, owner, lane, member = _publication_coordinates(paths)
    repo = paths.repo_root or paths.current.parent.parent
    from ..prototypes import lane_workspace_prototype as lanes
    definition = lanes.lane_file(workspace, owner, lane)
    # The project producer writes member receipts before its lane definition
    # and allocation. Exact managed creation coordinates permit restoration of
    # those bytes only, never selection or deletion authority.
    if (not definition.exists() and not definition.is_symlink() and evidence
            and repo == workspace / "reviews" / owner / lane / "repos" / member):
        if repo.resolve() != repo:
            raise ReviewRecordLocationError("publication project coordinate is redirected")
    else:
        _validate_context_repo(workspace, owner, lane, member, repo)
    parents = {str(p.parent): _physical_identity(p.parent) for p in targets}
    current = workspace
    if evidence:
        for component in paths.context.parent.relative_to(workspace).parts:
            current /= component
            parents[str(current)] = _physical_identity(current)
    return {"workspace": str(workspace), "workspace_identity": _physical_identity(workspace), "parents": parents}


def _valid_physical_identity(value: object) -> bool:
    return isinstance(value, list) and len(value) == 2 and all(type(v) is int for v in value)


def _stage_publication_pending(paths: ReviewRecordPaths, pointer: Path,
                               previous: dict[Path, bytes | None], data: bytes) -> None:
    repo = paths.repo_root or paths.current.parent.parent
    doc = {"version": 1, "repo": str(repo), "repo_identity": _physical_identity(repo),
           "git_dir": str(paths.current.parent), "git_identity": _physical_identity(paths.current.parent),
           "context": str(paths.context), "workspace_evidence": paths.legacy in previous,
           "ownership": _publication_ownership(paths, list(previous), paths.legacy in previous),
           "targets": {str(target): {"old": old.hex() if old is not None else None,
                                     "new": ((str(paths.context) + "\n").encode() if target == pointer else data).hex()}
                       for target, old in previous.items()}}
    with tempfile.NamedTemporaryFile(dir=paths.current.parent, prefix=".publication-", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write((json.dumps(doc, indent=2) + "\n").encode())
    try:
        # Create-only: an existing pending record is never overwritten.
        os.link(temporary, paths.publication_pending)
    finally:
        temporary.unlink(missing_ok=True)


def recover_review_publication(paths: ReviewRecordPaths) -> None:
    """Explicitly restore prior bytes, without changing HEAD or workspace authority."""
    pending = paths.publication_pending
    repo = paths.repo_root or paths.current.parent.parent
    pointer = review_record_pointer_path(repo)
    try:
        if pending.is_symlink():
            raise ValueError("symlink pending record")
        doc = json.loads(pending.read_text())
        if (set(doc) != {"version", "repo", "repo_identity", "git_dir", "git_identity", "context", "workspace_evidence", "ownership", "targets"}
                or type(doc["version"]) is not int or doc["version"] != 1
                or type(doc["workspace_evidence"]) is not bool
                or not _valid_physical_identity(doc["repo_identity"])
                or not _valid_physical_identity(doc["git_identity"])
                or doc["repo"] != str(repo) or doc["context"] != str(paths.context)
                or doc["repo_identity"] != _physical_identity(repo)
                or doc["git_dir"] != str(paths.current.parent)
                or doc["git_identity"] != _physical_identity(paths.current.parent)
                or legacy_review_record_path(repo) != paths.current):
            raise ValueError("publication owner or Git identity changed")
        targets = list(dict.fromkeys([paths.current, pointer] + ([paths.legacy] if doc["workspace_evidence"] else [])))
        ownership = doc["ownership"]
        if (not isinstance(ownership, dict) or set(ownership) != {"workspace", "workspace_identity", "parents"}
                or not _valid_physical_identity(ownership["workspace_identity"])
                or not isinstance(ownership["parents"], dict)
                or not all(_valid_physical_identity(value) for value in ownership["parents"].values())
                or ownership != _publication_ownership(paths, targets, doc["workspace_evidence"])):
            raise ValueError("publication workspace or destination parent identity changed")
        if not isinstance(doc["targets"], dict) or set(doc["targets"]) != {str(p) for p in targets}:
            raise ValueError("publication targets differ from owning coordinates")
        originals = {}
        for target in targets:
            item = doc["targets"][str(target)]
            if not isinstance(item, dict) or set(item) != {"old", "new"}:
                raise ValueError("invalid publication evidence")
            old = None if item["old"] is None else bytes.fromhex(item["old"])
            new = bytes.fromhex(item["new"])
            if target.is_symlink():
                raise ValueError("publication target is a symlink")
            live = target.read_bytes() if target.exists() else None
            if live not in (old, new):
                raise ValueError("publication target content changed")
            originals[target] = old
        # Validate every destination before the first restoration. Partial
        # restoration or finalization failure retains the complete evidence.
        for target, old in originals.items():
            if old is None:
                target.unlink(missing_ok=True)
            else:
                with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".publication-restore-", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(old)
                try:
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
        if any((p.read_bytes() if p.exists() else None) != old for p, old in originals.items()):
            raise ValueError("publication restoration could not be verified")
        pending.unlink()
    except (OSError, ValueError, TypeError, AttributeError, KeyError, IndexError) as exc:
        raise ReviewRecordLocationError(f"publication recovery refused; retaining {pending}: {exc}") from exc


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
    proc = gitops.run_argv(["git", "-C", str(repo), "rev-parse", "--git-path", name])
    if proc.returncode or not proc.stdout.strip():
        raise ReviewRecordLocationError(f"cannot resolve worktree Git metadata for {repo}")
    path = Path(proc.stdout.strip())
    absolute = path if path.is_absolute() else repo / path
    return absolute.parent.resolve() / absolute.name


def legacy_review_record_path(lane_repo_root: Path | str) -> Path:
    # Review-open computes its coordinates before creating the ordinary clone.
    # No lookup is possible yet. Publication resolves again once Git exists.
    if not (Path(lane_repo_root) / ".git").exists():
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
    """Active Git safety, workspace compatibility and external close recovery."""
    owner = _component(owner_unit, "owner unit")
    lane = _component(lane_name, "lane name")
    key = _component(member, "member")
    workspace = Path(workspace_root).resolve()
    compatibility = workspace / ".grip" / "state" / "reviews" / owner / lane / f"{key}.json"
    return ReviewRecordPaths(legacy_review_record_path(lane_repo_root), compatibility,
                             Path(lane_repo_root).resolve(),
                             compatibility.with_name(compatibility.name + ".closing.json"))


def _validate_context_repo(workspace: Path, owner: str, lane: str, member: str, repo: Path) -> None:
    """Check relationship only. HEAD, dirty state and deletion are other contracts."""
    from ..prototypes import lane_workspace_prototype as lanes
    definition = lanes.lane_file(workspace, owner, lane)
    document = None
    if definition.exists() or definition.is_symlink():
        if definition.is_symlink():
            raise ReviewRecordLocationError("review context lane definition is a symlink")
        try:
            document = tomllib.loads(definition.read_text())
        except (OSError, ValueError) as exc:
            raise ReviewRecordLocationError("review context lane definition cannot be read") from exc
        if document.get("owner_unit") != owner or document.get("lane_name") != lane:
            raise ReviewRecordLocationError("review context does not declare this lane member")
        kind = document.get("lane_kind", "materialized")
        project = document.get("creation_source") == "project-review"
        bound = document.get("bound_worktree")
        if ((project and (kind not in {"materialized", "review-ephemeral"} or bound))
                or (not project and kind not in {"bound", "materialized"})
                or (kind == "materialized" and bound)):
            raise ReviewRecordLocationError("review context has conflicting or unsupported ownership modes")
        if kind == "bound":
            if (document.get("repos") != [member] or not document.get("bound_worktree")
                    or Path(document["bound_worktree"]).resolve() != repo):
                raise ReviewRecordLocationError("review context belongs to a different bound worktree")
            return
        if project:
            if member not in document.get("repos", []):
                raise ReviewRecordLocationError("review context does not declare this lane member")
            target = workspace / "reviews" / owner / lane
            if repo != target / "repos" / member or target.resolve() != target:
                raise ReviewRecordLocationError("review context belongs to a different project member")
            from .review_allocation import require_member_relationship, ReviewAllocationError
            try:
                require_member_relationship(workspace, target, owner, lane, repo)
            except ReviewAllocationError as exc:
                raise ReviewRecordLocationError(str(exc)) from exc
            return
    # Explicit ordinary review producers may have no lane definition. Their
    # managed coordinate remains exact, not guessed from a parent or selection.
    if definition.exists():
        try:
            expected = lanes.lane_repo_root(workspace, owner, lane, member)
        except SystemExit as exc:
            raise ReviewRecordLocationError(str(exc)) from exc
    else:
        expected = lanes.lane_dir(workspace, owner, lane) / "repos" / member
    if repo != expected or expected.resolve() != expected:
        raise ReviewRecordLocationError("review context belongs to a different materialized member")


def read_review_record(paths: ReviewRecordPaths, *, notice: Callable[[str], None] = print) -> dict | None:
    result = read_review_record_at(paths, notice=notice)
    return result[0] if result else None


def read_review_records_for_guard(paths: ReviewRecordPaths) -> tuple[dict, ...]:
    """Read every receipt present so a disposable mark cannot be masked.

    ``read_review_record_at`` deliberately chooses one path for identity and
    cleanup. The commit and push guard has a different question: whether any
    extant receipt marks this checkout disposable.
    """
    _refuse_pending_publication(paths.publication_pending)
    if paths.closing.exists() or paths.closing.is_symlink():
        raise ReviewRecordLocationError(f"review close is incomplete; recovery at {paths.closing}")
    records: list[dict] = []
    for path in dict.fromkeys((paths.current, paths.legacy)):
        if not path.is_file():
            continue
        try:
            record = json.loads(path.read_text())
            if not isinstance(record, dict):
                raise ValueError("receipt must be an object")
            records.append(record)
        except (OSError, ValueError) as exc:
            raise ReviewRecordLocationError(f"review receipt cannot be read: {path}") from exc
    if len(records) == 2 and records[0] != records[1]:
        raise ReviewRecordLocationError("active and workspace safety evidence conflict; reconcile retained evidence before rebinding")
    return tuple(records)


def read_review_record_at(paths: ReviewRecordPaths, *, notice: Callable[[str], None] = print) -> tuple[dict, Path] | None:
    """Read a receipt and return the exact path that supplied it for cleanup."""
    _refuse_pending_publication(paths.publication_pending)
    if paths.closing.exists() or paths.closing.is_symlink():
        return None
    # Selection cannot turn a preferred active receipt into a way of masking
    # conflicting or unreadable compatibility evidence.
    read_review_records_for_guard(paths)
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


def write_review_record(paths: ReviewRecordPaths, record: dict, *, workspace_evidence: bool = False) -> Path:
    # A project review can materialize outside `.grip/state/reviews`; commit and
    # push run from that member checkout, so leave one coordinate pointer there.
    # It deliberately contains no receipt fields.
    _refuse_pending_publication(paths.publication_pending)
    if paths.closing.exists() or paths.closing.is_symlink():
        raise ReviewRecordLocationError(f"review close is incomplete; recovery at {paths.closing}")
    repo = paths.repo_root or paths.legacy.parent.parent
    pointer = review_record_pointer_path(repo)
    # Existing workspace evidence participates in explicit rebind. New ordinary
    # binds stay Git-only, while project creation requests independent evidence.
    evidence = workspace_evidence or paths.legacy.exists() or paths.legacy.is_symlink()
    targets = list(dict.fromkeys([paths.current, pointer] + ([paths.legacy] if evidence else [])))
    if any(target.is_symlink() for target in targets):
        raise ReviewRecordLocationError("refusing a symlink review receipt, pointer or workspace evidence")
    read_review_records_for_guard(paths)
    previous = {target: target.read_bytes() if target.exists() else None for target in targets}
    _publication_coordinates(paths)
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []

    def stage(target: Path, data: bytes) -> Path:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".review-", delete=False) as stream:
            path = Path(stream.name)
            staged.append(path)
            stream.write(data)
        return path

    changed: list[Path] = []
    try:
        data = (json.dumps(record, indent=2) + "\n").encode()
        replacements = {target: stage(target, (str(paths.context) + "\n").encode()
                                      if target == pointer else data) for target in targets}
        backups = {target: stage(target, old) if old is not None else None
                   for target, old in previous.items()}
        _stage_publication_pending(paths, pointer, previous, data)
        try:
            for target in targets:
                os.replace(replacements[target], target)
                changed.append(target)
        except OSError as publication_error:
            failures = []
            for target in reversed(changed):
                backup = backups[target]
                try:
                    if backup is None:
                        target.unlink(missing_ok=True)
                    else:
                        os.replace(backup, target)
                except OSError as rollback_error:
                    if backup is not None:
                        staged.remove(backup)
                    failures.append(f"{target}: {rollback_error}; prior bytes at {backup}" if backup
                                    else f"{target}: {rollback_error}; prior state was absent")
            if failures:
                raise ReviewRecordLocationError(
                    f"review publication failed: {publication_error}; rollback failed: "
                    + "; ".join(failures)
                ) from publication_error
            if any((target.read_bytes() if target.exists() else None) != old
                   for target, old in previous.items()):
                raise ReviewRecordLocationError("review rollback verification failed; retaining publication evidence") from publication_error
            paths.publication_pending.unlink()
            raise
        if any(target.read_bytes() != ((str(paths.context) + "\n").encode() if target == pointer else data)
               for target in targets):
            raise ReviewRecordLocationError("review publication verification failed; retaining publication evidence")
        paths.publication_pending.unlink()
    finally:
        for path in staged:
            path.unlink(missing_ok=True)
    return paths.current


def lane_paths_for_repo(repo: Path | str) -> ReviewRecordPaths | None:
    """Locate only a repo structurally inside a materialized lane, never by scan."""
    repo_path = Path(repo).resolve()
    legacy = legacy_review_record_path(repo_path)
    _refuse_pending_publication(legacy.with_name(legacy.name + ".publication.json"))
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
            workspace = Path(*parts[:index])
            key = filename[:-5]
            _validate_context_repo(workspace, owner, lane, key, repo_path)
            return review_record_paths(workspace, owner, lane, key, repo_path)
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
