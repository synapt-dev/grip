"""TDD tests for grip + config CLI wiring.

Tests the typer CLI layer, not the library functions (those are tested
in test_grip_snapshot.py, test_config_overlay.py, test_grip_hardening.py).

Focus: argument parsing, exit codes, JSON output format, error messages.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from python_cli.gitops import git
from python_cli.grip_cli import config_cli_app, grip_app

app = typer.Typer()
app.add_typer(grip_app, name="grip")
app.add_typer(config_cli_app, name="config")

runner = CliRunner()

SAMPLE_TOML = """\
[spawn]
session_name = "synapt"
channel = "dev"

[agents.opus]
role = "CEO / product design"
model = "claude-opus-4-6"
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _init_repo(path: Path, *, name: str = "test") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init")
    git(path, "config", "user.email", "test@test.com")
    git(path, "config", "user.name", "Test")
    (path / "README.md").write_text(f"# {name}\n")
    git(path, "add", ".")
    git(path, "commit", "-m", f"init {name}")
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    _init_repo(ws / "recall", name="recall")
    git(ws / "recall", "remote", "add", "origin", "https://github.com/synapt-dev/recall")
    config_dir = ws / "config_files"
    config_dir.mkdir()
    (config_dir / "agents.toml").write_text(SAMPLE_TOML)
    (config_dir / "overlay").mkdir()
    return ws


# ---------------------------------------------------------------------------
# gr grip init
# ---------------------------------------------------------------------------


