"""TDD spec: gr overlay CLI wiring from stubs to real module functions.

These tests verify that CLI commands call the underlying module functions
and return real output (exit code 0) instead of "not implemented" (exit code 1).

The test_overlay_cli.py tests verify the stub contract; these tests verify
the wired contract. Once wired, the stub tests should be updated or removed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import tomli_w
from typer.testing import CliRunner

from gr2_overlay.cli import overlay_app

runner = CliRunner()


def _init_bare_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", str(path)], check=True, capture_output=True)
    return path


def _workspace_with_overlay_store(tmp_path: Path) -> tuple[Path, Path]:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    grip_dir = workspace_root / ".grip"
    grip_dir.mkdir()
    overlay_store = _init_bare_git_repo(grip_dir / "overlay-store.git")
    return workspace_root, overlay_store


def _write_overlay_spec(workspace_root: Path, entries: list[dict]) -> None:
    spec_path = workspace_root / ".grip" / "overlays.toml"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    data = {"overlays": entries}
    spec_path.write_bytes(tomli_w.dumps(data).encode())


def _write_overlay_stack_toml(
    workspace_root: Path,
    active: list[str],
    available: list[str],
) -> None:
    stack_path = workspace_root / ".grip" / "overlay-stack.toml"
    stack_path.parent.mkdir(parents=True, exist_ok=True)
    data = {"active": active, "available": available}
    stack_path.write_bytes(tomli_w.dumps(data).encode())


def _write_overlay_status_toml(
    workspace_root: Path, active: list[str], available: list[str], applied: list[str]
) -> None:
    status_path = workspace_root / ".grip" / "overlay-status.toml"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    data = {"active": active, "available": available, "applied": applied}
    status_path.write_bytes(tomli_w.dumps(data).encode())


# --- stack command ---


class TestStackCommand:
    def test_stack_returns_active_and_available_overlays(self, tmp_path: Path):
        workspace_root, _ = _workspace_with_overlay_store(tmp_path)
        _write_overlay_stack_toml(
            workspace_root,
            active=["refs/overlays/alice/theme-dark"],
            available=["refs/overlays/bob/theme-light"],
        )

        result = runner.invoke(overlay_app, ["stack", str(workspace_root)])

        assert result.exit_code == 0
        assert "alice/theme-dark" in result.output
        assert "bob/theme-light" in result.output

    def test_stack_json_output(self, tmp_path: Path):
        workspace_root, _ = _workspace_with_overlay_store(tmp_path)
        _write_overlay_stack_toml(
            workspace_root,
            active=["refs/overlays/alice/theme-dark"],
            available=[],
        )

        result = runner.invoke(overlay_app, ["stack", str(workspace_root), "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["active"]) == 1
        assert data["active"][0]["author"] == "alice"
        assert data["active"][0]["name"] == "theme-dark"

    def test_stack_empty_workspace(self, tmp_path: Path):
        workspace_root, _ = _workspace_with_overlay_store(tmp_path)

        result = runner.invoke(overlay_app, ["stack", str(workspace_root)])

        assert result.exit_code == 0
        assert "Active" in result.output


# --- status command ---


class TestStatusCommand:
    def test_status_shows_overlay_state(self, tmp_path: Path):
        workspace_root, _ = _workspace_with_overlay_store(tmp_path)
        _write_overlay_status_toml(
            workspace_root,
            active=["theme-dark"],
            available=["theme-light"],
            applied=["theme-dark"],
        )

        result = runner.invoke(overlay_app, ["status", str(workspace_root)])

        assert result.exit_code == 0
        assert "theme-dark" in result.output

    def test_status_json_output(self, tmp_path: Path):
        workspace_root, _ = _workspace_with_overlay_store(tmp_path)
        _write_overlay_status_toml(
            workspace_root,
            active=["theme-dark"],
            available=["theme-light"],
            applied=["theme-dark"],
        )

        result = runner.invoke(overlay_app, ["status", str(workspace_root), "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "theme-dark" in data["active"]

    def test_status_empty_workspace(self, tmp_path: Path):
        workspace_root, _ = _workspace_with_overlay_store(tmp_path)

        result = runner.invoke(overlay_app, ["status", str(workspace_root)])

        assert result.exit_code == 0


# --- list command ---


class TestListCommand:
    def test_list_shows_declared_overlays(self, tmp_path: Path):
        workspace_root, _ = _workspace_with_overlay_store(tmp_path)
        _write_overlay_spec(
            workspace_root,
            [
                {
                    "name": "kanonic-root",
                    "path": "../config-root",
                    "applies_to": ["config"],
                    "priority": 0,
                },
                {
                    "name": "synapt-core",
                    "path": "../config",
                    "applies_to": ["config"],
                    "priority": 10,
                },
            ],
        )

        result = runner.invoke(overlay_app, ["list", str(workspace_root)])

        assert result.exit_code == 0
        assert "kanonic-root" in result.output
        assert "synapt-core" in result.output

    def test_list_json_output(self, tmp_path: Path):
        workspace_root, _ = _workspace_with_overlay_store(tmp_path)
        _write_overlay_spec(
            workspace_root,
            [
                {
                    "name": "kanonic-root",
                    "path": "../config-root",
                    "applies_to": ["config"],
                    "priority": 0,
                },
            ],
        )

        result = runner.invoke(overlay_app, ["list", str(workspace_root), "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["overlays"]) == 1
        assert data["overlays"][0]["name"] == "kanonic-root"
        assert data["overlays"][0]["priority"] == 0

    def test_list_empty_workspace(self, tmp_path: Path):
        workspace_root, _ = _workspace_with_overlay_store(tmp_path)

        result = runner.invoke(overlay_app, ["list", str(workspace_root)])

        assert result.exit_code == 0


# --- why command ---


class TestWhyCommand:
    def test_why_shows_overlay_attribution(self, tmp_path: Path):
        workspace_root, _ = _workspace_with_overlay_store(tmp_path)
        why_path = workspace_root / ".grip" / "overlay-why.toml"
        why_data = {
            "files": {
                "agents.toml": {
                    "rule": "overlay-deep",
                    "reason": "org overlay extends root agent definitions",
                    "ref": "refs/overlays/synapt/core",
                }
            }
        }
        why_path.write_bytes(tomli_w.dumps(why_data).encode())

        result = runner.invoke(overlay_app, ["why", str(workspace_root), "agents.toml"])

        assert result.exit_code == 0
        assert "overlay-deep" in result.output
        assert "synapt/core" in result.output

    def test_why_json_output(self, tmp_path: Path):
        workspace_root, _ = _workspace_with_overlay_store(tmp_path)
        why_path = workspace_root / ".grip" / "overlay-why.toml"
        why_data = {
            "files": {
                "agents.toml": {
                    "rule": "overlay-deep",
                    "reason": "extends root",
                    "ref": "refs/overlays/synapt/core",
                }
            }
        }
        why_path.write_bytes(tomli_w.dumps(why_data).encode())

        result = runner.invoke(overlay_app, ["why", str(workspace_root), "agents.toml", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["rule"] == "overlay-deep"


# --- trace command ---


class TestTraceCommand:
    def test_trace_shows_line_attribution(self, tmp_path: Path):
        workspace_root, _ = _workspace_with_overlay_store(tmp_path)
        attr_path = workspace_root / ".grip" / "overlay-attribution.toml"
        attr_data = {
            "files": {
                "agents.toml": {
                    "lines": [
                        {"start": 1, "end": 10, "ref": "refs/overlays/kanonic/root"},
                        {"start": 11, "end": 25, "ref": "refs/overlays/synapt/core"},
                    ]
                }
            }
        }
        attr_path.write_bytes(tomli_w.dumps(attr_data).encode())

        result = runner.invoke(overlay_app, ["trace", str(workspace_root), "agents.toml"])

        assert result.exit_code == 0
        assert "kanonic/root" in result.output
        assert "synapt/core" in result.output

    def test_trace_json_output(self, tmp_path: Path):
        workspace_root, _ = _workspace_with_overlay_store(tmp_path)
        attr_path = workspace_root / ".grip" / "overlay-attribution.toml"
        attr_data = {
            "files": {
                "config.toml": {
                    "lines": [
                        {"start": 1, "end": 5, "ref": "refs/overlays/alice/base"},
                    ]
                }
            }
        }
        attr_path.write_bytes(tomli_w.dumps(attr_data).encode())

        result = runner.invoke(overlay_app, ["trace", str(workspace_root), "config.toml", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["file"] == "config.toml"
        assert len(data["regions"]) == 1
