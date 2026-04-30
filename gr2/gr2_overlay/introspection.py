"""Overlay introspection: stack, trace, why, impact, status queries."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path
from typing import Any

from gr2_overlay.types import OverlayRef

GRIP_DIR = ".grip"


def overlay_stack(
    workspace_root: Path,
    overlay_store: Path,
    json_output: bool,
) -> str | dict[str, Any]:
    stack_file = workspace_root / GRIP_DIR / "overlay-stack.toml"
    data = _load_toml(stack_file)

    active_refs = data.get("active", [])
    available_refs = data.get("available", [])

    active_entries = [_ref_entry(r) for r in active_refs]
    available_entries = [_ref_entry(r) for r in available_refs]

    if json_output:
        return {"active": active_entries, "available": available_entries}

    lines = ["Active overlays:"]
    for entry in active_entries:
        lines.append(f"  {entry['author']}/{entry['name']} ({entry['ref']})")
    lines.append("Available overlays:")
    for entry in available_entries:
        lines.append(f"  {entry['author']}/{entry['name']} ({entry['ref']})")
    return "\n".join(lines)


def overlay_trace(
    workspace_root: Path,
    overlay_store: Path,
    file_path: str,
    json_output: bool,
) -> str | dict[str, Any]:
    attr_file = workspace_root / GRIP_DIR / "overlay-attribution.toml"
    data = _load_toml(attr_file)

    file_data = data.get("files", {}).get(file_path, {})
    regions = file_data.get("lines", [])

    if json_output:
        return {"file": file_path, "regions": regions}

    lines = [f"Trace for {file_path}:"]
    for region in regions:
        lines.append(f"  lines {region['start']}-{region['end']}: {region['ref']}")
    return "\n".join(lines)


def overlay_why(
    workspace_root: Path,
    overlay_store: Path,
    file_path: str,
    json_output: bool,
) -> str | dict[str, Any]:
    why_file = workspace_root / GRIP_DIR / "overlay-why.toml"
    data = _load_toml(why_file)

    file_data = data.get("files", {}).get(file_path, {})
    rule = file_data.get("rule", "")
    reason = file_data.get("reason", "")
    ref = file_data.get("ref", "")

    if json_output:
        return {"rule": rule, "reason": reason, "ref": ref}

    return f"{file_path}: rule={rule}, reason={reason} (ref={ref})"


def overlay_impact(
    overlay_store: Path,
    overlay_ref: OverlayRef,
    json_output: bool,
) -> str | dict[str, Any]:
    files = _read_overlay_file_list(overlay_store, overlay_ref)

    if json_output:
        return {"files": files}

    lines = [f"Files touched by {overlay_ref.author}/{overlay_ref.name}:"]
    for f in files:
        lines.append(f"  {f}")
    return "\n".join(lines)


def overlay_status(
    workspace_root: Path,
    overlay_store: Path,
    json_output: bool,
) -> str | dict[str, Any]:
    status_file = workspace_root / GRIP_DIR / "overlay-status.toml"
    data = _load_toml(status_file)

    active = data.get("active", [])
    available = data.get("available", [])
    applied = data.get("applied", [])

    if json_output:
        return {"active": active, "available": available, "applied": applied}

    lines = [
        "Active: " + ", ".join(active) if active else "Active: (none)",
        "Available: " + ", ".join(available) if available else "Available: (none)",
        "Applied: " + ", ".join(applied) if applied else "Applied: (none)",
    ]
    return "\n".join(lines)


def _ref_entry(ref_path: str) -> dict[str, str]:
    parts = ref_path.replace("refs/overlays/", "").split("/", 1)
    author = parts[0] if parts else ""
    name = parts[1] if len(parts) > 1 else ""
    return {"ref": ref_path, "author": author, "name": name}


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text()
    if not text.strip():
        return {}
    return tomllib.loads(text)


def _read_overlay_file_list(overlay_store: Path, overlay_ref: OverlayRef) -> list[str]:
    tag_oid = _git_output(overlay_store, "rev-parse", overlay_ref.ref_path)
    structured_tree_oid = _git_output(overlay_store, "rev-parse", f"{tag_oid}^{{tree}}")
    wt_line = _git_output(overlay_store, "ls-tree", structured_tree_oid, "working_tree_tree")
    working_tree_oid = wt_line.split()[2]
    ls_output = _git_output(overlay_store, "ls-tree", "-r", "--name-only", working_tree_oid)
    if not ls_output:
        return []
    return sorted(ls_output.splitlines())


def _git_output(git_dir: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", f"--git-dir={git_dir}", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()
