"""TDD tests for Phase 1: base + overlay configuration model.

Tests define the interface contract for:
  - config_apply: materialize TOML base into JSON overlay with _base_sha
  - config_show: overlay-first read resolution with base fallback
  - config_restore: roll back overlay to grip commit snapshot
  - overlay_write: write to overlay with policy enforcement
  - Per-agent prompt overlays
  - _base_sha referential integrity
  - Policy stubs: FreeWritePolicy, OwnWriteOnlyPolicy
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from python_cli.config import (
    BaseStaleError,
    FreeWritePolicy,
    OwnWriteOnlyPolicy,
    PolicyViolationError,
    config_apply,
    config_restore,
    config_show,
    overlay_write,
)
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
worktree = "main"
loop_interval = "1m"

[agents.apollo]
role = "Rust implementation / gitgrip"
model = "claude-opus-4-6"
worktree = "synapt-dev"
loop_interval = "5m"
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Workspace with a TOML base config and overlay directory."""
    ws = tmp_path / "ws"
    ws.mkdir()
    config_dir = ws / "config"
    config_dir.mkdir()
    (config_dir / "agents.toml").write_text(SAMPLE_TOML)
    (config_dir / "overlay").mkdir()
    return ws


@pytest.fixture
def applied_workspace(workspace: Path) -> Path:
    """Workspace with config_apply already run."""
    config_apply(
        base_path=workspace / "config" / "agents.toml",
        overlay_dir=workspace / "config" / "overlay",
    )
    return workspace


