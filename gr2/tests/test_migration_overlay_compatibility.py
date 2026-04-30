from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from gr2.python_cli.migration import detect_gr1_workspace, workspace_status
from gr2_overlay.agent_manifest import (
    AgentManifest,
    AgentManifestValidationError,
    read_workspace_repo_agent_manifest,
)


def test_gr1_manifest_stays_authoritative_while_overlay_supplies_missing_agent_metadata(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    overlay_root = tmp_path / "config" / "overlays" / "mem0"
    _write_gr1_workspace(workspace_root)
    _write_compose(
        overlay_root / "COMPOSE.md",
        """
        ---
        agent:
          description: "mem0 overlay manifest"
          language: "python"
          build: "uv sync"
          test: "pytest tests/"
          lint: "ruff check ."
          format: "ruff format ."
        ---
        # mem0 overlay
        """,
    )

    manifest_before = _gr1_manifest_path(workspace_root).read_text()

    agent_manifest = read_workspace_repo_agent_manifest(
        workspace_root=workspace_root,
        repo_name="mem0",
        overlays_root=tmp_path / "config" / "overlays",
    )

    assert agent_manifest == AgentManifest(
        description="mem0 overlay manifest",
        language="python",
        build="uv sync",
        test="pytest tests/",
        lint="ruff check .",
        format="ruff format .",
        source_path=overlay_root / "COMPOSE.md",
        source_kind="overlay",
        repo_name="mem0",
    )

    detection = detect_gr1_workspace(workspace_root)
    assert detection["detected"] is True
    assert detection["repo_count"] == 2
    assert detection["reference_repos"] == ["mem0"]

    status = workspace_status(workspace_root)
    assert status["phase"] == "gr1-only"
    assert status["gr1"] is True
    assert status["gr2"] is False

    assert _gr1_manifest_path(workspace_root).read_text() == manifest_before


def test_base_repo_compose_wins_over_overlay_during_gr1_to_gr2_migration(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    overlay_root = tmp_path / "config" / "overlays" / "mem0"
    _write_gr1_workspace(workspace_root)
    _write_compose(
        workspace_root / "reference" / "mem0" / "COMPOSE.md",
        """
        ---
        agent:
          description: "upstream mem0 compose"
          language: "python"
          build: "pip install -e ."
          test: "pytest tests/unit"
        ---
        # upstream mem0
        """,
    )
    _write_compose(
        overlay_root / "COMPOSE.md",
        """
        ---
        agent:
          description: "overlay fallback"
          language: "python"
          build: "uv sync"
          test: "pytest"
        ---
        # overlay mem0
        """,
    )

    agent_manifest = read_workspace_repo_agent_manifest(
        workspace_root=workspace_root,
        repo_name="mem0",
        overlays_root=tmp_path / "config" / "overlays",
    )

    assert agent_manifest.source_kind == "base"
    assert agent_manifest.source_path == workspace_root / "reference" / "mem0" / "COMPOSE.md"
    assert agent_manifest.description == "upstream mem0 compose"
    assert agent_manifest.build == "pip install -e ."
    assert agent_manifest.test == "pytest tests/unit"

    detection = detect_gr1_workspace(workspace_root)
    assert detection["reference_repos"] == ["mem0"]
    assert detection["writable_repos"] == ["grip"]


def test_invalid_overlay_manifest_blocks_read_but_not_gr1_workspace_detection(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    overlay_root = tmp_path / "config" / "overlays" / "mem0"
    _write_gr1_workspace(workspace_root)
    _write_compose(
        overlay_root / "COMPOSE.md",
        """
        ---
        agent:
          description: "broken overlay"
          language: 7
          test: ["pytest"]
        ---
        # broken
        """,
    )

    with pytest.raises(AgentManifestValidationError):
        read_workspace_repo_agent_manifest(
            workspace_root=workspace_root,
            repo_name="mem0",
            overlays_root=tmp_path / "config" / "overlays",
        )

    detection = detect_gr1_workspace(workspace_root)
    status = workspace_status(workspace_root)
    assert detection["detected"] is True
    assert detection["reference_repos"] == ["mem0"]
    assert status["phase"] == "gr1-only"


def _write_gr1_workspace(root: Path) -> None:
    gitgrip = root / ".gitgrip"
    (gitgrip / "spaces" / "main").mkdir(parents=True)
    (root / "gitgrip").mkdir()
    (root / "reference" / "mem0").mkdir(parents=True)

    manifest = {
        "version": 2,
        "repos": {
            "grip": {
                "url": "git@github.com:synapt-dev/grip.git",
                "path": "./gitgrip",
                "revision": "main",
            },
            "mem0": {
                "url": "https://github.com/mem0ai/mem0.git",
                "path": "reference/mem0",
                "default_branch": "main",
                "reference": True,
            },
        },
    }
    _gr1_manifest_path(root).write_text(yaml.dump(manifest))


def _gr1_manifest_path(root: Path) -> Path:
    return root / ".gitgrip" / "spaces" / "main" / "gripspace.yml"


def _write_compose(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(contents).lstrip())
