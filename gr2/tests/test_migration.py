"""TDD specs for grip#563: gr1 to gr2 migration commands.

Tests the full migration flow: detect -> migrate -> validate -> apply,
plus coexistence state awareness and the workspace status command.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from gr2.python_cli.migration import (
    compile_gr1_to_workspace_spec,
    detect_gr1_workspace,
    migrate_gr1_workspace,
    render_workspace_spec,
    workspace_status,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_gr1_workspace(root: Path) -> None:
    """Create a realistic gr1 workspace on disk."""
    gitgrip = root / ".gitgrip"
    (gitgrip / "spaces" / "main").mkdir(parents=True)
    (gitgrip / "spaces" / "main" / "gripspace.yml").write_text(
        yaml.dump({
            "version": 2,
            "manifest": {"url": "git@github.com:synapt-dev/synapt-gripspace.git"},
            "repos": {
                "grip": {
                    "url": "git@github.com:synapt-dev/grip.git",
                    "path": "./gitgrip",
                    "revision": "main",
                },
                "synapt": {
                    "url": "git@github.com:synapt-dev/synapt.git",
                    "path": "./synapt",
                    "revision": "main",
                },
                "mem0": {
                    "url": "https://github.com/mem0ai/mem0.git",
                    "path": "reference/mem0",
                    "default_branch": "main",
                    "reference": True,
                },
            },
        })
    )
    (gitgrip / "agents.toml").write_text(
        "[agents.atlas]\n"
        'worktree = "main"\n'
        'channel = "dev"\n\n'
        "[agents.apollo]\n"
        'worktree = "main"\n'
        'channel = "dev"\n'
    )
    (gitgrip / "state.json").write_text(
        json.dumps({"branchToPr": {"feat/auth": 123}})
    )
    (gitgrip / "sync-state.json").write_text(
        json.dumps({"timestamp": "2026-04-14T12:00:00Z"})
    )


@pytest.fixture
def gr1_workspace(tmp_path: Path) -> Path:
    _write_gr1_workspace(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# detect-gr1
# ---------------------------------------------------------------------------

class TestDetectGr1:
    def test_detects_valid_gr1_workspace(self, gr1_workspace: Path) -> None:
        result = detect_gr1_workspace(gr1_workspace)
        assert result["detected"] is True
        assert result["repo_count"] == 3
        assert set(result["agents"]) == {"apollo", "atlas"}

    def test_classifies_reference_repos(self, gr1_workspace: Path) -> None:
        result = detect_gr1_workspace(gr1_workspace)
        assert result["reference_repos"] == ["mem0"]
        assert "mem0" not in result["writable_repos"]

    def test_returns_false_for_non_gr1(self, tmp_path: Path) -> None:
        result = detect_gr1_workspace(tmp_path)
        assert result["detected"] is False

    def test_includes_state_files(self, gr1_workspace: Path) -> None:
        result = detect_gr1_workspace(gr1_workspace)
        assert "state_json" in result["state_files"]
        assert "sync_state_json" in result["state_files"]


# ---------------------------------------------------------------------------
# compile + migrate
# ---------------------------------------------------------------------------

class TestCompileGr1:
    def test_generates_spec_with_repos_and_units(self, gr1_workspace: Path) -> None:
        manifest = yaml.safe_load(
            (gr1_workspace / ".gitgrip" / "spaces" / "main" / "gripspace.yml").read_text()
        )
        import tomllib
        with (gr1_workspace / ".gitgrip" / "agents.toml").open("rb") as fh:
            agents_doc = tomllib.load(fh)
        compiled = compile_gr1_to_workspace_spec(gr1_workspace, manifest, agents_doc)
        assert len(compiled["repos"]) == 3
        assert len(compiled["units"]) == 2
        unit_names = {u["name"] for u in compiled["units"]}
        assert unit_names == {"apollo", "atlas"}

    def test_reference_repos_marked(self, gr1_workspace: Path) -> None:
        manifest = yaml.safe_load(
            (gr1_workspace / ".gitgrip" / "spaces" / "main" / "gripspace.yml").read_text()
        )
        compiled = compile_gr1_to_workspace_spec(gr1_workspace, manifest, {})
        mem0 = next(r for r in compiled["repos"] if r["name"] == "mem0")
        assert mem0.get("reference") is True

    def test_writable_repos_only_in_units(self, gr1_workspace: Path) -> None:
        manifest = yaml.safe_load(
            (gr1_workspace / ".gitgrip" / "spaces" / "main" / "gripspace.yml").read_text()
        )
        import tomllib
        with (gr1_workspace / ".gitgrip" / "agents.toml").open("rb") as fh:
            agents_doc = tomllib.load(fh)
        compiled = compile_gr1_to_workspace_spec(gr1_workspace, manifest, agents_doc)
        for unit in compiled["units"]:
            assert "mem0" not in unit["repos"]


class TestMigrateGr1:
    def test_creates_grip_dir_and_spec(self, gr1_workspace: Path) -> None:
        result = migrate_gr1_workspace(gr1_workspace)
        assert (gr1_workspace / ".grip" / "workspace_spec.toml").exists()
        assert result["repo_count"] == 3
        assert result["unit_count"] == 2

    def test_preserves_gr1_state_snapshots(self, gr1_workspace: Path) -> None:
        result = migrate_gr1_workspace(gr1_workspace)
        migration_dir = gr1_workspace / ".grip" / "migrations" / "gr1"
        assert migration_dir.exists()
        assert (migration_dir / "state.json").exists()
        assert (migration_dir / "sync-state.json").exists()
        assert (migration_dir / "migration-summary.json").exists()

    def test_does_not_modify_gr1_manifest(self, gr1_workspace: Path) -> None:
        manifest_path = gr1_workspace / ".gitgrip" / "spaces" / "main" / "gripspace.yml"
        before = manifest_path.read_text()
        migrate_gr1_workspace(gr1_workspace)
        after = manifest_path.read_text()
        assert before == after

    def test_blocks_overwrite_without_force(self, gr1_workspace: Path) -> None:
        migrate_gr1_workspace(gr1_workspace)
        with pytest.raises(SystemExit, match="refusing to overwrite"):
            migrate_gr1_workspace(gr1_workspace)

    def test_allows_overwrite_with_force(self, gr1_workspace: Path) -> None:
        migrate_gr1_workspace(gr1_workspace)
        result = migrate_gr1_workspace(gr1_workspace, force=True)
        assert result["repo_count"] == 3

    def test_generated_spec_is_valid_toml(self, gr1_workspace: Path) -> None:
        migrate_gr1_workspace(gr1_workspace)
        spec_text = (gr1_workspace / ".grip" / "workspace_spec.toml").read_text()
        import tomllib
        parsed = tomllib.loads(spec_text)
        assert parsed["workspace_name"] == gr1_workspace.name
        assert len(parsed["repos"]) == 3
        assert len(parsed["units"]) == 2


# ---------------------------------------------------------------------------
# workspace status (new command)
# ---------------------------------------------------------------------------

class TestWorkspaceStatus:
    def test_pure_gr1_workspace(self, gr1_workspace: Path) -> None:
        status = workspace_status(gr1_workspace)
        assert status["gr1"] is True
        assert status["gr2"] is False
        assert status["coexistence"] is False
        assert status["phase"] == "gr1-only"

    def test_pure_gr2_workspace(self, tmp_path: Path) -> None:
        grip = tmp_path / ".grip"
        grip.mkdir()
        (grip / "workspace_spec.toml").write_text('workspace_name = "test"\n')
        status = workspace_status(tmp_path)
        assert status["gr1"] is False
        assert status["gr2"] is True
        assert status["coexistence"] is False
        assert status["phase"] == "gr2-only"

    def test_coexistence_after_migration(self, gr1_workspace: Path) -> None:
        migrate_gr1_workspace(gr1_workspace)
        status = workspace_status(gr1_workspace)
        assert status["gr1"] is True
        assert status["gr2"] is True
        assert status["coexistence"] is True
        assert status["phase"] == "coexistence"
        assert status["migration_snapshot"] is True

    def test_no_workspace(self, tmp_path: Path) -> None:
        status = workspace_status(tmp_path)
        assert status["gr1"] is False
        assert status["gr2"] is False
        assert status["phase"] == "none"

    def test_includes_repo_counts(self, gr1_workspace: Path) -> None:
        migrate_gr1_workspace(gr1_workspace)
        status = workspace_status(gr1_workspace)
        assert status["gr1_repo_count"] == 3
        assert status["gr2_repo_count"] == 3


# ---------------------------------------------------------------------------
# End-to-end: detect -> migrate -> validate -> apply
# ---------------------------------------------------------------------------

class TestMigrationEndToEnd:
    def test_full_flow_detect_migrate_validate(self, gr1_workspace: Path) -> None:
        """The full migration path must work without errors."""
        detection = detect_gr1_workspace(gr1_workspace)
        assert detection["detected"] is True

        migration_result = migrate_gr1_workspace(gr1_workspace)
        assert migration_result["repo_count"] == 3

        status = workspace_status(gr1_workspace)
        assert status["coexistence"] is True

        spec_text = (gr1_workspace / ".grip" / "workspace_spec.toml").read_text()
        import tomllib
        spec = tomllib.loads(spec_text)
        assert spec["workspace_name"] == gr1_workspace.name
        for unit in spec["units"]:
            assert "repos" in unit
            assert len(unit["repos"]) > 0
