from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from gr2_overlay.agent_manifest import (
    AgentManifest,
    AgentManifestValidationError,
    read_effective_agent_manifest,
)


def test_reads_agent_manifest_fields_from_overlay_compose_when_base_missing(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "mem0"
    overlay_root = tmp_path / "overlay"
    repo_root.mkdir()
    overlay_root.mkdir()

    _write_compose(
        overlay_root / "COMPOSE.md",
        """
        ---
        agent:
          description: "mem0 reference overlay"
          language: "python"
          build: "uv sync"
          test: "pytest"
          lint: "ruff check ."
          format: "ruff format ."
        ---
        # mem0
        """,
    )

    manifest = read_effective_agent_manifest(
        repo_root=repo_root,
        overlay_root=overlay_root,
    )

    assert manifest == AgentManifest(
        description="mem0 reference overlay",
        language="python",
        build="uv sync",
        test="pytest",
        lint="ruff check .",
        format="ruff format .",
        source_path=overlay_root / "COMPOSE.md",
        source_kind="overlay",
    )


def test_base_repo_compose_takes_precedence_over_overlay_compose(tmp_path: Path) -> None:
    repo_root = tmp_path / "zep"
    overlay_root = tmp_path / "overlay"
    repo_root.mkdir()
    overlay_root.mkdir()

    _write_compose(
        repo_root / "COMPOSE.md",
        """
        ---
        agent:
          description: "zep upstream"
          language: "python"
          build: "pip install -e ."
          test: "pytest tests/unit"
        ---
        # zep
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
        # zep overlay
        """,
    )

    manifest = read_effective_agent_manifest(
        repo_root=repo_root,
        overlay_root=overlay_root,
    )

    assert manifest.source_path == repo_root / "COMPOSE.md"
    assert manifest.source_kind == "base"
    assert manifest.description == "zep upstream"
    assert manifest.build == "pip install -e ."
    assert manifest.test == "pytest tests/unit"


@pytest.mark.parametrize("source_kind", ["base", "overlay"])
def test_invalid_agent_manifest_is_rejected_regardless_of_source(
    tmp_path: Path,
    source_kind: str,
) -> None:
    repo_root = tmp_path / "hindsight"
    overlay_root = tmp_path / "overlay"
    repo_root.mkdir()
    overlay_root.mkdir()

    target_root = repo_root if source_kind == "base" else overlay_root
    _write_compose(
        target_root / "COMPOSE.md",
        """
        ---
        agent:
          description: "invalid manifest"
          language: 7
          build: ["pytest"]
        ---
        # hindsight
        """,
    )

    with pytest.raises(AgentManifestValidationError):
        read_effective_agent_manifest(
            repo_root=repo_root,
            overlay_root=overlay_root,
        )


def test_invalid_base_manifest_does_not_fall_back_to_overlay(tmp_path: Path) -> None:
    repo_root = tmp_path / "memobase"
    overlay_root = tmp_path / "overlay"
    repo_root.mkdir()
    overlay_root.mkdir()

    _write_compose(
        repo_root / "COMPOSE.md",
        """
        ---
        agent:
          description: "broken upstream manifest"
          test: ["pytest"]
        ---
        # memobase
        """,
    )
    _write_compose(
        overlay_root / "COMPOSE.md",
        """
        ---
        agent:
          description: "valid overlay fallback"
          language: "python"
          build: "uv sync"
          test: "pytest"
        ---
        # memobase overlay
        """,
    )

    with pytest.raises(AgentManifestValidationError):
        read_effective_agent_manifest(
            repo_root=repo_root,
            overlay_root=overlay_root,
        )


def _write_compose(path: Path, contents: str) -> None:
    path.write_text(dedent(contents).lstrip())