class TestGripInitCLI:
    def test_init_succeeds(self, workspace: Path) -> None:
        result = runner.invoke(app, ["grip", "init", str(workspace)])
        assert result.exit_code == 0

    def test_init_json_output(self, workspace: Path) -> None:
        result = runner.invoke(app, ["grip", "init", str(workspace), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["status"] == "initialized"

    def test_init_idempotent(self, workspace: Path) -> None:
        runner.invoke(app, ["grip", "init", str(workspace)])
        result = runner.invoke(app, ["grip", "init", str(workspace)])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# gr grip snapshot
# ---------------------------------------------------------------------------


class TestGripSnapshotCLI:
    def test_snapshot_succeeds(self, workspace: Path) -> None:
        runner.invoke(app, ["grip", "init", str(workspace)])
        result = runner.invoke(app, [
            "grip", "snapshot", str(workspace),
            "--repos", "recall",
        ])
        assert result.exit_code == 0

    def test_snapshot_json_output(self, workspace: Path) -> None:
        runner.invoke(app, ["grip", "init", str(workspace)])
        result = runner.invoke(app, [
            "grip", "snapshot", str(workspace),
            "--repos", "recall",
            "--json",
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "sha" in data
        assert len(data["sha"]) >= 40

    def test_snapshot_with_message(self, workspace: Path) -> None:
        runner.invoke(app, ["grip", "init", str(workspace)])
        result = runner.invoke(app, [
            "grip", "snapshot", str(workspace),
            "--repos", "recall",
            "--message", "Sprint 27 ceremony",
            "--json",
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "sha" in data

    def test_snapshot_with_type_and_sprint(self, workspace: Path) -> None:
        runner.invoke(app, ["grip", "init", str(workspace)])
        result = runner.invoke(app, [
            "grip", "snapshot", str(workspace),
            "--repos", "recall",
            "--type", "ceremony",
            "--sprint", "27",
            "--json",
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "sha" in data

    def test_snapshot_without_init_fails(self, workspace: Path) -> None:
        result = runner.invoke(app, [
            "grip", "snapshot", str(workspace),
            "--repos", "recall",
        ])
        assert result.exit_code != 0

    def test_snapshot_multiple_repos(self, workspace: Path) -> None:
        _init_repo(workspace / "premium", name="premium")
        runner.invoke(app, ["grip", "init", str(workspace)])
        result = runner.invoke(app, [
            "grip", "snapshot", str(workspace),
            "--repos", "recall,premium",
            "--json",
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "recall" in data["repos"]
        assert "premium" in data["repos"]


# ---------------------------------------------------------------------------
# gr grip log
# ---------------------------------------------------------------------------


class TestGripLogCLI:
    def test_log_empty(self, workspace: Path) -> None:
        runner.invoke(app, ["grip", "init", str(workspace)])
        result = runner.invoke(app, ["grip", "log", str(workspace), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["entries"] == []

    def test_log_after_snapshot(self, workspace: Path) -> None:
        runner.invoke(app, ["grip", "init", str(workspace)])
        runner.invoke(app, [
            "grip", "snapshot", str(workspace), "--repos", "recall",
            "--message", "test snap",
        ])
        result = runner.invoke(app, ["grip", "log", str(workspace), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert len(data["entries"]) == 1
        assert "test snap" in data["entries"][0]["message"]

    def test_log_max_count(self, workspace: Path) -> None:
        runner.invoke(app, ["grip", "init", str(workspace)])
        for i in range(3):
            (workspace / "recall" / f"f{i}.txt").write_text(str(i))
            git(workspace / "recall", "add", ".")
            git(workspace / "recall", "commit", "-m", f"c{i}")
            runner.invoke(app, [
                "grip", "snapshot", str(workspace), "--repos", "recall",
            ])
        result = runner.invoke(app, [
            "grip", "log", str(workspace), "--max-count", "2", "--json",
        ])
        data = json.loads(result.stdout)
        assert len(data["entries"]) == 2

    def test_log_without_init_fails(self, workspace: Path) -> None:
        result = runner.invoke(app, ["grip", "log", str(workspace)])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# gr grip diff
# ---------------------------------------------------------------------------


class TestGripDiffCLI:
    def test_diff_json(self, workspace: Path) -> None:
        runner.invoke(app, ["grip", "init", str(workspace)])
        r1 = runner.invoke(app, [
            "grip", "snapshot", str(workspace), "--repos", "recall", "--json",
        ])
        sha1 = json.loads(r1.stdout)["sha"]

        (workspace / "recall" / "new.txt").write_text("x")
        git(workspace / "recall", "add", ".")
        git(workspace / "recall", "commit", "-m", "change")

        r2 = runner.invoke(app, [
            "grip", "snapshot", str(workspace), "--repos", "recall", "--json",
        ])
        sha2 = json.loads(r2.stdout)["sha"]

        result = runner.invoke(app, [
            "grip", "diff", str(workspace), sha1, sha2, "--json",
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "recall" in data["changed"]

    def test_diff_without_init_fails(self, workspace: Path) -> None:
        result = runner.invoke(app, [
            "grip", "diff", str(workspace), "abc", "def",
        ])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# gr grip checkout
# ---------------------------------------------------------------------------


class TestGripCheckoutCLI:
    def test_checkout_json(self, workspace: Path) -> None:
        runner.invoke(app, ["grip", "init", str(workspace)])
        r1 = runner.invoke(app, [
            "grip", "snapshot", str(workspace), "--repos", "recall", "--json",
        ])
        sha = json.loads(r1.stdout)["sha"]

        result = runner.invoke(app, [
            "grip", "checkout", str(workspace), sha, "--json",
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "recall" in data["repos"]

    def test_checkout_without_init_fails(self, workspace: Path) -> None:
        result = runner.invoke(app, [
            "grip", "checkout", str(workspace), "HEAD",
        ])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# gr config apply
# ---------------------------------------------------------------------------


class TestConfigApplyCLI:
    def test_apply_succeeds(self, workspace: Path) -> None:
        result = runner.invoke(app, [
            "config", "apply",
            str(workspace / "config_files" / "agents.toml"),
            "--overlay-dir", str(workspace / "config_files" / "overlay"),
        ])
        assert result.exit_code == 0

    def test_apply_json_output(self, workspace: Path) -> None:
        result = runner.invoke(app, [
            "config", "apply",
            str(workspace / "config_files" / "agents.toml"),
            "--overlay-dir", str(workspace / "config_files" / "overlay"),
            "--json",
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "agents" in data
        assert "spawn" in data


# ---------------------------------------------------------------------------
# gr config show
# ---------------------------------------------------------------------------


class TestConfigShowCLI:
    def _apply_first(self, workspace: Path) -> None:
        runner.invoke(app, [
            "config", "apply",
            str(workspace / "config_files" / "agents.toml"),
            "--overlay-dir", str(workspace / "config_files" / "overlay"),
        ])

    def test_show_full(self, workspace: Path) -> None:
        self._apply_first(workspace)
        result = runner.invoke(app, [
            "config", "show",
            str(workspace / "config_files" / "agents.toml"),
            "--overlay-dir", str(workspace / "config_files" / "overlay"),
            "--json",
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "agents" in data

    def test_show_with_key(self, workspace: Path) -> None:
        self._apply_first(workspace)
        result = runner.invoke(app, [
            "config", "show",
            str(workspace / "config_files" / "agents.toml"),
            "--overlay-dir", str(workspace / "config_files" / "overlay"),
            "--key", "agents.opus.role",
            "--json",
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["value"] == "CEO / product design"

    def test_show_strict_stale(self, workspace: Path) -> None:
        self._apply_first(workspace)
        base = workspace / "config_files" / "agents.toml"
        base.write_text(SAMPLE_TOML + '\n[agents.new]\nrole = "new"\n')
        result = runner.invoke(app, [
            "config", "show",
            str(base),
            "--overlay-dir", str(workspace / "config_files" / "overlay"),
            "--strict",
        ])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# gr config restore
# ---------------------------------------------------------------------------


class TestConfigRestoreCLI:
    def test_restore_from_grip_commit(self, workspace: Path) -> None:
        runner.invoke(app, ["grip", "init", str(workspace)])
        runner.invoke(app, [
            "config", "apply",
            str(workspace / "config_files" / "agents.toml"),
            "--overlay-dir", str(workspace / "config_files" / "overlay"),
        ])
        snap = runner.invoke(app, [
            "grip", "snapshot", str(workspace),
            "--repos", "recall",
            "--overlay-dir", str(workspace / "config_files" / "overlay"),
            "--json",
        ])
        sha = json.loads(snap.stdout)["sha"]

        result = runner.invoke(app, [
            "config", "restore",
            str(workspace), sha,
            "--overlay-dir", str(workspace / "config_files" / "overlay"),
        ])
        assert result.exit_code == 0
