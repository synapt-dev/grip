"""Config base + overlay model: Docker OverlayFS analogy for workspace config.

Base layer: TOML files (cold, human-authored, git-reviewed)
Overlay layer: JSON files (hot, agent-writable)
Reads: overlay first, base fallback
Writes: overlay only
_base_sha: overlay tracks which base version it extends
"""
from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from copy import deepcopy
from pathlib import Path
from typing import Any

import toml


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BaseStaleError(Exception):
    """Raised when overlay's _base_sha doesn't match current base content."""


class OverlayCorruptError(Exception):
    """Raised when an overlay JSON file contains invalid JSON."""


class PolicyViolationError(Exception):
    """Raised when a write is blocked by the active policy."""


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------


class WritePolicy(ABC):
    @abstractmethod
    def can_write(self, agent: str, section: str, key: str) -> bool: ...


class FreeWritePolicy(WritePolicy):
    def can_write(self, agent: str, section: str, key: str) -> bool:
        return True


class OwnWriteOnlyPolicy(WritePolicy):
    """Agents can only write to their own section (agents.<name>, prompts.<name>).

    Shared sections (spawn, tools, etc.) are writable by anyone.
    """

    _SCOPED_PREFIXES = ("agents.", "prompts.")

    def can_write(self, agent: str, section: str, key: str) -> bool:
        for prefix in self._SCOPED_PREFIXES:
            if section.startswith(prefix):
                section_owner = section[len(prefix) :].split(".")[0]
                return section_owner == agent
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_sha(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge overlay into base. Overlay wins on conflict."""
    result = deepcopy(base)
    for key, val in overlay.items():
        if key == "_base_sha":
            continue
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = deepcopy(val)
    return result


def _resolve_dotted_key(data: dict, key: str) -> Any:
    parts = key.split(".")
    current: Any = data
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _set_nested(data: dict, dotted_section: str, key: str, value: Any) -> None:
    parts = dotted_section.split(".")
    current = data
    for part in parts:
        if part not in current or not isinstance(current[part], dict):
            current[part] = {}
        current = current[part]
    current[key] = value


def _overlay_stem(base_path: Path) -> str:
    return base_path.stem


def _safe_json_load(path: Path) -> dict:
    """Load JSON from path. On corrupt JSON, quarantine the file and raise OverlayCorruptError."""
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, ValueError):
        corrupt_path = path.with_suffix(path.suffix + ".corrupt")
        path.rename(corrupt_path)
        raise OverlayCorruptError(
            f"Corrupt overlay JSON at {path.name}. "
            f"Quarantined to {corrupt_path.name}."
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def config_apply(base_path: Path, overlay_dir: Path) -> dict:
    """Materialize TOML base into JSON overlay with _base_sha.

    If overlay already exists, merges new base keys into it while
    preserving existing overlay modifications.
    """
    base_content = base_path.read_text()
    base_data = toml.loads(base_content)
    sha = _base_sha(base_content)

    overlay_dir.mkdir(parents=True, exist_ok=True)
    overlay_path = overlay_dir / f"{_overlay_stem(base_path)}.json"

    if overlay_path.exists():
        existing = _safe_json_load(overlay_path)
        existing.pop("_base_sha", None)
        merged = _deep_merge(base_data, existing)
    else:
        merged = deepcopy(base_data)

    merged["_base_sha"] = sha
    overlay_path.write_text(json.dumps(merged, indent=2) + "\n")
    return {k: v for k, v in merged.items() if k != "_base_sha"}


def config_show(
    base_path: Path,
    overlay_dir: Path,
    key: str | None = None,
    *,
    strict: bool = False,
) -> Any:
    """Read config with overlay-first, base-fallback resolution.

    If key is None, returns the full merged dict.
    If key is a dotted path (e.g. "agents.opus.model"), resolves to that value.
    Returns None for missing keys.
    """
    base_content = base_path.read_text()
    base_data = toml.loads(base_content)

    overlay_path = overlay_dir / f"{_overlay_stem(base_path)}.json"
    overlay_data: dict = {}

    if overlay_path.exists():
        overlay_data = _safe_json_load(overlay_path)
        if strict:
            stored_sha = overlay_data.get("_base_sha", "")
            current_sha = _base_sha(base_content)
            if stored_sha != current_sha:
                raise BaseStaleError(
                    f"Overlay _base_sha ({stored_sha[:12]}...) does not match "
                    f"current base ({current_sha[:12]}...)"
                )

    clean_overlay = {k: v for k, v in overlay_data.items() if k != "_base_sha"}
    merged = _deep_merge(base_data, clean_overlay)

    # Include prompt overlays in the merged view
    prompts_dir = overlay_dir / "prompts"
    if prompts_dir.is_dir():
        prompts: dict[str, Any] = merged.get("prompts", {})
        for pf in sorted(prompts_dir.glob("*.json")):
            agent_name = pf.stem
            agent_prompts = _safe_json_load(pf)
            if agent_name in prompts and isinstance(prompts[agent_name], dict):
                prompts[agent_name] = _deep_merge(prompts[agent_name], agent_prompts)
            else:
                prompts[agent_name] = agent_prompts
        if prompts:
            merged["prompts"] = prompts

    if key is None:
        return merged
    return _resolve_dotted_key(merged, key)


def overlay_write(
    overlay_dir: Path,
    section: str,
    key: str,
    value: Any,
    *,
    agent: str = "",
    policy: WritePolicy | None = None,
    prompt_overlay: bool = False,
) -> None:
    """Write a value to the overlay. Respects policy enforcement.

    If prompt_overlay is True, writes to prompts/{agent_name}.json instead
    of the main overlay file.
    """
    if policy is not None and agent:
        if not policy.can_write(agent=agent, section=section, key=key):
            raise PolicyViolationError(
                f"Agent '{agent}' cannot write to section '{section}' "
                f"under {policy.__class__.__name__}"
            )

    if prompt_overlay:
        parts = section.split(".")
        if len(parts) >= 2 and parts[0] == "prompts":
            agent_name = parts[1]
            prompts_dir = overlay_dir / "prompts"
            prompts_dir.mkdir(parents=True, exist_ok=True)
            prompt_path = prompts_dir / f"{agent_name}.json"

            data: dict = {}
            if prompt_path.exists():
                data = json.loads(prompt_path.read_text())
            data[key] = value
            prompt_path.write_text(json.dumps(data, indent=2) + "\n")
            return

    overlay_path = overlay_dir / "agents.json"
    if not overlay_path.exists():
        raise FileNotFoundError(
            f"Overlay file not found: {overlay_path}. Run config_apply first."
        )

    data = json.loads(overlay_path.read_text())
    _set_nested(data, section, key, value)
    overlay_path.write_text(json.dumps(data, indent=2) + "\n")


def config_restore(workspace: Path, ref: str, overlay_dir: Path) -> dict:
    """Restore overlay files from a grip commit's config/ subtree.

    This is an exact restore: files in the overlay directory that are not in
    the snapshot are deleted (JSON files only; non-JSON files are preserved).
    """
    from python_cli.grip import _grip_git

    proc = _grip_git(workspace, "ls-tree", f"{ref}:config")
    has_config = proc.returncode == 0

    overlay_dir.mkdir(parents=True, exist_ok=True)
    restored: dict[str, str] = {}
    snapshot_names: set[str] = set()
    has_prompts_tree = False

    if has_config:
        for line in proc.stdout.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            meta, name = parts[0], parts[1]
            obj_type = meta.split()[1]

            if obj_type == "blob":
                snapshot_names.add(name)
                blob = _grip_git(workspace, "show", f"{ref}:config/{name}")
                if blob.returncode == 0:
                    (overlay_dir / name).write_text(blob.stdout)
                    restored[name] = "restored"
            elif obj_type == "tree" and name == "prompts":
                has_prompts_tree = True
                _restore_prompts(workspace, ref, overlay_dir)

    for f in overlay_dir.glob("*.json"):
        if f.name not in snapshot_names:
            f.unlink()

    prompts_dir = overlay_dir / "prompts"
    if prompts_dir.is_dir():
        if not has_prompts_tree:
            for pf in prompts_dir.glob("*.json"):
                pf.unlink()
        else:
            snapshot_prompts = _snapshot_prompt_names(workspace, ref)
            for pf in prompts_dir.glob("*.json"):
                if pf.name not in snapshot_prompts:
                    pf.unlink()

    return restored


def _snapshot_prompt_names(workspace: Path, ref: str) -> set[str]:
    """Get the set of prompt filenames in a grip commit's config/prompts/ subtree."""
    from python_cli.grip import _grip_git

    proc = _grip_git(workspace, "ls-tree", f"{ref}:config/prompts")
    if proc.returncode != 0:
        return set()
    names: set[str] = set()
    for line in proc.stdout.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            names.add(parts[1])
    return names


def _restore_prompts(workspace: Path, ref: str, overlay_dir: Path) -> None:
    from python_cli.grip import _grip_git

    prompts_dir = overlay_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)

    proc = _grip_git(workspace, "ls-tree", f"{ref}:config/prompts")
    if proc.returncode != 0:
        return

    for line in proc.stdout.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name = parts[1]
        blob = _grip_git(workspace, "show", f"{ref}:config/prompts/{name}")
        if blob.returncode == 0:
            (prompts_dir / name).write_text(blob.stdout)
