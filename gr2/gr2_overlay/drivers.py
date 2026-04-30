"""Curated overlay merge drivers: deep, prepend, union."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any

import tomli_w
import yaml

from gr2_overlay.types import OverlayRef

CURATED_DRIVERS: dict[str, str] = {
    "overlay-deep": "deep",
    "overlay-prepend": "prepend",
    "overlay-union": "union",
}


def install_driver_registry() -> None:
    home = Path(os.environ["HOME"])
    gitconfig = home / ".gitconfig"

    existing = gitconfig.read_text() if gitconfig.exists() else ""

    sections: list[str] = []
    for driver_name in CURATED_DRIVERS:
        header = f'[merge "{driver_name}"]'
        if header not in existing:
            sections.append(
                f"{header}\n"
                f"\tname = {driver_name}\n"
                f"\tdriver = gr2-overlay-driver {driver_name} %O %A %B %P\n"
            )

    if sections:
        with gitconfig.open("a") as f:
            f.write("\n".join(sections) + "\n")


def invoke_driver(
    driver_name: str,
    ancestor: Path,
    current: Path,
    other: Path,
    relative_path: str,
    *,
    source_overlay: OverlayRef,
    trusted_overlay_sources: set[str],
) -> None:
    if driver_name not in CURATED_DRIVERS:
        raise ValueError(f"Unknown overlay driver: {driver_name}")

    if source_overlay.ref_path not in trusted_overlay_sources:
        raise PermissionError(f"Overlay source {source_overlay.ref_path} is not in the allowlist")

    handlers = {
        "overlay-deep": _driver_deep,
        "overlay-prepend": _driver_prepend,
        "overlay-union": _driver_union,
    }
    handlers[driver_name](ancestor, current, other, relative_path)


def _driver_deep(ancestor: Path, current: Path, other: Path, relative_path: str) -> None:
    suffix = Path(relative_path).suffix

    if suffix == ".toml":
        current_data = tomllib.loads(current.read_text())
        other_data = tomllib.loads(other.read_text())
        merged = _deep_merge(current_data, other_data)
        current.write_bytes(tomli_w.dumps(merged).encode())
    elif suffix in {".yml", ".yaml"}:
        current_data = yaml.safe_load(current.read_text()) or {}
        other_data = yaml.safe_load(other.read_text()) or {}
        merged = _deep_merge(current_data, other_data)
        current.write_text(yaml.dump(merged, default_flow_style=False))
    elif suffix == ".json":
        current_data = json.loads(current.read_text())
        other_data = json.loads(other.read_text())
        merged = _deep_merge(current_data, other_data)
        current.write_text(json.dumps(merged, indent=2) + "\n")


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _driver_prepend(ancestor: Path, current: Path, other: Path, relative_path: str) -> None:
    current.write_text(other.read_text() + current.read_text())


def _driver_union(ancestor: Path, current: Path, other: Path, relative_path: str) -> None:
    current_lines = current.read_text().splitlines()
    other_lines = other.read_text().splitlines()

    seen = set(current_lines)
    result = list(current_lines)
    for line in other_lines:
        if line not in seen:
            result.append(line)
            seen.add(line)

    current.write_text("\n".join(result) + "\n" if result else "")
