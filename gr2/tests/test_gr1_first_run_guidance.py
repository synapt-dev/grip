"""First-run guidance for workspaces that still use gr1 state.

These are the four places a gr1 user reaches before any gr2 state exists.  They
must name the safe next verb rather than suggest initialization or an internal
function that cannot preserve the existing workspace.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from gr2.python_cli import grip
from gr2.python_cli.app import app
from typer.testing import CliRunner

runner = CliRunner()
EXPECTED_GR1_NEXT_STEP = (
    "This workspace is still in gr1 format. Run `gr2 workspace migrate-gr1 .` "
    "to add gr2 alongside it; your gr1 setup keeps working."
)


def _gr1_workspace(root: Path) -> Path:
    (root / ".gitgrip" / "spaces" / "main").mkdir(parents=True)
    (root / ".gitgrip" / "spaces" / "main" / "gripspace.yml").write_text("repos: {}\n")
    return root


def test_sync_status_defaults_to_cwd_and_names_safe_gr1_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _gr1_workspace(tmp_path)
    monkeypatch.chdir(workspace)

    result = runner.invoke(app, ["sync", "status"])

    assert result.exit_code == 1
    assert str(workspace / ".grip" / "workspace_spec.toml") in result.output
    assert EXPECTED_GR1_NEXT_STEP in result.output
    assert "workspace init" not in result.output


def test_store_log_names_safe_gr1_migration(tmp_path: Path) -> None:
    workspace = _gr1_workspace(tmp_path)

    result = runner.invoke(app, ["store", "log", str(workspace)])

    assert result.exit_code == 1
    assert EXPECTED_GR1_NEXT_STEP in result.output
    assert "grip_init" not in result.output


def test_grip_library_refusal_names_safe_gr1_migration(tmp_path: Path) -> None:
    workspace = _gr1_workspace(tmp_path)

    with pytest.raises(grip.GripInitError, match="migrate-gr1") as exc_info:
        grip.grip_log(workspace)

    assert str(exc_info.value).endswith(EXPECTED_GR1_NEXT_STEP)


def test_workspace_status_defaults_to_cwd_and_names_safe_gr1_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _gr1_workspace(tmp_path)
    monkeypatch.chdir(workspace)

    result = runner.invoke(app, ["workspace", "status"])

    assert result.exit_code == 0, result.output
    assert f"workspace_root = {workspace}" in result.output
    assert EXPECTED_GR1_NEXT_STEP in result.output


def test_detect_gr1_help_explains_reference_repo_count() -> None:
    result = runner.invoke(app, ["workspace", "detect-gr1", "--help"])

    assert result.exit_code == 0, result.output
    assert "reference-repo" in result.output
