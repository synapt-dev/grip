"""Unit-of-work manifest: declarative cross-repo overlay activation."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from gr2_overlay.cross_repo import (
    RepoOverlayTarget,
    activate_overlays_atomically,
)
from gr2_overlay.types import OverlayRef

_REFS_OVERLAYS_PREFIX = "refs/overlays/"
_VALID_SCOPES = {"workspace", "repo"}
_VALID_ON_FAILURE = {"rollback"}


@dataclass
class UnitOverlaySource:
    repo_name: str
    overlay_ref: OverlayRef
    overlay_source_kind: str
    overlay_source_value: str
    overlay_signer: str | None = None


@dataclass
class UnitManifest:
    version: int
    scope: str
    source_overlays: list[UnitOverlaySource]
    target_base_ref: str
    depends_on: list[str] = field(default_factory=list)
    on_failure: str = "rollback"


@dataclass
class UnitApplyPreview:
    status: str
    unit_name: str
    scope: str
    target_base_ref: str
    on_failure: str
    depends_on: list[str]
    repo_order: list[str]
    overlay_refs: list[str]


def unit_manifest_path(workspace_root: Path, name: str) -> Path:
    return workspace_root / ".grip" / "units" / f"{name}.toml"


def load_unit_manifest(workspace_root: Path, name: str) -> UnitManifest:
    path = unit_manifest_path(workspace_root, name)
    raw = tomllib.loads(path.read_text())

    source_overlays = []
    for entry in raw.get("source_overlays", []):
        ref_str = entry["overlay_ref"]
        if ref_str.startswith(_REFS_OVERLAYS_PREFIX):
            ref_str = ref_str[len(_REFS_OVERLAYS_PREFIX) :]
        overlay_ref = OverlayRef.parse(ref_str)

        source_overlays.append(
            UnitOverlaySource(
                repo_name=entry["repo_name"],
                overlay_ref=overlay_ref,
                overlay_source_kind=entry["overlay_source_kind"],
                overlay_source_value=entry["overlay_source_value"],
                overlay_signer=entry.get("overlay_signer"),
            )
        )

    return UnitManifest(
        version=raw["version"],
        scope=raw["scope"],
        source_overlays=source_overlays,
        target_base_ref=raw["target_base_ref"],
        depends_on=raw.get("depends_on", []),
        on_failure=raw["on_failure"],
    )


def validate_unit_manifest(manifest: UnitManifest) -> None:
    if manifest.version != 1:
        raise ValueError(f"Unsupported unit manifest version: {manifest.version}")
    if manifest.scope not in _VALID_SCOPES:
        raise ValueError(f"Invalid scope '{manifest.scope}': must be one of {_VALID_SCOPES}")
    if not manifest.source_overlays:
        raise ValueError("source_overlays must not be empty")
    seen_repos: set[str] = set()
    for source in manifest.source_overlays:
        if source.repo_name in seen_repos:
            raise ValueError(f"Duplicate source overlay repo_name: '{source.repo_name}'")
        seen_repos.add(source.repo_name)
    if not manifest.target_base_ref:
        raise ValueError("target_base_ref must not be empty")
    if manifest.on_failure not in _VALID_ON_FAILURE:
        raise ValueError(
            f"Invalid on_failure '{manifest.on_failure}': must be one of {_VALID_ON_FAILURE}"
        )


def preview_unit_apply(*, workspace_root: Path, unit_name: str) -> UnitApplyPreview:
    manifest = load_unit_manifest(workspace_root, unit_name)
    validate_unit_manifest(manifest)
    return UnitApplyPreview(
        status="ok",
        unit_name=unit_name,
        scope=manifest.scope,
        target_base_ref=manifest.target_base_ref,
        on_failure=manifest.on_failure,
        depends_on=manifest.depends_on,
        repo_order=[s.repo_name for s in manifest.source_overlays],
        overlay_refs=[s.overlay_ref.ref_path for s in manifest.source_overlays],
    )


def apply_unit(
    *, workspace_root: Path, unit_name: str
) -> dict[str, object]:
    order = _resolve_dependency_order(workspace_root, unit_name)
    applied: list[str] = []
    for name in order:
        _apply_single_unit(workspace_root=workspace_root, unit_name=name)
        applied.append(name)
    return {"applied_units": applied, "status": "ok"}


def _apply_single_unit(
    *, workspace_root: Path, unit_name: str
) -> object:
    manifest = load_unit_manifest(workspace_root, unit_name)
    validate_unit_manifest(manifest)
    targets = _build_targets(workspace_root, manifest)
    return activate_overlays_atomically(targets=targets)


def abort_unit(
    *, workspace_root: Path, unit_name: str
) -> dict[str, object]:
    state_path = (
        workspace_root / ".grip" / "unit-transactions" / f"{unit_name}.json"
    )
    state: dict[str, object] = json.loads(state_path.read_text())
    result = rollback_inflight_unit(workspace_root=workspace_root, state=state)
    state_path.unlink()
    return result


def rollback_inflight_unit(
    *, workspace_root: Path, state: dict[str, object]
) -> dict[str, object]:
    raise NotImplementedError("rollback_inflight_unit not yet implemented")


def _build_targets(
    workspace_root: Path, manifest: UnitManifest
) -> list[RepoOverlayTarget]:
    targets: list[RepoOverlayTarget] = []
    for src in manifest.source_overlays:
        repo_root = workspace_root / "repos" / src.repo_name
        targets.append(
            RepoOverlayTarget(
                repo_name=src.repo_name,
                checkout_root=repo_root,
                overlay_store=repo_root / ".grip" / "overlays",
                overlay_ref=src.overlay_ref,
                overlay_source_kind=src.overlay_source_kind,
                overlay_source_value=src.overlay_source_value,
                overlay_signer=src.overlay_signer,
            )
        )
    return targets


def _resolve_dependency_order(workspace_root: Path, unit_name: str) -> list[str]:
    order: list[str] = []
    visited: set[str] = set()
    in_progress: set[str] = set()

    def visit(name: str) -> None:
        if name in in_progress:
            raise ValueError(f"dependency cycle detected involving '{name}'")
        if name in visited:
            return
        in_progress.add(name)
        manifest = load_unit_manifest(workspace_root, name)
        for dep in manifest.depends_on:
            visit(dep)
        in_progress.remove(name)
        visited.add(name)
        order.append(name)

    visit(unit_name)
    return order
