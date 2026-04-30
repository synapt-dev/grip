"""Unit-of-work manifest: declarative cross-repo overlay activation."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

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