@pytest.fixture
def grip_workspace(workspace: Path) -> Path:
    """Workspace with .grip/ repo and a sample git repo for snapshotting."""
    repo = workspace / "recall"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@test.com")
    git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("# recall\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "init")
    git(repo, "remote", "add", "origin", "https://github.com/synapt-dev/recall")
    grip_init(workspace)
    return workspace


# ---------------------------------------------------------------------------
# config_apply
# ---------------------------------------------------------------------------


class TestConfigApply:
    def test_creates_overlay_json(self, workspace: Path) -> None:
        config_apply(
            base_path=workspace / "config" / "agents.toml",
            overlay_dir=workspace / "config" / "overlay",
        )
        overlay = workspace / "config" / "overlay" / "agents.json"
        assert overlay.exists()

    def test_overlay_contains_base_data(self, workspace: Path) -> None:
        config_apply(
            base_path=workspace / "config" / "agents.toml",
            overlay_dir=workspace / "config" / "overlay",
        )
        overlay = json.loads(
            (workspace / "config" / "overlay" / "agents.json").read_text()
        )
        assert overlay["spawn"]["session_name"] == "synapt"
        assert overlay["agents"]["opus"]["role"] == "CEO / product design"

    def test_overlay_has_base_sha(self, workspace: Path) -> None:
        config_apply(
            base_path=workspace / "config" / "agents.toml",
            overlay_dir=workspace / "config" / "overlay",
        )
        overlay = json.loads(
            (workspace / "config" / "overlay" / "agents.json").read_text()
        )
        assert "_base_sha" in overlay
        expected = hashlib.sha256(SAMPLE_TOML.encode()).hexdigest()
        assert overlay["_base_sha"] == expected

    def test_returns_applied_config(self, workspace: Path) -> None:
        result = config_apply(
            base_path=workspace / "config" / "agents.toml",
            overlay_dir=workspace / "config" / "overlay",
        )
        assert isinstance(result, dict)
        assert "agents" in result
        assert "spawn" in result

    def test_idempotent(self, workspace: Path) -> None:
        overlay_dir = workspace / "config" / "overlay"
        r1 = config_apply(
            base_path=workspace / "config" / "agents.toml",
            overlay_dir=overlay_dir,
        )
        r2 = config_apply(
            base_path=workspace / "config" / "agents.toml",
            overlay_dir=overlay_dir,
        )
        assert r1 == r2


# ---------------------------------------------------------------------------
# config_show
# ---------------------------------------------------------------------------


class TestConfigShow:
    def test_returns_full_merged_config(self, applied_workspace: Path) -> None:
        result = config_show(
            base_path=applied_workspace / "config" / "agents.toml",
            overlay_dir=applied_workspace / "config" / "overlay",
        )
        assert "spawn" in result
        assert "agents" in result

    def test_base_values_present(self, applied_workspace: Path) -> None:
        result = config_show(
            base_path=applied_workspace / "config" / "agents.toml",
            overlay_dir=applied_workspace / "config" / "overlay",
        )
        assert result["agents"]["opus"]["model"] == "claude-opus-4-6"

    def test_overlay_overrides_base(self, applied_workspace: Path) -> None:
        overlay_path = applied_workspace / "config" / "overlay" / "agents.json"
        overlay = json.loads(overlay_path.read_text())
        overlay["agents"]["opus"]["model"] = "claude-opus-4-7"
        overlay_path.write_text(json.dumps(overlay))

        result = config_show(
            base_path=applied_workspace / "config" / "agents.toml",
            overlay_dir=applied_workspace / "config" / "overlay",
        )
        assert result["agents"]["opus"]["model"] == "claude-opus-4-7"

    def test_base_fallback_for_missing_overlay_key(self, applied_workspace: Path) -> None:
        overlay_path = applied_workspace / "config" / "overlay" / "agents.json"
        overlay = json.loads(overlay_path.read_text())
        del overlay["agents"]["opus"]["worktree"]
        overlay_path.write_text(json.dumps(overlay))

        result = config_show(
            base_path=applied_workspace / "config" / "agents.toml",
            overlay_dir=applied_workspace / "config" / "overlay",
        )
        assert result["agents"]["opus"]["worktree"] == "main"

    def test_dotted_key_access(self, applied_workspace: Path) -> None:
        result = config_show(
            base_path=applied_workspace / "config" / "agents.toml",
            overlay_dir=applied_workspace / "config" / "overlay",
            key="agents.opus.role",
        )
        assert result == "CEO / product design"

    def test_dotted_key_returns_subtree(self, applied_workspace: Path) -> None:
        result = config_show(
            base_path=applied_workspace / "config" / "agents.toml",
            overlay_dir=applied_workspace / "config" / "overlay",
            key="agents.opus",
        )
        assert isinstance(result, dict)
        assert "role" in result
        assert "model" in result

    def test_missing_key_returns_none(self, applied_workspace: Path) -> None:
        result = config_show(
            base_path=applied_workspace / "config" / "agents.toml",
            overlay_dir=applied_workspace / "config" / "overlay",
            key="agents.nonexistent.field",
        )
        assert result is None

    def test_no_overlay_file_falls_back_to_base(self, workspace: Path) -> None:
        result = config_show(
            base_path=workspace / "config" / "agents.toml",
            overlay_dir=workspace / "config" / "overlay",
        )
        assert result["spawn"]["session_name"] == "synapt"


# ---------------------------------------------------------------------------
# overlay_write
# ---------------------------------------------------------------------------


class TestOverlayWrite:
    def test_writes_value_to_overlay(self, applied_workspace: Path) -> None:
        overlay_dir = applied_workspace / "config" / "overlay"
        overlay_write(overlay_dir, "agents.opus", "model", "claude-opus-4-7")

        overlay = json.loads((overlay_dir / "agents.json").read_text())
        assert overlay["agents"]["opus"]["model"] == "claude-opus-4-7"

    def test_preserves_existing_overlay_values(self, applied_workspace: Path) -> None:
        overlay_dir = applied_workspace / "config" / "overlay"
        overlay_write(overlay_dir, "agents.opus", "model", "claude-opus-4-7")

        overlay = json.loads((overlay_dir / "agents.json").read_text())
        assert overlay["agents"]["opus"]["role"] == "CEO / product design"
        assert overlay["spawn"]["session_name"] == "synapt"

    def test_creates_new_section(self, applied_workspace: Path) -> None:
        overlay_dir = applied_workspace / "config" / "overlay"
        overlay_write(overlay_dir, "agents.sentinel", "model", "gpt-5.4")

        overlay = json.loads((overlay_dir / "agents.json").read_text())
        assert overlay["agents"]["sentinel"]["model"] == "gpt-5.4"

    def test_preserves_base_sha(self, applied_workspace: Path) -> None:
        overlay_dir = applied_workspace / "config" / "overlay"
        original = json.loads((overlay_dir / "agents.json").read_text())
        original_sha = original["_base_sha"]

        overlay_write(overlay_dir, "agents.opus", "model", "claude-opus-4-7")

        overlay = json.loads((overlay_dir / "agents.json").read_text())
        assert overlay["_base_sha"] == original_sha


# ---------------------------------------------------------------------------
# Per-agent prompt overlays
# ---------------------------------------------------------------------------


class TestPromptOverlays:
    def test_write_agent_prompt(self, applied_workspace: Path) -> None:
        overlay_dir = applied_workspace / "config" / "overlay"
        overlay_write(
            overlay_dir,
            "prompts.opus",
            "system_prompt",
            "You are the CEO.",
            prompt_overlay=True,
        )
        prompt_file = overlay_dir / "prompts" / "opus.json"
        assert prompt_file.exists()

    def test_read_agent_prompt(self, applied_workspace: Path) -> None:
        overlay_dir = applied_workspace / "config" / "overlay"
        overlay_write(
            overlay_dir,
            "prompts.opus",
            "system_prompt",
            "You are the CEO.",
            prompt_overlay=True,
        )
        result = config_show(
            base_path=applied_workspace / "config" / "agents.toml",
            overlay_dir=overlay_dir,
            key="prompts.opus.system_prompt",
        )
        assert result == "You are the CEO."

    def test_separate_files_per_agent(self, applied_workspace: Path) -> None:
        overlay_dir = applied_workspace / "config" / "overlay"
        overlay_write(
            overlay_dir, "prompts.opus", "hint", "product focus", prompt_overlay=True
        )
        overlay_write(
            overlay_dir, "prompts.apollo", "hint", "rust focus", prompt_overlay=True
        )
        assert (overlay_dir / "prompts" / "opus.json").exists()
        assert (overlay_dir / "prompts" / "apollo.json").exists()

        opus_data = json.loads((overlay_dir / "prompts" / "opus.json").read_text())
        apollo_data = json.loads((overlay_dir / "prompts" / "apollo.json").read_text())
        assert opus_data["hint"] == "product focus"
        assert apollo_data["hint"] == "rust focus"


# ---------------------------------------------------------------------------
# _base_sha referential integrity
# ---------------------------------------------------------------------------


class TestBaseShaIntegrity:
    def test_stale_overlay_detected(self, applied_workspace: Path) -> None:
        base_path = applied_workspace / "config" / "agents.toml"
        overlay_dir = applied_workspace / "config" / "overlay"

        base_path.write_text(SAMPLE_TOML + '\n[agents.sentinel]\nrole = "QA"\n')

        with pytest.raises(BaseStaleError):
            config_show(base_path, overlay_dir, strict=True)

    def test_stale_overlay_allowed_in_non_strict_mode(
        self, applied_workspace: Path
    ) -> None:
        base_path = applied_workspace / "config" / "agents.toml"
        overlay_dir = applied_workspace / "config" / "overlay"

        base_path.write_text(SAMPLE_TOML + '\n[agents.sentinel]\nrole = "QA"\n')

        result = config_show(base_path, overlay_dir)
        assert "agents" in result

    def test_reapply_updates_base_sha(self, applied_workspace: Path) -> None:
        base_path = applied_workspace / "config" / "agents.toml"
        overlay_dir = applied_workspace / "config" / "overlay"

        new_toml = SAMPLE_TOML + '\n[agents.sentinel]\nrole = "QA"\n'
        base_path.write_text(new_toml)
        config_apply(base_path, overlay_dir)

        overlay = json.loads((overlay_dir / "agents.json").read_text())
        expected = hashlib.sha256(new_toml.encode()).hexdigest()
        assert overlay["_base_sha"] == expected

    def test_reapply_preserves_overlay_modifications(
        self, applied_workspace: Path
    ) -> None:
        overlay_dir = applied_workspace / "config" / "overlay"
        overlay_write(overlay_dir, "agents.opus", "model", "claude-opus-4-7")

        base_path = applied_workspace / "config" / "agents.toml"
        new_toml = SAMPLE_TOML + '\n[agents.sentinel]\nrole = "QA"\n'
        base_path.write_text(new_toml)
        config_apply(base_path, overlay_dir)

        overlay = json.loads((overlay_dir / "agents.json").read_text())
        assert overlay["agents"]["opus"]["model"] == "claude-opus-4-7"
        assert overlay["agents"]["sentinel"]["role"] == "QA"


# ---------------------------------------------------------------------------
# config_restore
# ---------------------------------------------------------------------------


class TestConfigRestore:
    def test_restores_overlay_from_grip_commit(self, grip_workspace: Path) -> None:
        base_path = grip_workspace / "config" / "agents.toml"
        overlay_dir = grip_workspace / "config" / "overlay"
        config_apply(base_path, overlay_dir)

        snap_sha = grip_snapshot(
            grip_workspace,
            repos={"recall": grip_workspace / "recall"},
            overlay_dir=overlay_dir,
        )

        overlay_write(overlay_dir, "agents.opus", "model", "claude-opus-4-7")

        config_restore(grip_workspace, snap_sha, overlay_dir)

        overlay = json.loads((overlay_dir / "agents.json").read_text())
        assert overlay["agents"]["opus"]["model"] == "claude-opus-4-6"

    def test_restores_prompt_overlays(self, grip_workspace: Path) -> None:
        base_path = grip_workspace / "config" / "agents.toml"
        overlay_dir = grip_workspace / "config" / "overlay"
        config_apply(base_path, overlay_dir)
        overlay_write(
            overlay_dir, "prompts.opus", "hint", "original hint", prompt_overlay=True
        )

        snap_sha = grip_snapshot(
            grip_workspace,
            repos={"recall": grip_workspace / "recall"},
            overlay_dir=overlay_dir,
        )

        overlay_write(
            overlay_dir, "prompts.opus", "hint", "changed hint", prompt_overlay=True
        )

        config_restore(grip_workspace, snap_sha, overlay_dir)

        prompt = json.loads((overlay_dir / "prompts" / "opus.json").read_text())
        assert prompt["hint"] == "original hint"

    def test_restore_to_older_snapshot(self, grip_workspace: Path) -> None:
        base_path = grip_workspace / "config" / "agents.toml"
        overlay_dir = grip_workspace / "config" / "overlay"
        config_apply(base_path, overlay_dir)

        sha1 = grip_snapshot(
            grip_workspace,
            repos={"recall": grip_workspace / "recall"},
            overlay_dir=overlay_dir,
        )

        overlay_write(overlay_dir, "agents.opus", "model", "claude-opus-4-7")
        (grip_workspace / "recall" / "new.txt").write_text("change")
        git(grip_workspace / "recall", "add", ".")
        git(grip_workspace / "recall", "commit", "-m", "second")

        grip_snapshot(
            grip_workspace,
            repos={"recall": grip_workspace / "recall"},
            overlay_dir=overlay_dir,
        )

        config_restore(grip_workspace, sha1, overlay_dir)
        overlay = json.loads((overlay_dir / "agents.json").read_text())
        assert overlay["agents"]["opus"]["model"] == "claude-opus-4-6"


# ---------------------------------------------------------------------------
# Policy stubs
# ---------------------------------------------------------------------------


class TestPolicies:
    def test_free_write_allows_any_write(self) -> None:
        policy = FreeWritePolicy()
        assert policy.can_write(agent="opus", section="agents.apollo", key="model")
        assert policy.can_write(agent="apollo", section="spawn", key="channel")

    def test_own_write_only_allows_own_section(self) -> None:
        policy = OwnWriteOnlyPolicy()
        assert policy.can_write(agent="opus", section="agents.opus", key="model")
        assert policy.can_write(agent="apollo", section="agents.apollo", key="model")
        assert policy.can_write(agent="opus", section="prompts.opus", key="hint")

    def test_own_write_only_blocks_other_section(self) -> None:
        policy = OwnWriteOnlyPolicy()
        assert not policy.can_write(agent="opus", section="agents.apollo", key="model")
        assert not policy.can_write(agent="apollo", section="agents.opus", key="role")
        assert not policy.can_write(agent="opus", section="prompts.apollo", key="hint")

    def test_own_write_only_allows_shared_sections(self) -> None:
        policy = OwnWriteOnlyPolicy()
        assert policy.can_write(agent="opus", section="spawn", key="channel")

    def test_overlay_write_with_policy_enforcement(
        self, applied_workspace: Path
    ) -> None:
        overlay_dir = applied_workspace / "config" / "overlay"
        policy = OwnWriteOnlyPolicy()

        overlay_write(
            overlay_dir,
            "agents.opus",
            "model",
            "claude-opus-4-7",
            agent="opus",
            policy=policy,
        )
        overlay = json.loads((overlay_dir / "agents.json").read_text())
        assert overlay["agents"]["opus"]["model"] == "claude-opus-4-7"

    def test_overlay_write_blocked_by_policy(self, applied_workspace: Path) -> None:
        overlay_dir = applied_workspace / "config" / "overlay"
        policy = OwnWriteOnlyPolicy()

        with pytest.raises(PolicyViolationError):
            overlay_write(
                overlay_dir,
                "agents.apollo",
                "model",
                "gpt-5.4",
                agent="opus",
                policy=policy,
            )
