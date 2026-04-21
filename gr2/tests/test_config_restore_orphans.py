"""TDD tests for Story 7: config_restore orphan overlay deletion.

Bug: config_restore only writes files found in the grip commit's config/
subtree. It never deletes files that exist locally but are absent from the
snapshot. Example: if you add prompts/atlas.json after a snapshot and then
restore to that snapshot, atlas.json survives the restore.

Expected behavior: after config_restore, the overlay directory should be an
exact mirror of the snapshot — no extra files.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from python_cli.config import config_apply, config_restore, overlay_write
from python_cli.gitops import git
from python_cli.grip import grip_init, grip_snapshot


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_TOML = """\
[spawn]
session_name = "synapt"
channel = "dev"

[agents.opus]
role = "CEO / product design"
model = "claude-opus-4-6"
"""


@pytest.fixture
def grip_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    config_dir = ws / "config"
    config_dir.mkdir()
    (config_dir / "agents.toml").write_text(SAMPLE_TOML)
    (config_dir / "overlay").mkdir()

    repo = ws / "recall"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@test.com")
    git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("# recall\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "init")
    git(repo, "remote", "add", "origin", "https://github.com/synapt-dev/recall")
    grip_init(ws)
    return ws


# ---------------------------------------------------------------------------
# Tests: overlay file orphan deletion
# ---------------------------------------------------------------------------


class TestRestoreDeletesOrphanOverlays:
    def test_extra_overlay_file_deleted_on_restore(self, grip_workspace: Path) -> None:
        """An overlay JSON file added after the snapshot should be deleted on restore."""
        base_path = grip_workspace / "config" / "agents.toml"
        overlay_dir = grip_workspace / "config" / "overlay"
        config_apply(base_path, overlay_dir)

        snap_sha = grip_snapshot(
            grip_workspace,
            repos={"recall": grip_workspace / "recall"},
            overlay_dir=overlay_dir,
        )

        # Add a new overlay file AFTER the snapshot
        extra_file = overlay_dir / "extra_config.json"
        extra_file.write_text(json.dumps({"rogue": True}))

        config_restore(grip_workspace, snap_sha, overlay_dir)

        assert not extra_file.exists(), (
            "extra_config.json should be deleted by restore "
            "(it wasn't in the snapshot)"
        )

    def test_extra_prompt_file_deleted_on_restore(self, grip_workspace: Path) -> None:
        """A prompt overlay added after the snapshot should be deleted on restore."""
        base_path = grip_workspace / "config" / "agents.toml"
        overlay_dir = grip_workspace / "config" / "overlay"
        config_apply(base_path, overlay_dir)

        overlay_write(
            overlay_dir, "prompts.opus", "hint", "original", prompt_overlay=True
        )

        snap_sha = grip_snapshot(
            grip_workspace,
            repos={"recall": grip_workspace / "recall"},
            overlay_dir=overlay_dir,
        )

        # Add a NEW agent's prompt after the snapshot
        overlay_write(
            overlay_dir, "prompts.atlas", "hint", "new agent", prompt_overlay=True
        )
        assert (overlay_dir / "prompts" / "atlas.json").exists()

        config_restore(grip_workspace, snap_sha, overlay_dir)

        assert not (overlay_dir / "prompts" / "atlas.json").exists(), (
            "prompts/atlas.json should be deleted by restore "
            "(it wasn't in the snapshot)"
        )

    def test_snapshot_prompt_files_survive_restore(self, grip_workspace: Path) -> None:
        """Prompt files that WERE in the snapshot should still exist after restore."""
        base_path = grip_workspace / "config" / "agents.toml"
        overlay_dir = grip_workspace / "config" / "overlay"
        config_apply(base_path, overlay_dir)

        overlay_write(
            overlay_dir, "prompts.opus", "hint", "keep me", prompt_overlay=True
        )

        snap_sha = grip_snapshot(
            grip_workspace,
            repos={"recall": grip_workspace / "recall"},
            overlay_dir=overlay_dir,
        )

        # Add orphan, then restore
        overlay_write(
            overlay_dir, "prompts.atlas", "hint", "delete me", prompt_overlay=True
        )
        config_restore(grip_workspace, snap_sha, overlay_dir)

        assert (overlay_dir / "prompts" / "opus.json").exists()
        prompt = json.loads((overlay_dir / "prompts" / "opus.json").read_text())
        assert prompt["hint"] == "keep me"

    def test_restore_to_snapshot_with_no_config_clears_overlay(
        self, grip_workspace: Path
    ) -> None:
        """Restoring to a snapshot that has no config/ subtree should clear overlays."""
        overlay_dir = grip_workspace / "config" / "overlay"

        # Snapshot WITHOUT overlay_dir (no config subtree in grip commit)
        snap_sha = grip_snapshot(
            grip_workspace,
            repos={"recall": grip_workspace / "recall"},
        )

        # Now add overlay files
        base_path = grip_workspace / "config" / "agents.toml"
        config_apply(base_path, overlay_dir)
        overlay_write(
            overlay_dir, "prompts.opus", "hint", "should vanish", prompt_overlay=True
        )

        config_restore(grip_workspace, snap_sha, overlay_dir)

        json_files = list(overlay_dir.glob("*.json"))
        prompt_files = list((overlay_dir / "prompts").glob("*.json")) if (overlay_dir / "prompts").is_dir() else []
        assert json_files == [], f"Expected no overlay JSONs, found {json_files}"
        assert prompt_files == [], f"Expected no prompt JSONs, found {prompt_files}"

    def test_non_json_files_in_overlay_untouched(self, grip_workspace: Path) -> None:
        """Non-JSON files (e.g. .gitkeep) should not be deleted by restore."""
        base_path = grip_workspace / "config" / "agents.toml"
        overlay_dir = grip_workspace / "config" / "overlay"
        config_apply(base_path, overlay_dir)

        snap_sha = grip_snapshot(
            grip_workspace,
            repos={"recall": grip_workspace / "recall"},
            overlay_dir=overlay_dir,
        )

        gitkeep = overlay_dir / ".gitkeep"
        gitkeep.write_text("")

        config_restore(grip_workspace, snap_sha, overlay_dir)

        assert gitkeep.exists(), ".gitkeep should survive restore (not a JSON overlay)"
