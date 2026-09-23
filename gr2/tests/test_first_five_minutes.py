"""First-five-minutes contract for the released gr2 CLI.

The two forms below deliberately share the production repo-status function.
The top-level command must not grow a second, slightly different status view.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from gr2.python_cli.app import app


runner = CliRunner()


def _repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("test\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)


def test_workspace_init_defaults_to_the_current_directory(tmp_path: Path, monkeypatch) -> None:
    _repo(tmp_path / "repo-a")
    _repo(tmp_path / "repo-b")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["workspace", "init"])

    assert result.exit_code == 0, result.output
    assert f"workspace_root = {tmp_path}" in result.output
    assert (tmp_path / ".grip" / "workspace_spec.toml").is_file()


def test_top_level_status_routes_from_a_repo_subdirectory_without_prototype_banner(tmp_path: Path, monkeypatch) -> None:
    _repo(tmp_path / "repo-a")
    _repo(tmp_path / "repo-b")
    init = runner.invoke(app, ["workspace", "init", str(tmp_path)])
    assert init.exit_code == 0, init.output
    monkeypatch.chdir(tmp_path / "repo-a")

    top_level = runner.invoke(app, ["status"])
    existing = runner.invoke(app, ["repo", "status", str(tmp_path)])

    assert top_level.exit_code == 0, top_level.output
    assert existing.exit_code == 0, existing.output
    assert top_level.output == existing.output
    assert top_level.output.startswith("SCOPE\tTARGET\tREPO\tACTION\tBRANCH\tUPSTREAM\tSTATE\tREASON\n")
    assert "prototype" not in top_level.output.lower()
