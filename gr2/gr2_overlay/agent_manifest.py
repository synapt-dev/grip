"""Agent manifest reader: YAML frontmatter from COMPOSE.md with base-wins precedence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class AgentManifestValidationError(Exception):
    pass


@dataclass(frozen=True)
class AgentManifest:
    description: str
    language: str
    build: str
    test: str
    lint: str | None = None
    format: str | None = None
    source_path: Path | None = None
    source_kind: str = "overlay"
    repo_name: str | None = None


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---", re.DOTALL)

_STRING_FIELDS = ("description", "language", "build", "test", "lint", "format")


def read_effective_agent_manifest(
    *,
    repo_root: Path,
    overlay_root: Path,
) -> AgentManifest:
    base_compose = repo_root / "COMPOSE.md"
    overlay_compose = overlay_root / "COMPOSE.md"

    if base_compose.exists():
        raw = _extract_agent_block(base_compose)
        if raw is not None:
            _validate(raw, base_compose)
            return _build_manifest(raw, source_path=base_compose, source_kind="base")

    if overlay_compose.exists():
        raw = _extract_agent_block(overlay_compose)
        if raw is not None:
            _validate(raw, overlay_compose)
            return _build_manifest(raw, source_path=overlay_compose, source_kind="overlay")

    raise AgentManifestValidationError("No valid COMPOSE.md with agent frontmatter found")


def read_workspace_repo_agent_manifest(
    *,
    workspace_root: Path,
    repo_name: str,
    overlays_root: Path,
) -> AgentManifest:
    repo_root = _resolve_repo_root(workspace_root, repo_name)
    overlay_root = overlays_root / repo_name

    manifest = read_effective_agent_manifest(
        repo_root=repo_root,
        overlay_root=overlay_root,
    )

    return AgentManifest(
        description=manifest.description,
        language=manifest.language,
        build=manifest.build,
        test=manifest.test,
        lint=manifest.lint,
        format=manifest.format,
        source_path=manifest.source_path,
        source_kind=manifest.source_kind,
        repo_name=repo_name,
    )


def _resolve_repo_root(workspace_root: Path, repo_name: str) -> Path:
    """Resolve repo root from gripspace.yml manifest, falling back to reference/<name>."""
    manifest_path = workspace_root / ".gitgrip" / "spaces" / "main" / "gripspace.yml"
    if manifest_path.exists():
        manifest = yaml.safe_load(manifest_path.read_text()) or {}
        repos = manifest.get("repos", {}) or {}
        repo_entry = repos.get(repo_name, {}) or {}
        repo_path = str(repo_entry.get("path", "")).strip()
        if repo_path:
            normalized = repo_path[2:] if repo_path.startswith("./") else repo_path
            resolved = workspace_root / normalized
            if resolved.is_dir():
                return resolved
    return workspace_root / "reference" / repo_name


def _extract_agent_block(compose_path: Path) -> dict[str, Any] | None:
    text = compose_path.read_text()
    m = _FRONTMATTER_RE.match(text)
    if m is None:
        return None
    frontmatter = yaml.safe_load(m.group(1))
    if not isinstance(frontmatter, dict):
        return None
    return frontmatter.get("agent")


def _validate(raw: Any, source: Path) -> None:
    if not isinstance(raw, dict):
        raise AgentManifestValidationError(f"agent block in {source} is not a mapping")
    for field in _STRING_FIELDS:
        value = raw.get(field)
        if value is not None and not isinstance(value, str):
            raise AgentManifestValidationError(
                f"Field '{field}' in {source} must be a string, got {type(value).__name__}"
            )


def _build_manifest(
    raw: dict[str, Any],
    *,
    source_path: Path,
    source_kind: str,
) -> AgentManifest:
    return AgentManifest(
        description=raw.get("description", ""),
        language=raw.get("language", ""),
        build=raw.get("build", ""),
        test=raw.get("test", ""),
        lint=raw.get("lint"),
        format=raw.get("format"),
        source_path=source_path,
        source_kind=source_kind,
    )
