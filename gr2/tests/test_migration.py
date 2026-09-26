"""TDD specs for grip#563: gr1 to gr2 migration commands.

Tests the full migration flow: detect -> migrate -> validate -> apply,
plus coexistence state awareness and the workspace status command.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner
from tests.conftest import make_cli_runner

from gr2.python_cli.app import app
from gr2.python_cli import migration
from gr2.python_cli.migration import (
    bootstrap_gr1_workspace,
    compile_gr1_to_workspace_spec,
    detect_gr1_workspace,
    migrate_gr1_workspace,
    regenerate_gr1_workspace,
    rollback_gr1_workspace,
    render_status,
    render_workspace_spec,
    workspace_status,
)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Strip ANSI so a flag name in a Rich-rendered usage/error panel is a
    literal substring under forced color (CI) as well as plain (local).
    """
    return _ANSI_RE.sub("", text)


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

    def test_normalizes_safe_nested_repo_path(self, gr1_workspace: Path) -> None:
        compiled = compile_gr1_to_workspace_spec(
            gr1_workspace,
            {"repos": {"safe": {"path": "./nested//safe", "url": "https://example.invalid/safe.git"}}},
            {"agents": {"atlas": {}}},
        )

        assert compiled["repos"] == [{"name": "safe", "path": "nested/safe", "url": "https://example.invalid/safe.git"}]
        assert compiled["units"][0]["path"] == "agents/atlas/home"


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
# manifest bootstrap: compile the canonical gr1 manifest and initialize grip
# ---------------------------------------------------------------------------

