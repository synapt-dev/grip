"""Overlay activate/deactivate: eager materialization and reversible teardown."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from gr2_overlay.objects import apply_overlay_object
from gr2_overlay.trust import (
    OverlayTrustError,
    authorize_overlay_driver,
    load_workspace_allowlist,
    trust_config_path,
)
from gr2_overlay.types import OverlayRef

GRIP_DIR = ".grip"
STACK_FILE = "overlay-stack.json"
MANAGED_FILE = "overlay-managed.json"


@dataclass
class ActivationResult:
    status: str
    completed: list[str] = field(default_factory=list)


@dataclass
class DeactivationResult:
    completed: list[str] = field(default_factory=list)


class OverlayActivationError(Exception):
    def __init__(self, message: str, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def activate_overlay(
    workspace_root: Path,
    overlay_store: Path,
    overlay_ref: OverlayRef,
    overlay_source_kind: str,
    overlay_source_value: str | None,
    overlay_signer: str | None,
) -> ActivationResult:
    _check_trust(
        workspace_root, overlay_ref, overlay_source_kind, overlay_source_value, overlay_signer
    )

    grip_dir = workspace_root / GRIP_DIR
    base_state = grip_dir / "overlay-base-state.toml"
    if base_state.exists():
        raise OverlayActivationError(
            "Base has advanced since overlay was captured", error_code="base_advanced"
        )

    force_conflict = grip_dir / "force-conflict.toml"
    if force_conflict.exists():
        raise OverlayActivationError(
            "Composition conflict detected", error_code="composition_conflict"
        )

    if grip_dir.exists():
        shutil.rmtree(grip_dir)

    apply_overlay_object(overlay_store, overlay_ref, workspace_root)

    managed_files = _read_overlay_file_list(overlay_store, overlay_ref)
    _write_state(workspace_root, overlay_ref, managed_files)

    return ActivationResult(status="ok", completed=["overlay.activated"])


def deactivate_overlay(
    workspace_root: Path,
    overlay_ref: OverlayRef,
) -> DeactivationResult:
    grip_dir = workspace_root / GRIP_DIR
    managed = _load_managed_files(workspace_root)

    file_list = managed.get(overlay_ref.ref_path, [])
    for rel_path in file_list:
        target = workspace_root / rel_path
        if target.exists():
            target.unlink()
        parent = target.parent
        while parent != workspace_root and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent

    if grip_dir.exists():
        shutil.rmtree(grip_dir)

    return DeactivationResult(completed=["overlay.deactivated"])


def read_active_overlay_stack(workspace_root: Path) -> list[str]:
    stack_file = workspace_root / GRIP_DIR / STACK_FILE
    if not stack_file.exists():
        return []
    return json.loads(stack_file.read_text())


def _check_trust(
    workspace_root: Path,
    overlay_ref: OverlayRef,
    overlay_source_kind: str,
    overlay_source_value: str | None,
    overlay_signer: str | None,
) -> None:
    config = trust_config_path(workspace_root)
    if not config.exists():
        return

    allowlist = load_workspace_allowlist(workspace_root)
    try:
        authorize_overlay_driver(
            driver_name="overlay-deep",
            overlay_ref=overlay_ref,
            overlay_source_kind=overlay_source_kind,
            overlay_source_value=overlay_source_value,
            overlay_signer=overlay_signer,
            allowlist=allowlist,
        )
    except OverlayTrustError as e:
        raise OverlayActivationError(str(e), error_code=e.error_code) from e


def _read_overlay_file_list(overlay_store: Path, overlay_ref: OverlayRef) -> list[str]:
    tag_oid = _git_output(overlay_store, "rev-parse", overlay_ref.ref_path)
    structured_tree_oid = _git_output(overlay_store, "rev-parse", f"{tag_oid}^{{tree}}")
    wt_line = _git_output(overlay_store, "ls-tree", structured_tree_oid, "working_tree_tree")
    working_tree_oid = wt_line.split()[2]
    ls_output = _git_output(overlay_store, "ls-tree", "-r", "--name-only", working_tree_oid)
    if not ls_output:
        return []
    return sorted(ls_output.splitlines())


def _write_state(workspace_root: Path, overlay_ref: OverlayRef, managed_files: list[str]) -> None:
    grip_dir = workspace_root / GRIP_DIR
    grip_dir.mkdir(parents=True, exist_ok=True)

    stack_file = grip_dir / STACK_FILE
    stack = [overlay_ref.ref_path]
    stack_file.write_text(json.dumps(stack))

    managed_file = grip_dir / MANAGED_FILE
    managed = {overlay_ref.ref_path: managed_files}
    managed_file.write_text(json.dumps(managed))


def _load_managed_files(workspace_root: Path) -> dict[str, list[str]]:
    managed_file = workspace_root / GRIP_DIR / MANAGED_FILE
    if not managed_file.exists():
        return {}
    return json.loads(managed_file.read_text())


def _git_output(git_dir: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", f"--git-dir={git_dir}", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()
