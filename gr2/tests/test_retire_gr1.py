"""`gr2 workspace retire-gr1` ends coexistence: it moves .gitgrip aside so `gr` resolves to gr2.

The verb moves, never deletes; prints the one command that undoes the move; and refuses
when the move would break gr1 spawn (agents.toml or spawn dir present) unless told.
"""
from __future__ import annotations

import datetime
import json
import shlex
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli.app import app
from gr2.python_cli import migration
from tests.conftest import make_cli_runner
from tests.test_migration import _write_gr1_workspace

runner = make_cli_runner()


def _stamp() -> str:
    return datetime.date.today().strftime("%Y%m%d")


@pytest.fixture
def coexisting(tmp_path: Path) -> Path:
    """A gr1 workspace after migrate-gr1, with no gr1 spawn state (the movable case)."""
    _write_gr1_workspace(tmp_path)
    migration.migrate_gr1_workspace(tmp_path)
    (tmp_path / ".gitgrip" / "agents.toml").unlink()
    return tmp_path


def _run(root: Path, *extra: str):
    return runner.invoke(app, ["workspace", "retire-gr1", str(root), *extra])


def test_moves_gitgrip_aside_and_phase_becomes_gr2_only(coexisting: Path) -> None:
    before = (coexisting / ".gitgrip" / "state.json").read_bytes()
    result = _run(coexisting)
    assert result.exit_code == 0, result.output
    target = coexisting / f".gitgrip.retired-{_stamp()}"
    assert not (coexisting / ".gitgrip").exists()
    assert (target / "state.json").read_bytes() == before  # moved, never deleted
    assert migration.workspace_status(coexisting)["phase"] == "gr2-only"


def test_prints_the_undo_command_and_it_works(coexisting: Path) -> None:
    result = _run(coexisting)
    target = coexisting / f".gitgrip.retired-{_stamp()}"
    undo = f"mv {shlex.quote(str(target))} {shlex.quote(str(coexisting / '.gitgrip'))}"
    assert undo in result.stdout
    target.rename(coexisting / ".gitgrip")
    assert migration.workspace_status(coexisting)["phase"] == "coexistence"


def test_json_receipt_names_target_and_undo(coexisting: Path) -> None:
    result = _run(coexisting, "--json")
    payload = json.loads(result.stdout)
    assert payload["retired_to"].endswith(f".gitgrip.retired-{_stamp()}")
    assert payload["undo"].startswith("mv ")


def test_refuses_when_not_coexistence(tmp_path: Path) -> None:
    _write_gr1_workspace(tmp_path)  # gr1-only, migrate-gr1 never ran
    (tmp_path / ".gitgrip" / "agents.toml").unlink()
    result = _run(tmp_path)
    assert result.exit_code == 1
    assert "migrate-gr1" in result.stderr
    assert (tmp_path / ".gitgrip").is_dir()


def test_refuses_when_agents_toml_present_and_says_what_it_breaks(coexisting: Path) -> None:
    (coexisting / ".gitgrip" / "agents.toml").write_text('[agents.a]\nworktree = "main"\n')
    result = _run(coexisting)
    assert result.exit_code == 1
    assert "agents.toml" in result.stderr
    assert "gr1 spawn, status and every gr1 verb here stop working" in result.stderr
    assert "--break-gr1-spawn" in result.stderr
    assert (coexisting / ".gitgrip").is_dir()


def test_refuses_when_spawn_dir_present(coexisting: Path) -> None:
    (coexisting / ".gitgrip" / "spawn").mkdir()
    result = _run(coexisting)
    assert result.exit_code == 1
    assert "spawn" in result.stderr
    assert "--break-gr1-spawn" in result.stderr
    assert (coexisting / ".gitgrip").is_dir()


def test_override_flag_moves_despite_agents_toml(coexisting: Path) -> None:
    (coexisting / ".gitgrip" / "agents.toml").write_text('[agents.a]\nworktree = "main"\n')
    result = _run(coexisting, "--break-gr1-spawn")
    assert result.exit_code == 0, result.stderr
    assert not (coexisting / ".gitgrip").exists()
    assert (coexisting / f".gitgrip.retired-{_stamp()}" / "agents.toml").is_file()


def test_refuses_when_target_name_is_taken(coexisting: Path) -> None:
    taken = coexisting / f".gitgrip.retired-{_stamp()}"
    taken.mkdir()
    (taken / "keep").write_text("x")
    result = _run(coexisting)
    assert result.exit_code == 1
    assert "already exists" in result.stderr
    assert (coexisting / ".gitgrip").is_dir()
    assert (taken / "keep").read_text() == "x"


def test_undo_line_survives_a_space_in_the_root(tmp_path: Path) -> None:
    root = tmp_path / "my workspace"
    root.mkdir()
    _write_gr1_workspace(root)
    migration.migrate_gr1_workspace(root)
    (root / ".gitgrip" / "agents.toml").unlink()
    result = _run(root)
    assert result.exit_code == 0, result.stderr
    target = root / f".gitgrip.retired-{_stamp()}"
    undo = f"mv {shlex.quote(str(target))} {shlex.quote(str(root / '.gitgrip'))}"
    assert undo in result.stdout
    assert shlex.split(undo) == ["mv", str(target), str(root / ".gitgrip")]
    subprocess.run(["sh", "-c", undo], check=True)  # the printed line itself, as a shell reads it
    assert (root / ".gitgrip" / "state.json").is_file()
    assert not target.exists()
    assert migration.workspace_status(root)["phase"] == "coexistence"


def test_second_run_on_a_retired_workspace_says_so_not_run_migrate(coexisting: Path) -> None:
    assert _run(coexisting).exit_code == 0
    result = _run(coexisting)
    assert result.exit_code == 1
    assert "already" in result.stderr and "gr2-only" in result.stderr
    assert "run `gr2 workspace migrate-gr1`" not in result.stderr