class TestBootstrapGr1:
    def test_compiles_manifest_and_initializes_grip_object_store(self, gr1_workspace: Path) -> None:
        """The production bootstrap makes a usable gr2 control plane, not a hand-written spec."""
        result = bootstrap_gr1_workspace(gr1_workspace)

        assert result["status"] == "initialized"
        assert (gr1_workspace / ".grip" / ".git").is_dir()
        spec_path = gr1_workspace / ".grip" / "workspace_spec.toml"
        assert result["workspace_spec_path"] == str(spec_path)

        import tomllib
        spec = tomllib.loads(spec_path.read_text())
        assert {repo["name"] for repo in spec["repos"]} == {"grip", "synapt", "mem0"}
        assert [unit["name"] for unit in spec["units"]] == ["apollo", "atlas"]

    def test_invalid_manifest_refuses_before_creating_grip_state(self, gr1_workspace: Path) -> None:
        manifest_path = gr1_workspace / ".gitgrip" / "spaces" / "main" / "gripspace.yml"
        manifest_path.write_text("repos: [not-a-map]\n")

        with pytest.raises(SystemExit, match="repos"):
            bootstrap_gr1_workspace(gr1_workspace)

        assert not (gr1_workspace / ".grip").exists()

    @pytest.mark.parametrize(
        ("repo_name", "repo_path", "agent_name"),
        [
            ("escape", "../outside", "atlas"),
            ("escape", "/outside", "atlas"),
            ("escape", "nested/../outside", "atlas"),
            ("escape", "C:/outside", "atlas"),
            ("../repo", "safe", "atlas"),
            ("repo/child", "safe", "atlas"),
            ("escape", "safe", "../unit"),
            ("escape", "safe", "unit/child"),
            ("escape", "safe", "C:"),
        ],
    )
    def test_path_escapes_refuse_before_creating_grip_state(
        self, gr1_workspace: Path, repo_name: str, repo_path: str, agent_name: str
    ) -> None:
        manifest_path = gr1_workspace / ".gitgrip" / "spaces" / "main" / "gripspace.yml"
        manifest_path.write_text(yaml.dump({"repos": {repo_name: {"path": repo_path, "url": "https://example.invalid/escape.git"}}}))
        (gr1_workspace / ".gitgrip" / "agents.toml").write_text(f"[agents.\"{agent_name}\"]\nworktree = \"main\"\n")

        with pytest.raises(SystemExit, match="cannot compile canonical gripspace manifest"):
            bootstrap_gr1_workspace(gr1_workspace)

        assert not (gr1_workspace / ".grip").exists()

    def test_deleting_repo_path_guard_recreates_the_unsafe_store_side_effect(
        self, gr1_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mutation control: without the source guard, valid TOML alone is unsafe."""
        manifest_path = gr1_workspace / ".gitgrip" / "spaces" / "main" / "gripspace.yml"
        manifest_path.write_text(yaml.dump({"repos": {"escape": {"path": "../outside", "url": "https://example.invalid/escape.git"}}}))
        monkeypatch.setattr(migration, "_safe_workspace_relative_path", lambda value, _field: str(value))

        bootstrap_gr1_workspace(gr1_workspace)

        assert (gr1_workspace / ".grip" / ".git").is_dir()

    def test_deleting_unit_name_guard_recreates_the_unsafe_store_side_effect(
        self, gr1_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mutation control: unit validation protects the generated unit name and every
        path derived from it."""
        (gr1_workspace / ".gitgrip" / "agents.toml").write_text('[agents."../unit"]\nworktree = "main"\n')
        monkeypatch.setattr(migration, "_safe_workspace_component", lambda value, _field: str(value))

        bootstrap_gr1_workspace(gr1_workspace)

        assert (gr1_workspace / ".grip" / ".git").is_dir()

    def test_idempotent_bootstrap_preserves_compiled_bytes(self, gr1_workspace: Path) -> None:
        bootstrap_gr1_workspace(gr1_workspace)
        spec_path = gr1_workspace / ".grip" / "workspace_spec.toml"
        before = spec_path.read_bytes()

        result = bootstrap_gr1_workspace(gr1_workspace)

        assert result["status"] == "already_initialized"
        assert spec_path.read_bytes() == before

    def test_existing_empty_grip_directory_is_completed(self, gr1_workspace: Path) -> None:
        """Repair the observed partial control-plane shape without accepting corrupt git state."""
        (gr1_workspace / ".grip").mkdir()

        result = bootstrap_gr1_workspace(gr1_workspace)

        assert result["status"] == "initialized"
        assert (gr1_workspace / ".grip" / ".git").is_dir()
        assert (gr1_workspace / ".grip" / "workspace_spec.toml").is_file()

    @pytest.mark.parametrize("surface", ["grip", "git", "spec"])
    def test_symlinked_control_plane_refuses_before_external_write(
        self, gr1_workspace: Path, tmp_path: Path, surface: str
    ) -> None:
        external = tmp_path / "external"
        external.mkdir()
        grip_dir = gr1_workspace / ".grip"
        if surface == "grip":
            grip_dir.symlink_to(external, target_is_directory=True)
        elif surface == "git":
            grip_dir.mkdir()
            (grip_dir / ".git").symlink_to(external, target_is_directory=True)
        else:
            grip_dir.mkdir()
            (grip_dir / "workspace_spec.toml").symlink_to(external / "spec")

        with pytest.raises(SystemExit, match="must not be a symlink"):
            bootstrap_gr1_workspace(gr1_workspace)

        assert not (external / ".git").exists()
        assert not (external / "workspace_spec.toml").exists()

    def test_deleting_symlink_guard_recreates_external_write(
        self, gr1_workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        external = tmp_path / "external"
        external.mkdir()
        (gr1_workspace / ".grip").symlink_to(external, target_is_directory=True)
        monkeypatch.setattr(migration, "_refuse_symlink", lambda _path, _label: None)

        bootstrap_gr1_workspace(gr1_workspace)

        assert (external / ".git").is_dir()
        assert (external / "workspace_spec.toml").is_file()

    def test_publish_failure_rolls_back_new_object_store(
        self, gr1_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(migration, "_atomic_write", lambda _path, _content: (_ for _ in ()).throw(OSError("injected publish failure")))

        with pytest.raises(OSError, match="injected publish failure"):
            bootstrap_gr1_workspace(gr1_workspace)

        assert not (gr1_workspace / ".grip").exists()

    def test_publish_failure_preserves_preexisting_partial_directory(
        self, gr1_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        grip_dir = gr1_workspace / ".grip"
        grip_dir.mkdir()
        monkeypatch.setattr(migration, "_atomic_write", lambda _path, _content: (_ for _ in ()).throw(OSError("injected publish failure")))

        with pytest.raises(OSError, match="injected publish failure"):
            bootstrap_gr1_workspace(gr1_workspace)

        assert grip_dir.is_dir()
        assert not (grip_dir / ".git").exists()
        assert not (grip_dir / "workspace_spec.toml").exists()

    def test_deleting_rollback_recreates_partial_object_store(
        self, gr1_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(migration, "_atomic_write", lambda _path, _content: (_ for _ in ()).throw(OSError("injected publish failure")))
        monkeypatch.setattr(migration, "_rollback_bootstrap", lambda *_args: None)

        with pytest.raises(OSError, match="injected publish failure"):
            bootstrap_gr1_workspace(gr1_workspace)

        assert (gr1_workspace / ".grip" / ".git").is_dir()

    @pytest.mark.parametrize("preexisting_grip", [False, True])
    def test_nonzero_git_init_refuses_without_spec_and_rolls_back_created_state(
        self, gr1_workspace: Path, monkeypatch: pytest.MonkeyPatch, preexisting_grip: bool
    ) -> None:
        if preexisting_grip:
            (gr1_workspace / ".grip").mkdir()
        real_git = migration.grip.git

        def fail_init(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
            if args == ("init",):
                return subprocess.CompletedProcess(["git", *args], 1, "", "injected init failure")
            return real_git(cwd, *args)

        monkeypatch.setattr(migration.grip, "git", fail_init)
        with pytest.raises(SystemExit, match="git init failed: injected init failure"):
            bootstrap_gr1_workspace(gr1_workspace)

        assert not (gr1_workspace / ".grip" / "workspace_spec.toml").exists()
        assert (gr1_workspace / ".grip").exists() is preexisting_grip
        assert not (gr1_workspace / ".grip" / ".git").exists()

    @pytest.mark.parametrize("config_key", ["user.email", "user.name"])
    @pytest.mark.parametrize("preexisting_grip", [False, True])
    def test_nonzero_git_config_refuses_and_rolls_back_new_store(
        self, gr1_workspace: Path, monkeypatch: pytest.MonkeyPatch, config_key: str, preexisting_grip: bool
    ) -> None:
        if preexisting_grip:
            (gr1_workspace / ".grip").mkdir()
        real_git = migration.grip.git

        def fail_config(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
            if args[:2] == ("config", config_key):
                return subprocess.CompletedProcess(["git", *args], 1, "", f"injected {config_key} failure")
            return real_git(cwd, *args)

        monkeypatch.setattr(migration.grip, "git", fail_config)
        with pytest.raises(SystemExit, match=f"git config {config_key} failed"):
            bootstrap_gr1_workspace(gr1_workspace)

        assert not (gr1_workspace / ".grip" / "workspace_spec.toml").exists()
        assert (gr1_workspace / ".grip").exists() is preexisting_grip
        assert not (gr1_workspace / ".grip" / ".git").exists()

    def test_deleting_git_process_guard_recreates_spec_without_store(
        self, gr1_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_git = migration.grip.git

        def missing_init_but_plausible_probe(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
            if args == ("init",):
                return subprocess.CompletedProcess(["git", *args], 1, "", "injected init failure")
            if args == ("rev-parse", "--is-inside-work-tree"):
                return subprocess.CompletedProcess(["git", *args], 0, "true\n", "")
            return real_git(cwd, *args)

        monkeypatch.setattr(migration.grip, "git", missing_init_but_plausible_probe)
        monkeypatch.setattr(migration.grip, "_require_git_success", lambda _proc, _action: None)

        result = bootstrap_gr1_workspace(gr1_workspace)

        assert result["status"] == "initialized"
        assert (gr1_workspace / ".grip" / "workspace_spec.toml").is_file()
        assert not (gr1_workspace / ".grip" / ".git").exists()

    def test_conflicting_existing_spec_refuses_before_initializing_store(self, gr1_workspace: Path) -> None:
        grip_dir = gr1_workspace / ".grip"
        grip_dir.mkdir()
        (grip_dir / "workspace_spec.toml").write_text('workspace_name = "not-derived"\n')

        with pytest.raises(SystemExit, match="existing generated spec differs"):
            bootstrap_gr1_workspace(gr1_workspace)

        assert not (grip_dir / ".git").exists()

    def test_invalid_existing_git_directory_refuses_without_replacing_spec(self, gr1_workspace: Path) -> None:
        grip_dir = gr1_workspace / ".grip"
        (grip_dir / ".git").mkdir(parents=True)
        manifest = yaml.safe_load(
            (gr1_workspace / ".gitgrip" / "spaces" / "main" / "gripspace.yml").read_text()
        )
        import tomllib
        with (gr1_workspace / ".gitgrip" / "agents.toml").open("rb") as fh:
            agents_doc = tomllib.load(fh)
        expected = render_workspace_spec(
            compile_gr1_to_workspace_spec(gr1_workspace, manifest, agents_doc)
        ).encode()
        (grip_dir / "workspace_spec.toml").write_bytes(expected)

        with pytest.raises(SystemExit, match="not a valid git object store"):
            bootstrap_gr1_workspace(gr1_workspace)

        assert (grip_dir / "workspace_spec.toml").read_bytes() == expected

    def test_cli_reports_the_single_bootstrap_outcome(self, gr1_workspace: Path) -> None:
        result = make_cli_runner().invoke(app, ["workspace", "bootstrap-gr1", str(gr1_workspace), "--json"])

        assert result.exit_code == 0, result.stdout
        payload = json.loads(result.stdout)
        assert payload["status"] == "initialized"
        assert payload["manifest_path"].endswith(".gitgrip/spaces/main/gripspace.yml")


class TestRegenerateGr1Workspace:
    """Regeneration is deliberately narrower than bootstrap and materialization."""

    def _prepared(self, workspace: Path) -> tuple[Path, str]:
        bootstrap_gr1_workspace(workspace)
        spec_path = workspace / ".grip" / "workspace_spec.toml"
        return spec_path, hashlib.sha256(spec_path.read_bytes()).hexdigest()

    def _url_only_manifest_change(self, workspace: Path) -> None:
        manifest_path = workspace / ".gitgrip" / "spaces" / "main" / "gripspace.yml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["repos"]["synapt"]["url"] = "git@github.com:synapt-dev/recall.git"
        manifest_path.write_text(yaml.dump(manifest))

    def test_url_only_change_replaces_only_generated_spec_and_emits_nonmaterializing_receipt(
        self, gr1_workspace: Path, tmp_path: Path
    ) -> None:
        spec_path, expected = self._prepared(gr1_workspace)
        before_git = (gr1_workspace / ".grip" / ".git").stat().st_ino
        self._url_only_manifest_change(gr1_workspace)
        receipt = tmp_path / "receipt.json"

        result = regenerate_gr1_workspace(
            gr1_workspace, expected_spec_sha256=expected, receipt_path=receipt
        )

        assert result["status"] == "regenerated"
        assert result["materialization"] is False
        assert result["expected_old_spec_sha256"] == expected
        assert result["new_spec_sha256"] != expected
        assert (gr1_workspace / ".grip" / ".git").stat().st_ino == before_git
        assert "recall.git" in spec_path.read_text()
        assert json.loads(receipt.read_text())["new_spec_sha256"] == result["new_spec_sha256"]

    def test_wrong_expected_hash_refuses_before_replacing_spec(self, gr1_workspace: Path, tmp_path: Path) -> None:
        spec_path, _ = self._prepared(gr1_workspace)
        before = spec_path.read_bytes()
        self._url_only_manifest_change(gr1_workspace)

        with pytest.raises(SystemExit, match="expected old spec hash"):
            regenerate_gr1_workspace(
                gr1_workspace, expected_spec_sha256="0" * 64, receipt_path=tmp_path / "receipt.json"
            )

        assert spec_path.read_bytes() == before
        assert not (tmp_path / "receipt.json").exists()

    def test_dirty_object_store_refuses_before_replacing_spec(self, gr1_workspace: Path, tmp_path: Path) -> None:
        spec_path, expected = self._prepared(gr1_workspace)
        before = spec_path.read_bytes()
        self._url_only_manifest_change(gr1_workspace)
        (gr1_workspace / ".grip" / "unexpected").write_text("dirty\n")

        with pytest.raises(SystemExit, match="dirty object store"):
            regenerate_gr1_workspace(
                gr1_workspace, expected_spec_sha256=expected, receipt_path=tmp_path / "receipt.json"
            )

        assert spec_path.read_bytes() == before

    def test_active_lane_and_lease_refuse_before_replacing_spec(self, gr1_workspace: Path, tmp_path: Path) -> None:
        spec_path, expected = self._prepared(gr1_workspace)
        before = spec_path.read_bytes()
        self._url_only_manifest_change(gr1_workspace)
        current = gr1_workspace / ".grip" / "state" / "current_lane" / "apollo.json"
        current.parent.mkdir(parents=True)
        current.write_text(json.dumps({"current": {"lane_name": "review"}}))

        with pytest.raises(SystemExit, match="active lanes"):
            regenerate_gr1_workspace(gr1_workspace, expected_spec_sha256=expected, receipt_path=tmp_path / "receipt.json")
        assert spec_path.read_bytes() == before

        current.write_text(json.dumps({"current": None}))
        leases = gr1_workspace / ".grip" / "state" / "lanes" / "apollo" / "review" / "leases.json"
        leases.parent.mkdir(parents=True)
        leases.write_text(json.dumps([{"actor": "apollo"}]))
        with pytest.raises(SystemExit, match="active lane leases"):
            regenerate_gr1_workspace(gr1_workspace, expected_spec_sha256=expected, receipt_path=tmp_path / "receipt.json")
        assert spec_path.read_bytes() == before

    def test_atomic_publish_failure_preserves_old_spec(self, gr1_workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        spec_path, expected = self._prepared(gr1_workspace)
        before = spec_path.read_bytes()
        self._url_only_manifest_change(gr1_workspace)
        monkeypatch.setattr(migration, "_atomic_write", lambda _path, _content: (_ for _ in ()).throw(OSError("injected atomic failure")))

        with pytest.raises(OSError, match="injected atomic failure"):
            regenerate_gr1_workspace(gr1_workspace, expected_spec_sha256=expected, receipt_path=tmp_path / "receipt.json")
        assert spec_path.read_bytes() == before

    def test_lock_rechecks_expected_hash_after_a_competing_writer(self, gr1_workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        spec_path, expected = self._prepared(gr1_workspace)
        before = spec_path.read_bytes()
        self._url_only_manifest_change(gr1_workspace)

        @contextlib.contextmanager
        def competing_writer(_path: Path):
            spec_path.write_bytes(b"competing bytes\n")
            yield

        monkeypatch.setattr(migration.lane_proto, "exclusive_lock", competing_writer)
        with pytest.raises(SystemExit, match="expected old spec hash"):
            regenerate_gr1_workspace(gr1_workspace, expected_spec_sha256=expected, receipt_path=tmp_path / "receipt.json")
        assert spec_path.read_bytes() == b"competing bytes\n"
        assert not (tmp_path / "receipt.json").exists()

    def test_symlinked_generated_spec_refuses_without_touching_target(self, gr1_workspace: Path, tmp_path: Path) -> None:
        spec_path, expected = self._prepared(gr1_workspace)
        external = tmp_path / "external-spec"
        external.write_bytes(spec_path.read_bytes())
        spec_path.unlink()
        spec_path.symlink_to(external)
        self._url_only_manifest_change(gr1_workspace)

        with pytest.raises(SystemExit, match="must not be a symlink"):
            regenerate_gr1_workspace(gr1_workspace, expected_spec_sha256=expected, receipt_path=tmp_path / "receipt.json")
        assert external.read_bytes() != b""

    def test_cli_requires_caller_selected_receipt_for_regeneration(self, gr1_workspace: Path) -> None:
        _spec_path, expected = self._prepared(gr1_workspace)
        self._url_only_manifest_change(gr1_workspace)
        result = CliRunner().invoke(
            app,
            ["workspace", "bootstrap-gr1", str(gr1_workspace), "--regenerate", "--expected-spec-sha256", expected],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code != 0
        assert "--receipt" in _plain(result.output)

    def test_receipt_bound_round_trip_restores_exact_old_bytes(self, gr1_workspace: Path, tmp_path: Path) -> None:
        spec_path, expected = self._prepared(gr1_workspace)
        before = spec_path.read_bytes()
        self._url_only_manifest_change(gr1_workspace)
        forward_receipt = tmp_path / "forward.json"
        forward = regenerate_gr1_workspace(gr1_workspace, expected_spec_sha256=expected, receipt_path=forward_receipt)
        sidecar = tmp_path / json.loads(forward_receipt.read_text())["sidecar_relative_path"]
        assert sidecar.read_bytes() == before
        assert forward["sidecar_sha256"] == expected

        rollback = rollback_gr1_workspace(
            gr1_workspace,
            rollback_receipt_path=forward_receipt,
            expected_current_spec_sha256=forward["new_spec_sha256"],
            receipt_path=tmp_path / "rollback.json",
        )
        assert spec_path.read_bytes() == before
        assert rollback["status"] == "rolled_back"
        assert rollback["materialization"] is False

    def test_rollback_refuses_stale_or_tampered_authority(self, gr1_workspace: Path, tmp_path: Path) -> None:
        spec_path, expected = self._prepared(gr1_workspace)
        before = spec_path.read_bytes()
        self._url_only_manifest_change(gr1_workspace)
        forward_receipt = tmp_path / "forward.json"
        forward = regenerate_gr1_workspace(gr1_workspace, expected_spec_sha256=expected, receipt_path=forward_receipt)

        with pytest.raises(SystemExit, match="current spec hash"):
            rollback_gr1_workspace(gr1_workspace, rollback_receipt_path=forward_receipt, expected_current_spec_sha256=expected, receipt_path=tmp_path / "stale.json")
        assert spec_path.read_bytes() != before

        sidecar = tmp_path / json.loads(forward_receipt.read_text())["sidecar_relative_path"]
        sidecar.write_text("tampered\n")
        with pytest.raises(SystemExit, match="sidecar hash"):
            rollback_gr1_workspace(gr1_workspace, rollback_receipt_path=forward_receipt, expected_current_spec_sha256=forward["new_spec_sha256"], receipt_path=tmp_path / "tampered.json")

    def test_existing_receipt_and_unsafe_sidecar_path_refuse(self, gr1_workspace: Path, tmp_path: Path) -> None:
        _spec_path, expected = self._prepared(gr1_workspace)
        self._url_only_manifest_change(gr1_workspace)
        receipt = tmp_path / "forward.json"
        receipt.write_text("existing\n")
        with pytest.raises(SystemExit, match="overwrite"):
            regenerate_gr1_workspace(gr1_workspace, expected_spec_sha256=expected, receipt_path=receipt)

        receipt.write_text(json.dumps({"schema": "gr2-workspace-regeneration/v2", "workspace_root": str(gr1_workspace), "workspace_spec_path": str(gr1_workspace / ".grip" / "workspace_spec.toml"), "grip_repo_path": str(gr1_workspace / ".grip"), "manifest_sha256": hashlib.sha256((gr1_workspace / ".gitgrip" / "spaces" / "main" / "gripspace.yml").read_bytes()).hexdigest(), "agents_sha256": hashlib.sha256((gr1_workspace / ".gitgrip" / "agents.toml").read_bytes()).hexdigest(), "object_store_head": "x", "object_store_status": [], "lane_snapshot": {"files": []}, "observed_old_spec_sha256": "0" * 64, "new_spec_sha256": "0" * 64, "sidecar_sha256": "0" * 64, "materialization": False, "sidecar_relative_path": "../escape"}))
        with pytest.raises(SystemExit, match="sidecar path is unsafe"):
            rollback_gr1_workspace(gr1_workspace, rollback_receipt_path=receipt, expected_current_spec_sha256="0" * 64, receipt_path=tmp_path / "out.json")

    def test_rollback_refuses_altered_receipt_workspace_and_symlinked_sidecar(self, gr1_workspace: Path, tmp_path: Path) -> None:
        _spec_path, expected = self._prepared(gr1_workspace)
        self._url_only_manifest_change(gr1_workspace)
        receipt = tmp_path / "forward.json"
        forward = regenerate_gr1_workspace(gr1_workspace, expected_spec_sha256=expected, receipt_path=receipt)
        doc = json.loads(receipt.read_text())
        doc["workspace_root"] = "/wrong/workspace"
        receipt.write_text(json.dumps(doc))
        with pytest.raises(SystemExit, match="workspace binding"):
            rollback_gr1_workspace(gr1_workspace, rollback_receipt_path=receipt, expected_current_spec_sha256=forward["new_spec_sha256"], receipt_path=tmp_path / "wrong-workspace.json")

        doc["workspace_root"] = str(gr1_workspace)
        receipt.write_text(json.dumps(doc))
        sidecar = tmp_path / doc["sidecar_relative_path"]
        copy = tmp_path / "sidecar-copy"
        copy.write_bytes(sidecar.read_bytes())
        sidecar.unlink()
        sidecar.symlink_to(copy)
        with pytest.raises(SystemExit, match="must not be a symlink"):
            rollback_gr1_workspace(gr1_workspace, rollback_receipt_path=receipt, expected_current_spec_sha256=forward["new_spec_sha256"], receipt_path=tmp_path / "symlink.json")

    def test_rollback_refuses_existing_output_and_wrong_store_binding(self, gr1_workspace: Path, tmp_path: Path) -> None:
        spec_path, expected = self._prepared(gr1_workspace)
        self._url_only_manifest_change(gr1_workspace)
        receipt = tmp_path / "forward.json"
        forward = regenerate_gr1_workspace(gr1_workspace, expected_spec_sha256=expected, receipt_path=receipt)
        existing_output = tmp_path / "rollback.json"
        existing_output.write_text("do not replace\n")
        with pytest.raises(SystemExit, match="overwrite"):
            rollback_gr1_workspace(gr1_workspace, rollback_receipt_path=receipt, expected_current_spec_sha256=forward["new_spec_sha256"], receipt_path=existing_output)
        assert spec_path.read_bytes() != b""

        doc = json.loads(receipt.read_text())
        doc["object_store_head"] = "0" * 40
        receipt.write_text(json.dumps(doc))
        with pytest.raises(SystemExit, match="object-store binding"):
            rollback_gr1_workspace(gr1_workspace, rollback_receipt_path=receipt, expected_current_spec_sha256=forward["new_spec_sha256"], receipt_path=tmp_path / "wrong-store.json")

    def test_prepared_marker_recovers_complete_forward_receipt_before_next_attempt(self, gr1_workspace: Path, tmp_path: Path) -> None:
        spec_path, expected = self._prepared(gr1_workspace)
        self._url_only_manifest_change(gr1_workspace)
        receipt = tmp_path / "forward.json"
        forward = regenerate_gr1_workspace(gr1_workspace, expected_spec_sha256=expected, receipt_path=receipt)
        receipt.unlink()
        marker = tmp_path / "forward.json.prepared.json"
        marker.write_text(json.dumps({"schema": "gr2-workspace-regeneration-prepared/v1", "phase": "prepared", "forward_receipt": receipt.name, "payload": forward}))
        with pytest.raises(SystemExit, match="overwrite"):
            regenerate_gr1_workspace(gr1_workspace, expected_spec_sha256=expected, receipt_path=receipt)
        assert json.loads(receipt.read_text())["new_spec_sha256"] == forward["new_spec_sha256"]
        assert not marker.exists()
        assert hashlib.sha256(spec_path.read_bytes()).hexdigest() == forward["new_spec_sha256"]

    def test_rollback_receipt_failure_compensates_to_pre_rollback_bytes(self, gr1_workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        spec_path, expected = self._prepared(gr1_workspace)
        self._url_only_manifest_change(gr1_workspace)
        forward_receipt = tmp_path / "forward.json"
        forward = regenerate_gr1_workspace(gr1_workspace, expected_spec_sha256=expected, receipt_path=forward_receipt)
        current = spec_path.read_bytes()
        monkeypatch.setattr(migration, "_write_new_receipt", lambda *_args: (_ for _ in ()).throw(OSError("receipt failure")))
        with pytest.raises(OSError, match="receipt failure"):
            rollback_gr1_workspace(gr1_workspace, rollback_receipt_path=forward_receipt, expected_current_spec_sha256=forward["new_spec_sha256"], receipt_path=tmp_path / "rollback.json")
        assert spec_path.read_bytes() == current
        assert forward_receipt.exists()
        assert not (tmp_path / "rollback.json").exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL crash simulation is POSIX-only; signal.SIGKILL is absent on Windows")
    @pytest.mark.parametrize("phase", ["marker_durable", "spec_replaced", "receipt_durable", "marker_cleared"])
    def test_sigkill_at_every_forward_phase_leaves_recoverable_or_actionable_state(self, gr1_workspace: Path, tmp_path: Path, phase: str) -> None:
        spec_path, expected = self._prepared(gr1_workspace)
        self._url_only_manifest_change(gr1_workspace)
        receipt = tmp_path / "kill-forward.json"
        program = (
            "import os,signal,sys,types; from pathlib import Path; root=Path(sys.argv[1]).resolve(); pkg=types.ModuleType('gr2'); pkg.__path__=[str(root)]; sys.modules['gr2']=pkg; "
            "from gr2.python_cli import migration; "
            "assert str(Path(migration.__file__).resolve()).startswith(str(root)), migration.__file__; "
            "p=sys.argv[2]; migration._transaction_phase_hook=lambda x: os.kill(os.getpid(), signal.SIGKILL) if x==p else None; "
            "migration.regenerate_gr1_workspace(Path(sys.argv[3]), expected_spec_sha256=sys.argv[4], receipt_path=Path(sys.argv[5]))"
        )
        gr2_root = Path(__file__).parents[1]
        child = subprocess.run([sys.executable, "-c", program, str(gr2_root), phase, str(gr1_workspace), expected, str(receipt)])
        assert child.returncode != 0
        marker = tmp_path / "kill-forward.json.prepared.json"
        if phase == "marker_durable":
            assert marker.exists() and hashlib.sha256(spec_path.read_bytes()).hexdigest() == expected
        else:
            assert hashlib.sha256(spec_path.read_bytes()).hexdigest() != expected
        with pytest.raises(SystemExit):
            regenerate_gr1_workspace(gr1_workspace, expected_spec_sha256=expected, receipt_path=receipt)
        assert receipt.exists() or marker.exists()

    @pytest.mark.parametrize("written", [b"", b"partial", b"complete receipt"])
    def test_forward_receipt_writer_failure_compensates_all_output_shapes(self, gr1_workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, written: bytes) -> None:
        spec_path, expected = self._prepared(gr1_workspace)
        old = spec_path.read_bytes()
        self._url_only_manifest_change(gr1_workspace)
        receipt = tmp_path / "forward-failure.json"
        def fail_writer(path: Path, _payload: dict[str, object]) -> None:
            if written:
                path.write_bytes(written)
            raise OSError("injected forward receipt failure")
        monkeypatch.setattr(migration, "_write_new_receipt", fail_writer)
        with pytest.raises(OSError, match="injected forward receipt failure"):
            regenerate_gr1_workspace(gr1_workspace, expected_spec_sha256=expected, receipt_path=receipt)
        assert spec_path.read_bytes() == old
        assert not receipt.exists()
        assert not (tmp_path / "forward-failure.json.prepared.json").exists()
        assert not (tmp_path / f"forward-failure.json.old-spec-{expected}.toml").exists()

    def test_inactive_lane_state_is_not_object_store_dirt(self, gr1_workspace: Path, tmp_path: Path) -> None:
        """An inactive lane file under state/ is the control plane, not store dirt.
        Regeneration proceeds (active lanes are refused separately); before the
        state/ exclusion this refused as 'dirty object store'."""
        spec_path, expected = self._prepared(gr1_workspace)
        self._url_only_manifest_change(gr1_workspace)
        current = gr1_workspace / ".grip" / "state" / "current_lane" / "apollo.json"
        current.parent.mkdir(parents=True)
        current.write_text(json.dumps({"current": None}))
        result = regenerate_gr1_workspace(
            gr1_workspace, expected_spec_sha256=expected, receipt_path=tmp_path / "receipt.json"
        )
        assert result["status"] == "regenerated"

    def test_in_lock_active_lane_recheck_refuses(self, gr1_workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A lane can go active while a regenerator waits for the lock, so the
        active-lane guard is re-checked after acquisition. Activate a lane at the
        lock_acquired seam: the pre-lock check already passed, so only the in-lock
        re-check can refuse. Removing that re-check leaves this green."""
        spec_path, expected = self._prepared(gr1_workspace)
        before = spec_path.read_bytes()
        self._url_only_manifest_change(gr1_workspace)
        current = gr1_workspace / ".grip" / "state" / "current_lane" / "apollo.json"

        def hook(phase: str) -> None:
            if phase == "lock_acquired":
                current.parent.mkdir(parents=True, exist_ok=True)
                current.write_text(json.dumps({"current": {"lane_name": "review"}}))

        monkeypatch.setattr(migration, "_transaction_phase_hook", hook)
        with pytest.raises(SystemExit, match="active lanes"):
            regenerate_gr1_workspace(gr1_workspace, expected_spec_sha256=expected, receipt_path=tmp_path / "receipt.json")
        assert spec_path.read_bytes() == before  # refused before replacing the spec

    def test_regeneration_lock_file_kept_across_release(self, tmp_path: Path) -> None:
        """The lock file is KEPT across release (no unlink/rmdir): flock on a path
        unlinked and re-created binds a new inode and excludes no waiter. Restoring
        the unlink makes the second acquisition see a different inode."""
        lock = tmp_path / "state" / "x.lock"
        with migration._regeneration_lock(lock):
            assert lock.exists()
            ino = lock.stat().st_ino
        assert lock.exists()  # kept, not unlinked
        with migration._regeneration_lock(lock):
            assert lock.stat().st_ino == ino  # same inode == real mutual exclusion

    def test_regenerate_and_rollback_render_through_cli_without_keyerror(self, gr1_workspace: Path, tmp_path: Path) -> None:
        """The non-JSON render hardcoded bootstrap keys, so rollback (no
        manifest_path) and regenerate (no repo_count) raised KeyError AFTER a
        successful mutation -- exit 1 on a completed rollback. Render per schema.
        Exercised through the real CLI verb, one assertion per verb path."""
        runner = CliRunner()
        spec_path, expected = self._prepared(gr1_workspace)
        self._url_only_manifest_change(gr1_workspace)
        fwd = tmp_path / "forward.json"
        r = runner.invoke(app, ["workspace", "bootstrap-gr1", str(gr1_workspace),
                                "--regenerate", "--expected-spec-sha256", expected, "--receipt", str(fwd)])
        assert r.exit_code == 0, r.output
        assert "Traceback" not in r.output and "KeyError" not in r.output
        assert "regenerated" in r.output
        new_sha = json.loads(fwd.read_text())["new_spec_sha256"]
        rb = runner.invoke(app, ["workspace", "bootstrap-gr1", str(gr1_workspace),
                                 "--rollback-receipt", str(fwd),
                                 "--expected-current-spec-sha256", new_sha,
                                 "--receipt", str(tmp_path / "rollback.json")])
        assert rb.exit_code == 0, rb.output
        assert "Traceback" not in rb.output and "KeyError" not in rb.output
        assert "rolled_back" in rb.output


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

    def test_flags_a_real_linked_worktree(self, tmp_path: Path) -> None:
        """The playbook has always claimed workspace status flags a
        hand-made linked worktree the same way repo status does; until this
        test, only repo status actually did. Real git worktree add, not a
        synthetic fixture -- same discipline as test_repo_status_
        linked_worktree.py's own real-worktree tests."""
        canonical = tmp_path / "canonical"
        canonical.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=canonical, check=True)
        subprocess.run(["git", "config", "user.email", "dev@layne.pro"], cwd=canonical, check=True)
        subprocess.run(["git", "config", "user.name", "Layne Penney"], cwd=canonical, check=True)
        (canonical / "README.md").write_text("a\n")
        subprocess.run(["git", "add", "."], cwd=canonical, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=canonical, check=True)

        workspace_root = tmp_path / "workspace"
        linked_path = workspace_root / "linked-repo"
        result = subprocess.run(
            ["git", "worktree", "add", "-b", "wt-branch", str(linked_path)],
            cwd=canonical,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

        grip_dir = workspace_root / ".grip"
        grip_dir.mkdir(parents=True, exist_ok=True)
        (grip_dir / "workspace_spec.toml").write_text(
            'workspace_name = "test"\n\n'
            '[[repos]]\n'
            'name = "linked-repo"\n'
            'path = "linked-repo"\n'
            f'url = "file://{canonical}"\n'
        )

        status = workspace_status(workspace_root)
        assert status["linked_worktrees"] == [str(linked_path)]
        rendered = render_status(status)
        assert str(linked_path) in rendered
        assert "convert-clone" in rendered

    def test_does_not_flag_an_own_clone(self, tmp_path: Path) -> None:
        """Control: a real clone (not a linked worktree) at the same shape
        must not be flagged."""
        origin = tmp_path / "origin"
        origin.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=origin, check=True)
        subprocess.run(["git", "config", "user.email", "dev@layne.pro"], cwd=origin, check=True)
        subprocess.run(["git", "config", "user.name", "Layne Penney"], cwd=origin, check=True)
        (origin / "README.md").write_text("a\n")
        subprocess.run(["git", "add", "."], cwd=origin, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=origin, check=True)

        workspace_root = tmp_path / "workspace"
        cloned_path = workspace_root / "cloned-repo"
        cloned_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "-q", str(origin), str(cloned_path)],
            check=True, capture_output=True, text=True,
        )

        grip_dir = workspace_root / ".grip"
        grip_dir.mkdir(parents=True, exist_ok=True)
        (grip_dir / "workspace_spec.toml").write_text(
            'workspace_name = "test"\n\n'
            '[[repos]]\n'
            'name = "cloned-repo"\n'
            'path = "cloned-repo"\n'
            f'url = "file://{origin}"\n'
        )

        status = workspace_status(workspace_root)
        assert status["linked_worktrees"] == []
        assert "convert-clone" not in render_status(status)


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


def test_regeneration_guard_ignores_plumbing_store_head_deletions(tmp_path):
    """A .grip store is plumbing-only: HEAD is a real commit whose tree is never
    checked out, so `git status --porcelain` reports every HEAD path as a
    deletion. That is the store's normal state, not dirt -- the guard used to
    refuse every store that had ever held a review commit (Stromus m_4a3fdd37).
    It must judge untracked files only; an untracked file it would clobber still
    refuses."""
    grip = tmp_path / "store"
    grip.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}

    def g(*a, inp=None):
        return subprocess.run(["git", "-C", str(grip), *a], env=env, input=inp,
                              capture_output=True, text=True, check=True).stdout.strip()

    g("init", "-q")
    # Build a commit by PLUMBING ONLY, exactly like grip.py: hash a blob, mktree,
    # commit-tree, update-ref. Never `git add`, never check the tree out.
    blob = g("hash-object", "-w", "--stdin", inp="review commit content\n")
    tree = g("mktree", inp=f"100644 blob {blob}\treview\n")
    commit = g("commit-tree", tree, "-m", "grip project review")
    g("update-ref", "HEAD", commit)

    # The scenario: porcelain reports the HEAD path as a deletion.
    porcelain = subprocess.run(["git", "-C", str(grip), "status", "--porcelain"],
                               env=env, capture_output=True, text=True).stdout
    assert any("review" in line and "D" in line for line in porcelain.splitlines()), porcelain

    # The fix: a clean plumbing store is NOT refused (this raised before the fix).
    migration._require_clean_regeneration_store(grip)
    # The allowed generated spec is still fine.
    (grip / "workspace_spec.toml").write_text("x\n")
    migration._require_clean_regeneration_store(grip)
    # A real untracked file still refuses.
    (grip / "unexpected").write_text("dirt\n")
    with pytest.raises(SystemExit, match="dirty object store"):
        migration._require_clean_regeneration_store(grip)


# --- lane-state migration (agents/<unit>/lanes -> .grip/state/lanes) ---

from gr2.python_cli.migration import migrate_lane_state


def _legacy_lane(workspace: Path, unit: str, lane: str, *, repos=("app",)) -> Path:
    """Build a legacy lane tree with a lane.toml, leases, and a repo."""
    lane_dir = workspace / "agents" / unit / "lanes" / lane
    (lane_dir).mkdir(parents=True)
    (lane_dir / "lane.toml").write_text(f'lane_name = "{lane}"\nowner_unit = "{unit}"\n')
    (lane_dir / "leases.json").write_text("[]")
    for repo in repos:
        rd = lane_dir / "repos" / repo
        rd.mkdir(parents=True)
        (rd / "README.md").write_text("x\n")
    return lane_dir


class TestMigrateLaneState:
    def test_moves_legacy_tree_and_receipt_names_it(self, tmp_path: Path) -> None:
        src = _legacy_lane(tmp_path, "atlas", "feature")
        src_files = sorted(p.relative_to(src).as_posix() for p in src.rglob("*") if p.is_file())
        receipt = tmp_path / "migrate-receipt.json"

        payload = migrate_lane_state(tmp_path, receipt_path=receipt)

        dest = tmp_path / ".grip" / "state" / "lanes" / "atlas" / "feature"
        assert dest.is_dir()
        assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()) == src_files
        assert not (tmp_path / "agents" / "atlas" / "lanes" / "feature").exists()
        assert payload["count"] == 1
        row = payload["moved"][0]
        assert row["unit"] == "atlas" and row["lane"] == "feature"
        assert row["dest"] == ".grip/state/lanes/atlas/feature"
        assert row["files"] == len(src_files)
        # Receipt is the durable record of what moved.
        assert json.loads(receipt.read_text())["moved"][0]["source"] == "agents/atlas/lanes/feature"

    def test_zero_move_when_no_legacy_tree(self, tmp_path: Path) -> None:
        receipt = tmp_path / "empty-receipt.json"
        payload = migrate_lane_state(tmp_path, receipt_path=receipt)
        assert payload["count"] == 0 and payload["moved"] == []
        assert receipt.is_file()

    def test_refuses_active_lease_and_moves_nothing(self, tmp_path: Path) -> None:
        src = _legacy_lane(tmp_path, "atlas", "feature")
        (src / "leases.json").write_text(json.dumps([{"actor": "atlas", "mode": "edit"}]))
        receipt = tmp_path / "receipt.json"
        with pytest.raises(SystemExit, match="active lane leases"):
            migrate_lane_state(tmp_path, receipt_path=receipt)
        assert src.exists()  # nothing moved
        assert not receipt.exists()  # no receipt for a refused migration

    def test_refuses_active_current_lane_pointer(self, tmp_path: Path) -> None:
        _legacy_lane(tmp_path, "atlas", "feature")
        current = tmp_path / ".grip" / "state" / "current_lane" / "atlas.json"
        current.parent.mkdir(parents=True)
        current.write_text(json.dumps({"current": {"lane_name": "feature"}}))
        with pytest.raises(SystemExit, match="active lanes"):
            migrate_lane_state(tmp_path, receipt_path=tmp_path / "r.json")

    def test_refuses_existing_destination_and_moves_nothing(self, tmp_path: Path) -> None:
        # Two lanes in legacy; one already present at the new path. Two-pass must
        # refuse BEFORE moving either, so the clean lane is not half-migrated.
        _legacy_lane(tmp_path, "atlas", "already")
        clean = _legacy_lane(tmp_path, "atlas", "clean")
        (tmp_path / ".grip" / "state" / "lanes" / "atlas" / "already").mkdir(parents=True)
        with pytest.raises(SystemExit, match="refuses to overwrite existing destinations"):
            migrate_lane_state(tmp_path, receipt_path=tmp_path / "r.json")
        assert clean.exists()  # the clean lane was NOT moved

    def test_idempotent_across_two_runs_with_fresh_receipts(self, tmp_path: Path) -> None:
        _legacy_lane(tmp_path, "atlas", "feature")
        first = migrate_lane_state(tmp_path, receipt_path=tmp_path / "r1.json")
        assert first["count"] == 1
        second = migrate_lane_state(tmp_path, receipt_path=tmp_path / "r2.json")
        assert second["count"] == 0

    def test_cli_migrate_lane_state(self, tmp_path: Path) -> None:
        _legacy_lane(tmp_path, "atlas", "feature")
        receipt = tmp_path / "cli-receipt.json"
        result = make_cli_runner().invoke(app, ["workspace", "migrate-lane-state", str(tmp_path), "--receipt", str(receipt), "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["count"] == 1
        assert (tmp_path / ".grip" / "state" / "lanes" / "atlas" / "feature").is_dir()
