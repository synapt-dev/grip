from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path
import sys

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gr2.python_cli import execops, migration, spec_apply
from gr2.python_cli.app import app


runner = CliRunner()


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _init_bare_remote(tmp_path: Path, name: str) -> tuple[Path, str]:
    source = tmp_path / f"{name}-src"
    source.mkdir(parents=True, exist_ok=True)
    assert _git(source, "init", "-b", "main").returncode == 0
    assert _git(source, "config", "user.name", "Atlas").returncode == 0
    assert _git(source, "config", "user.email", "atlas@example.com").returncode == 0
    (source / "README.md").write_text(f"# {name}\n")
    assert _git(source, "add", "README.md").returncode == 0
    assert _git(source, "commit", "-m", "initial").returncode == 0

    remote = tmp_path / f"{name}.git"
    assert subprocess.run(
        ["git", "clone", "--bare", str(source), str(remote)],
        capture_output=True,
        text=True,
        check=False,
    ).returncode == 0
    return remote, remote.as_uri()


def _write_workspace_spec(workspace_root: Path, repo_name: str, repo_url: str, *, legacy_agent_id: str | None = None) -> None:
    spec_path = workspace_root / ".grip" / "workspace_spec.toml"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    extra = f'\nagent_id = "{legacy_agent_id}"' if legacy_agent_id else ""
    spec_path.write_text(
        textwrap.dedent(
            f"""
            schema_version = 1
            workspace_name = "{workspace_root.name}"

            [[repos]]
            name = "{repo_name}"
            path = "repos/{repo_name}"
            url = "{repo_url}"

            [[units]]
            name = "atlas"
            path = "agents/atlas/home"
            repos = ["{repo_name}"]{extra}
            """
        ).strip()
        + "\n"
    )


def test_compile_gr1_workspace_spec_omits_agent_id() -> None:
    compiled = migration.compile_gr1_to_workspace_spec(
        Path("/tmp/example"),
        {
            "repos": {
                "app": {"path": "./repos/app", "url": "https://example.com/app.git"},
            }
        },
        {
            "agents": {
                "atlas": {"worktree": "atlas-tree", "channel": "#dev"},
            }
        },
    )

    assert compiled["units"] == [
        {
            "name": "atlas",
            "path": "agents/atlas/home",
            "repos": ["app"],
            "migration_source": {"worktree": "atlas-tree", "channel": "#dev"},
        }
    ]
    rendered = migration.render_workspace_spec(compiled)
    assert 'agent_id = "' not in rendered


def test_render_unit_toml_ignores_legacy_agent_id() -> None:
    unit_toml = spec_apply.render_unit_toml(
        {
            "name": "atlas",
            "repos": ["app"],
            "agent_id": "gr1:atlas",
        }
    )
    assert 'agent_id = "' not in unit_toml
    assert 'name = "atlas"' in unit_toml
    assert 'repos = ["app"]' in unit_toml


def test_exec_lease_event_does_not_emit_agent_id_from_workspace_spec(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    _, repo_url = _init_bare_remote(tmp_path, "app")
    _write_workspace_spec(workspace_root, "app", repo_url, legacy_agent_id="legacy-atlas")

    result = runner.invoke(app, ["apply", str(workspace_root), "--yes"])
    assert result.exit_code == 0, result.stdout

    result = runner.invoke(
        app,
        [
            "lane",
            "create",
            str(workspace_root),
            "atlas",
            "feat-auth",
            "--repos",
            "app",
            "--branch",
            "feat/auth",
        ],
    )
    assert result.exit_code == 0, result.stdout

    execops.acquire_exec_lease(workspace_root, "atlas", "feat-auth", "agent:atlas", 900)
    execops.release_exec_lease(workspace_root, "atlas", "feat-auth", "agent:atlas")

    events_path = workspace_root / ".grip" / "events" / "lane_events.jsonl"
    rows = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
    lease_rows = [row for row in rows if row.get("type") in {"lease_acquire", "lease_release"}]

    assert lease_rows
    assert all("agent_id" not in row for row in lease_rows)
    assert all(row["owner_unit"] == "atlas" for row in lease_rows)
