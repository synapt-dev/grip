"""Cross-repo atomic overlay activation with rollback."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from gr2_overlay.activate import (
    OverlayActivationError,
    activate_overlay,
    deactivate_overlay,
)
from gr2_overlay.types import OverlayRef


@dataclass(frozen=True)
class RepoOverlayTarget:
    repo_name: str
    checkout_root: Path
    overlay_store: Path
    overlay_ref: OverlayRef
    overlay_source_kind: str
    overlay_source_value: str | None
    overlay_signer: str | None


@dataclass
class CrossRepoActivationResult:
    status: str
    completed_repos: list[str] = field(default_factory=list)
    rolled_back_repos: list[str] = field(default_factory=list)


class CrossRepoActivationError(Exception):
    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        failing_repo: str,
        rolled_back_repos: list[str],
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.failing_repo = failing_repo
        self.rolled_back_repos = rolled_back_repos


def activate_overlays_atomically(
    targets: list[RepoOverlayTarget],
) -> CrossRepoActivationResult:
    snapshots: dict[str, dict[str, str]] = {}
    applied: list[RepoOverlayTarget] = []

    for target in targets:
        snapshots[target.repo_name] = _snapshot(target.checkout_root)

    try:
        for target in targets:
            activate_overlay(
                workspace_root=target.checkout_root,
                overlay_store=target.overlay_store,
                overlay_ref=target.overlay_ref,
                overlay_source_kind=target.overlay_source_kind,
                overlay_source_value=target.overlay_source_value,
                overlay_signer=target.overlay_signer,
            )
            applied.append(target)
    except OverlayActivationError as e:
        failing_target = target
        rolled_back: list[str] = []

        for prev in reversed(applied):
            _restore_snapshot(prev.checkout_root, snapshots[prev.repo_name])
            rolled_back.append(prev.repo_name)
        rolled_back.reverse()

        if _snapshot(failing_target.checkout_root) != snapshots[failing_target.repo_name]:
            _restore_snapshot(failing_target.checkout_root, snapshots[failing_target.repo_name])
            rolled_back.append(failing_target.repo_name)

        raise CrossRepoActivationError(
            str(e),
            error_code=e.error_code,
            failing_repo=failing_target.repo_name,
            rolled_back_repos=rolled_back,
        ) from e

    return CrossRepoActivationResult(
        status="ok",
        completed_repos=[t.repo_name for t in applied],
    )


def _snapshot(root: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[str(path.relative_to(root))] = path.read_bytes()
    return result


def _restore_snapshot(root: Path, snapshot: dict[str, bytes | str]) -> None:
    current_files = set()
    for path in root.rglob("*"):
        if path.is_file():
            current_files.add(str(path.relative_to(root)))

    for rel_path in current_files - set(snapshot.keys()):
        target = root / rel_path
        target.unlink()

    for rel_path, content in snapshot.items():
        target = root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            target.write_text(content)
        else:
            target.write_bytes(content)

    for dirpath in sorted(root.rglob("*"), reverse=True):
        if dirpath.is_dir() and not any(dirpath.iterdir()):
            dirpath.rmdir()
