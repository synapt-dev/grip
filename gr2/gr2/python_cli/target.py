"""gr2 `target`: the stored PR target (`[settings].target` in the workspace spec).

This is the WRITER for the value gr2 `prune` already reads. gr1's `gr target set`
writes the gr1 manifest (`.gitgrip/...`), a different file; this writes
`.grip/workspace_spec.toml`, which is what gr2 reads.

The write is a faithful round-trip -- `tomllib.load` the whole spec, set (or
remove) `settings.target`, `tomli_w.dumps` the whole thing back, atomically --
so every other field (repos, units, workspace_name, an existing merge_method)
survives untouched. It deliberately does NOT go through `_write_workspace_spec`,
which regenerates the spec from repos/units alone and emits no `[settings]` table
at all (it would silently drop merge_method). The spec is machine-written, so the
round-trip dropping TOML comments is a non-issue -- there are none to keep.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from pathlib import Path

import tomli_w


class TargetError(Exception):
    pass


_SPEC_REL = (".grip", "workspace_spec.toml")


def spec_path(workspace_root: Path) -> Path:
    return workspace_root.joinpath(*_SPEC_REL)


def _load(workspace_root: Path) -> dict:
    path = spec_path(workspace_root)
    if not path.is_file():
        raise TargetError(
            f"no workspace spec at {path}: a stored target needs a workspace "
            f"(run inside a gripspace, or set --target on prune directly)"
        )
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _settings(spec: dict) -> dict:
    settings = spec.get("settings", {})
    if not isinstance(settings, dict):
        raise TargetError("workspace spec [settings] must be a table")
    return settings


def _atomic_write(workspace_root: Path, spec: dict) -> None:
    path = spec_path(workspace_root)
    data = tomli_w.dumps(spec)
    # Atomic: write a sibling temp file then os.replace, so a crash mid-write
    # can never leave a truncated spec.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".workspace_spec.", suffix=".toml")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def show_target(workspace_root: Path) -> str | None:
    """The stored `[settings].target`, or None if unset."""
    value = _settings(_load(workspace_root)).get("target")
    if value is None:
        return None
    if not isinstance(value, str):
        raise TargetError("workspace setting target must be a string")
    return value


def set_target(workspace_root: Path, branch: str) -> None:
    """Write `[settings].target = <branch>`, preserving every other field."""
    if not branch:
        raise TargetError("target branch must be a non-empty name")
    spec = _load(workspace_root)
    spec.setdefault("settings", {})
    if not isinstance(spec["settings"], dict):
        raise TargetError("workspace spec [settings] must be a table")
    spec["settings"]["target"] = branch
    _atomic_write(workspace_root, spec)


def unset_target(workspace_root: Path) -> bool:
    """Remove `[settings].target` if present. Returns True if something was removed."""
    spec = _load(workspace_root)
    settings = spec.get("settings")
    if isinstance(settings, dict) and "target" in settings:
        del settings["target"]
        _atomic_write(workspace_root, spec)
        return True
    return False
