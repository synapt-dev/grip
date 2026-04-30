"""WorkspaceSpec overlays section: declare overlay entries for a workspace."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OverlaySpecEntry:
    name: str
    path: str
    applies_to: list[str]
    priority: int = 0


def overlay_spec_path(workspace_root: Path) -> Path:
    return workspace_root / ".grip" / "overlays.toml"


def load_overlay_spec(workspace_root: Path) -> list[OverlaySpecEntry]:
    spec_file = overlay_spec_path(workspace_root)
    if not spec_file.exists():
        return []

    text = spec_file.read_text()
    if not text.strip():
        return []

    data = tomllib.loads(text)
    raw_entries = data.get("overlays", [])

    return [
        OverlaySpecEntry(
            name=entry["name"],
            path=entry["path"],
            applies_to=entry["applies_to"],
            priority=entry.get("priority", 0),
        )
        for entry in raw_entries
    ]


def validate_overlay_spec(entries: list[OverlaySpecEntry]) -> None:
    seen_names: set[str] = set()

    for entry in entries:
        if not entry.name:
            raise ValueError("Overlay entry has empty name")
        if not entry.path:
            raise ValueError("Overlay entry has empty path")
        if not entry.applies_to:
            raise ValueError("Overlay entry has empty applies_to")
        if entry.priority < 0:
            raise ValueError(f"Overlay entry '{entry.name}' has negative priority")
        if entry.name in seen_names:
            raise ValueError(f"Duplicate overlay name: '{entry.name}'")
        seen_names.add(entry.name)
